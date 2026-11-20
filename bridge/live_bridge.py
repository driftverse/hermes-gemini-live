"""Gemini Live <-> OpenAI-Live-protocol bridge (loopback only).

Clients that only speak OpenAI's Live contract (WebRTC audio + an `oai-events` data channel,
e.g. the Hermes phone app) talk to THIS process instead of OpenAI. Per session it:

  * answers the client's WebRTC offer (aiortc), receives mic audio (Opus -> 16 kHz PCM),
    plays Gemini's 24 kHz PCM back as an Opus track;
  * holds one Gemini Live WebSocket (API key read here, never sent to any client);
  * turns Gemini's `delegate_to_hermes` function call into `session.delegation.created`, collects
    the client's `session.commentary.append` chunks (Hermes' streamed reply) and answers the
    function call with them once the reply has settled;
  * maps transcripts, barge-in, mute and close between the two protocols.

Only the Hermes dashboard route (same host) calls POST /offer. Nothing here is exposed publicly.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
import urllib.request
import uuid
from fractions import Fraction

import av
import numpy as np
import websockets
from aiohttp import web
from aiortc import MediaStreamTrack, RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription

log = logging.getLogger("live_bridge")

GEMINI_WS = ("wss://generativelanguage.googleapis.com/ws/"
             "google.ai.generativelanguage.v1alpha.GenerativeService.BidiGenerateContent")
DELEGATE = "delegate_to_hermes"
SETUP_TIMEOUT = 12.0
SETTLE_QUIET_S = 2.0        # no new reply chunk for this long -> reply considered complete
SETTLE_AFTER_THINK_S = 4.0  # ...and no tool-progress note this recent
NO_REPLY_TIMEOUT_S = 150.0
# Google may still hold the old socket for a moment after it drops; an immediate re-dial is rejected
# with 1008 "already connected to an existing client". Retry the resume with growing pauses.
RESUME_RETRY_DELAYS_S = (0.5, 1.5, 3.0)
IN_RATE, OUT_RATE, WIRE_RATE = 16_000, 24_000, 48_000
# STUN adds ~5 s to every session start (aiortc waits for each interface to time out). Off by default:
# same-network and IPv6-direct phones do not need it. Set LIVE_BRIDGE_STUN=stun:... to enable.
STUN = os.environ.get("LIVE_BRIDGE_STUN", "")
# Comma list of oai-event types NOT to send to the client (diagnostics for client-side bugs).
SUPPRESS = {x for x in os.environ.get("LIVE_BRIDGE_SUPPRESS", "").split(",") if x}


# ---- TURN relay (Cloudflare Realtime TURN) ---------------------------------------------------
# A phone on another network cannot reach this host's private addresses. The BRIDGE asks a public
# relay for an address and advertises it in its WebRTC answer; the phone then only needs outbound
# internet. Key id + API token come from a mode-600 env file written by `penny-turn-key` (never
# printed, never sent to any client or model). Short-lived per-session credentials are minted from it.
TURN_ENV = os.path.expanduser(os.environ.get("LIVE_BRIDGE_TURN_ENV", "~/.config/live-bridge/turn.env"))
_ice_cache: dict = {"at": 0.0, "servers": []}


def _turn_secrets() -> tuple[str, str]:
    key_id = os.environ.get("CF_TURN_KEY_ID", "")
    token = os.environ.get("CF_TURN_API_TOKEN", "")
    if not (key_id and token) and os.path.exists(TURN_ENV):
        for line in open(TURN_ENV, encoding="utf-8"):
            name, _, value = line.strip().partition("=")
            if name == "CF_TURN_KEY_ID":
                key_id = value
            elif name == "CF_TURN_API_TOKEN":
                token = value
    return key_id.strip(), token.strip()


def _fetch_ice() -> list:
    key_id, token = _turn_secrets()
    if not (key_id and token):
        return []
    req = urllib.request.Request(
        f"https://rtc.live.cloudflare.com/v1/turn/keys/{key_id}/credentials/generate-ice-servers",
        data=json.dumps({"ttl": 3600}).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": "live-bridge/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode())
    servers = []
    for entry in data.get("iceServers", []):
        urls = entry.get("urls") or []
        urls = [urls] if isinstance(urls, str) else urls
        # UDP TURN + STUN only: aioice handles those; the TLS/TCP variants add nothing for a server.
        urls = [u for u in urls if u.startswith("stun:") or (u.startswith("turn:") and "transport=tcp" not in u)]
        if urls:
            servers.append({"urls": urls, "username": entry.get("username"), "credential": entry.get("credential")})
    return servers


async def ice_servers() -> list:
    """Cloudflare relay servers (cached ~10 min); [] when unconfigured or unreachable."""
    if time.time() - _ice_cache["at"] < 600 and _ice_cache["servers"]:
        return _ice_cache["servers"]
    try:
        servers = await asyncio.get_running_loop().run_in_executor(None, _fetch_ice)
    except Exception as exc:  # noqa: BLE001 - never take Live down because the relay is unreachable
        log.warning("TURN credentials unavailable (%s); continuing without relay", type(exc).__name__)
        return []
    if servers:
        _ice_cache.update(at=time.time(), servers=servers)
    return servers


def _cand_summary(sdp: str) -> str:
    """Candidate types/protocols only (host/srflx/relay x udp/tcp); no addresses in the log."""
    import collections, re
    counts = collections.Counter()
    for m in re.finditer(r"a=candidate:\S+ \d+ (\w+) \d+ (\S+) \d+ typ (\w+)", sdp):
        counts[f"{m.group(3)}/{m.group(1).lower()}"] += 1
    return ", ".join(f"{k}x{v}" for k, v in sorted(counts.items())) or "none"


class OutTrack(MediaStreamTrack):
    """Plays queued 24 kHz PCM16 as 20 ms / 48 kHz frames, silence when idle."""

    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self.buf = bytearray()
        self._resampler = av.AudioResampler(format="s16", layout="mono", rate=WIRE_RATE)
        self._start = 0.0
        self._ts = 0

    def push(self, pcm24: bytes) -> None:
        if len(pcm24) < 2:
            return
        arr = np.frombuffer(pcm24[: len(pcm24) // 2 * 2], dtype=np.int16).reshape(1, -1)
        frame = av.AudioFrame.from_ndarray(arr, format="s16", layout="mono")
        frame.sample_rate = OUT_RATE
        for out in self._resampler.resample(frame):
            self.buf += out.to_ndarray().tobytes()

    def clear(self) -> None:
        self.buf.clear()

    async def recv(self):
        n = 960
        if self._start == 0.0:
            self._start = time.time()
        else:
            wait = self._start + self._ts / WIRE_RATE - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
        chunk = bytes(self.buf[: n * 2])
        del self.buf[: n * 2]
        if len(chunk) < n * 2:
            chunk += b"\x00" * (n * 2 - len(chunk))
        frame = av.AudioFrame.from_ndarray(np.frombuffer(chunk, dtype=np.int16).reshape(1, -1),
                                           format="s16", layout="mono")
        frame.sample_rate = WIRE_RATE
        frame.pts = self._ts
        frame.time_base = Fraction(1, WIRE_RATE)
        self._ts += n
        return frame


class Session:
    def __init__(self, api_key: str, model: str, setup: dict) -> None:
        self.key, self.model, self.setup = api_key, model, setup
        self.id = f"gemlive_{uuid.uuid4().hex[:12]}"
        self.t0 = time.time()
        self.pc: RTCPeerConnection | None = None
        self.out = OutTrack()
        self.channel = None
        self.ws = None
        self.resume: str | None = None
        self.muted = False
        self.closed = False
        self.reconnecting = False
        self.pending: dict[str, dict] = {}
        self.done: dict[str, float] = {}
        self.results: dict[str, str] = {}   # call id -> response already sent (replay-safe)
        self.seen: set[str] = set()         # every call id ever dispatched to the client
        self.cancelled: set[str] = set()    # ids the provider cancelled: nothing may be spoken for them
        self.tasks: list[asyncio.Task] = []
        self.late: list[str] = []
        self.late_at = 0.0
        self.counter = 0

    # ---- helpers ----
    def ms(self) -> int:
        return int((time.time() - self.t0) * 1000)

    def emit(self, event: dict) -> None:
        ch = self.channel
        if ch is None or ch.readyState != "open" or event.get("type") in SUPPRESS:
            return
        self.counter += 1
        event.setdefault("event_id", f"srv_{self.counter}")
        ch.send(json.dumps(event))

    async def gsend(self, message: dict) -> None:
        if self.ws is not None:
            try:
                await self.ws.send(json.dumps(message))
            except Exception:  # noqa: BLE001 - closed sockets are handled by the reader
                pass

    # ---- gemini ----
    async def dial(self, resume: str | None = None) -> None:
        setup = json.loads(json.dumps(self.setup))
        if resume:
            setup["setup"]["sessionResumption"] = {"handle": resume}
        ws = await websockets.connect(f"{GEMINI_WS}?key={self.key}", max_size=None, open_timeout=15)
        await ws.send(json.dumps(setup))
        first = json.loads(await asyncio.wait_for(ws.recv(), SETUP_TIMEOUT))
        if "setupComplete" not in first:
            await ws.close()
            raise RuntimeError(f"no setupComplete: {str(first)[:160]}")
        self.ws = ws

    async def gemini_reader(self) -> None:
        while not self.closed:
            ws = self.ws
            try:
                async for raw in ws:
                    await self.on_gemini(json.loads(raw))
            except Exception as exc:  # noqa: BLE001
                log.info("%s gemini socket ended: %s", self.id, type(exc).__name__)
            if self.closed or ws is not self.ws:
                return
            if self.resume and not self.reconnecting:
                self.reconnecting = True
                try:
                    resumed = False
                    for attempt, pause in enumerate((0.0,) + tuple(RESUME_RETRY_DELAYS_S)):
                        if pause:
                            await asyncio.sleep(pause)
                        if self.closed:
                            return
                        try:
                            await self.dial(self.resume)
                            log.info("%s resumed%s", self.id, f" (attempt {attempt + 1})" if attempt else "")
                            resumed = True
                            break
                        except Exception as exc:  # noqa: BLE001
                            log.warning("%s resume attempt %d failed: %s", self.id, attempt + 1, str(exc)[:120])
                    if resumed:
                        continue
                finally:
                    self.reconnecting = False
            await self.close("connection_lost")
            return

    async def on_gemini(self, m: dict) -> None:
        upd = m.get("sessionResumptionUpdate") or {}
        if upd.get("newHandle") and upd.get("resumable") is not False:
            self.resume = upd["newHandle"]
        if m.get("goAway") and self.resume and self.ws is not None:
            await self.ws.close()  # reader loop re-dials with the handle
        sc = m.get("serverContent") or {}
        if sc.get("interrupted"):
            self.out.clear()
        for part in (sc.get("modelTurn") or {}).get("parts", []):
            data = (part.get("inlineData") or {}).get("data")
            if data:
                self.out.push(base64.b64decode(data))
        for key, kind in (("inputTranscription", "input"), ("outputTranscription", "output")):
            text = (sc.get(key) or {}).get("text")
            if text:
                now = self.ms()
                self.emit({"type": f"session.{kind}_transcript.delta", "delta": text,
                           "start_ms": now, "end_ms": now})
        for call in (m.get("toolCall") or {}).get("functionCalls", []):
            if call.get("name") != DELEGATE:
                await self.gsend({"toolResponse": {"functionResponses": [
                    {"id": call["id"], "name": call["name"], "response": {"error": "Unknown function"}}]}})
                continue
            cid = call["id"]
            if cid in self.seen:
                # A replayed call (e.g. after a provider resume) must never reach the client twice.
                log.info("%s duplicate call %s ignored", self.id, cid)
                if cid in self.results:  # already answered: repeat the same answer once, no new work
                    await self.gsend({"toolResponse": {"functionResponses": [{
                        "id": cid, "name": DELEGATE, "response": {"result": self.results[cid]}}]}})
                continue
            self.seen.add(cid)
            self.pending[cid] = {"chunks": [], "last": 0.0, "think": 0.0, "start": time.time()}
            self.emit({"type": "session.delegation.created",
                       "delegation": {"id": cid, "type": "client", "target": "hermes"}})
        for cid in (m.get("toolCallCancellation") or {}).get("ids", []):
            self.pending.pop(cid, None)
            self.cancelled.add(cid)

    async def respond(self, cid: str, text: str) -> None:
        self.pending.pop(cid, None)
        if cid in self.done or cid in self.cancelled:  # one answer per call, none for a cancelled one
            return
        self.done[cid] = time.time()
        self.results[cid] = (text or "Hermes finished without anything to report.")[:6000]
        await self.gsend({"toolResponse": {"functionResponses": [{
            "id": cid, "name": DELEGATE, "response": {"result": self.results[cid]}}]}})

    async def watcher(self) -> None:
        while not self.closed:
            await asyncio.sleep(0.3)
            now = time.time()
            for cid, p in list(self.pending.items()):
                if p["chunks"] and now - p["last"] > SETTLE_QUIET_S and now - p["think"] > SETTLE_AFTER_THINK_S:
                    await self.respond(cid, " ".join(p["chunks"]))
                elif not p["chunks"] and now - p["start"] > NO_REPLY_TIMEOUT_S:
                    await self.respond(cid, "Hermes did not answer in time.")
            if self.late and now - self.late_at > SETTLE_QUIET_S and not self.pending:
                text, self.late = " ".join(self.late), []
                await self.gsend({"realtimeInput": {"text": f"[Update from Hermes, say this briefly]: {text}"}})

    # ---- client (oai-events) ----
    async def on_client(self, raw: str) -> None:
        try:
            ev = json.loads(raw)
        except ValueError:
            return
        kind = ev.get("type")
        cid = ev.get("delegation_id")
        if cid in self.cancelled and kind in ("session.commentary.append", "session.thinking.append"):
            return  # a result for a call the provider cancelled must not be spoken later
        if kind == "session.commentary.append":
            text = str(ev.get("content") or "").strip()
            if not text:
                return
            p = self.pending.get(cid) or (next(iter(self.pending.values()), None) if not cid else None)
            if p is not None:
                p["chunks"].append(text)
                p["last"] = time.time()
            elif cid in self.done or not self.pending:
                self.late.append(text)
                self.late_at = time.time()
        elif kind == "session.thinking.append":
            text = str(ev.get("content") or "")
            p = self.pending.get(cid) or (next(iter(self.pending.values()), None) if not cid else None)
            if p is not None:
                p["think"] = time.time()
                if "without a spoken result" in text and not p["chunks"]:
                    await self.respond(cid or next(iter(self.pending)), text)
        elif kind == "session.input_audio.mute":
            self.muted = True
            await self.gsend({"realtimeInput": {"audioStreamEnd": True}})
        elif kind == "session.input_audio.unmute":
            self.muted = False
        elif kind == "session.close":
            await self.close("close_requested")

    async def pump_audio(self, track) -> None:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=IN_RATE)
        batch = bytearray()
        try:
            while not self.closed:
                frame = await track.recv()
                for f in resampler.resample(frame):
                    batch += f.to_ndarray().tobytes()
                if len(batch) >= 3200:
                    data, batch = bytes(batch), bytearray()
                    if not self.muted and self.ws is not None:
                        await self.gsend({"realtimeInput": {"audio": {
                            "data": base64.b64encode(data).decode(), "mimeType": f"audio/pcm;rate={IN_RATE}"}}})
        except Exception:  # noqa: BLE001 - track ended
            pass

    async def connect_timeout(self, seconds: float = 30.0) -> None:
        """A phone that can never reach us would otherwise hold a Gemini session open for hours."""
        await asyncio.sleep(seconds)
        if not self.closed and self.pc is not None and self.pc.connectionState != "connected":
            log.warning("%s media never connected within %.0fs (connection=%s ice=%s)",
                        self.id, seconds, self.pc.connectionState, self.pc.iceConnectionState)
            await self.close("never_connected")

    async def answer(self, sdp: str, stun: str) -> str:
        relay = await ice_servers()
        servers = [RTCIceServer(urls=r["urls"], username=r["username"], credential=r["credential"]) for r in relay]
        if not servers and stun:
            servers = [RTCIceServer(stun)]
        log.info("%s ice servers: %s", self.id, "cloudflare-relay" if relay else ("stun" if stun else "none"))
        pc = RTCPeerConnection(RTCConfiguration(iceServers=servers))
        self.pc = pc

        @pc.on("track")
        def on_track(track):
            if track.kind == "audio":
                self.tasks.append(asyncio.ensure_future(self.pump_audio(track)))

        @pc.on("datachannel")
        def on_channel(channel):
            self.channel = channel

            def opened():
                self.emit({"type": "session.started", "session": {"id": self.id}})

            if channel.readyState == "open":
                opened()
            else:
                channel.on("open", opened)

            @channel.on("message")
            def on_message(message):
                self.tasks.append(asyncio.ensure_future(self.on_client(message)))

        @pc.on("connectionstatechange")
        async def on_state():
            log.info("%s connection=%s ice=%s", self.id, pc.connectionState, pc.iceConnectionState)
            if pc.connectionState in ("failed", "closed", "disconnected") and not self.closed:
                await self.close("connection_lost")

        @pc.on("iceconnectionstatechange")
        async def on_ice():
            log.info("%s ice=%s gathering=%s", self.id, pc.iceConnectionState, pc.iceGatheringState)

        log.info("%s offer candidates: %s", self.id, _cand_summary(sdp))
        await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
        pc.addTrack(self.out)
        await pc.setLocalDescription(await pc.createAnswer())
        log.info("%s answer candidates: %s", self.id, _cand_summary(pc.localDescription.sdp))
        self.tasks.append(asyncio.ensure_future(self.connect_timeout()))
        self.tasks += [asyncio.ensure_future(self.gemini_reader()), asyncio.ensure_future(self.watcher())]
        return pc.localDescription.sdp

    async def close(self, reason: str) -> None:
        if self.closed:
            return
        self.closed = True
        self.emit({"type": "session.closed", "reason": reason,
                   "usage": {"seconds": round(time.time() - self.t0, 1)}})
        for task in self.tasks:
            if task is not asyncio.current_task():
                task.cancel()
        try:
            if self.ws is not None:
                await self.ws.close()
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.2)  # let the closing event flush
        try:
            if self.pc is not None:
                await self.pc.close()
        except Exception:  # noqa: BLE001
            pass
        SESSIONS.pop(self.id, None)
        log.info("%s closed (%s) after %.0fs", self.id, reason, time.time() - self.t0)


SESSIONS: dict[str, Session] = {}


def _api_key() -> str:
    return (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()


async def offer(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        sdp, model, setup = body["sdp"], body["model"], body["setup"]
    except Exception:  # noqa: BLE001
        return web.json_response({"error": "bad request"}, status=400)
    key = _api_key()
    if not key:
        return web.json_response({"error": "no Gemini API key in bridge environment"}, status=503)
    session = Session(key, model, setup)
    try:
        await session.dial()
    except Exception as exc:  # noqa: BLE001 - report model-level failure so the caller can fail over
        log.info("model %s failed to start: %s", model, str(exc)[:160])
        return web.json_response({"error": "model_start_failed", "model": model,
                                  "detail": str(exc)[:200]}, status=502)
    try:
        answer = await session.answer(sdp, body.get("stun", STUN))
    except Exception as exc:  # noqa: BLE001
        log.exception("webrtc answer failed")
        await session.close("webrtc_failed")
        return web.json_response({"error": "webrtc_failed", "detail": str(exc)[:200]}, status=500)
    SESSIONS[session.id] = session
    log.info("%s started on %s", session.id, model)
    return web.json_response({"session": {"id": session.id, "model": model}, "sdp": answer})


async def health(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "sessions": len(SESSIONS), "key": bool(_api_key()),
                              "relay_configured": all(_turn_secrets())})


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("aioice", "aiortc"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    app = web.Application(client_max_size=2_000_000)
    app.add_routes([web.post("/offer", offer), web.get("/health", health)])
    web.run_app(app, host="127.0.0.1", port=int(os.environ.get("LIVE_BRIDGE_PORT", "8890")), print=None)


if __name__ == "__main__":
    main()
