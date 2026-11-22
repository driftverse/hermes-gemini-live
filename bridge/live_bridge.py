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
import ipaddress
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
PROGRESS = "check_hermes_progress"
# Models whose Live API runs function calls asynchronously (the model keeps talking and listening while a
# call is pending). Only these get the progress tool: a synchronous model is frozen until a call returns.
ASYNC_MODEL_PREFIXES = ("gemini-3.8-live",)
PROGRESS_TOOL = os.environ.get("LIVE_BRIDGE_PROGRESS_TOOL", "1") != "0"
PROGRESS_SCHEDULING = os.environ.get("LIVE_BRIDGE_PROGRESS_SCHEDULING", "INTERRUPT")  # INTERRUPT | WHEN_IDLE | ""
DEBUG_EVENTS = os.environ.get("LIVE_BRIDGE_DEBUG_EVENTS", "") == "1"  # lab only: tool events on the data channel
DUPLICATE_REQUEST_RATIO = 0.82

PROGRESS_DECLARATION = {
    "name": PROGRESS,
    "behavior": "NON_BLOCKING",
    "description": (
        "Returns the REAL current state of every task you handed to Hermes in this conversation: still running "
        "(with Hermes' latest progress note), finished (with Hermes' result) or cancelled. Instant. Call it "
        "whenever the user asks how something is going, whether it is done, whether it worked, what the result "
        "was, or what is happening. Takes no arguments."),
}

DELEGATION_POLICY = (
    "\n\nDELEGATION POLICY.\n"
    "- delegate_to_hermes starts work in the background. After you call it, say ONE short acknowledgement and "
    "carry on talking with the user normally; do not wait in silence. When Hermes' result arrives, tell the user "
    "what it says, briefly, in your own words.\n"
    "- check_hermes_progress returns the real state of work already handed to Hermes. Call it, instead of "
    "delegating again, EVERY time the user asks how something is going, whether it is done, whether it worked, "
    "what the result was, or what is happening, even if you checked a moment ago. Examples that ALWAYS need it, "
    "even right after you heard a result: 'is it done?', 'did that work?', 'did it go through?', 'what happened?', "
    "\"what's happening with that?\", 'how is it going?', 'is the server up now?', and anything else about whether "
    "something worked, finished, is running, is healthy or is still in progress. The state can change at any "
    "moment, so check again each time before you answer.\n"
    "- Never say that something is done, failed, saved, sent or still in progress unless check_hermes_progress "
    "or a Hermes result has just told you so. If you have not checked, check first. Do not guess while waiting.\n"
    "- Do not call delegate_to_hermes again for a request that is already running; call it again only for a "
    "genuinely new request or a correction to one."
)


def prepare_setup(setup: dict, model: str) -> dict:
    """Return a copy of the Gemini setup with the progress tool + delegation policy for async-capable models."""
    out = json.loads(json.dumps(setup))
    if not PROGRESS_TOOL or not str(model).startswith(ASYNC_MODEL_PREFIXES):
        return out
    inner = out.setdefault("setup", {})
    decls = None
    for tool in inner.setdefault("tools", [{"functionDeclarations": []}]):
        if isinstance(tool, dict) and "functionDeclarations" in tool:
            decls = tool["functionDeclarations"]
            break
    if decls is None:
        decls = []
        inner["tools"].append({"functionDeclarations": decls})
    for d in decls:
        if d.get("name") == DELEGATE:
            d["behavior"] = "NON_BLOCKING"
    if not any(d.get("name") == PROGRESS for d in decls):
        decls.append(json.loads(json.dumps(PROGRESS_DECLARATION)))
    parts = inner.setdefault("systemInstruction", {"parts": [{"text": ""}]}).setdefault("parts", [{"text": ""}])
    if parts and "DELEGATION POLICY." not in str(parts[0].get("text", "")):
        parts[0]["text"] = str(parts[0].get("text", "")) + DELEGATION_POLICY
    return out
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


def peer_hint_sdp(sdp: str, peer_ip: str) -> str:
    """Add the client's PUBLIC address to its offer as a placeholder candidate.

    A phone behind NAT offers only private host candidates, so nothing tells the TURN relay which public
    address its packets will come from, and the relay drops them. Adding a placeholder srflx candidate makes
    the bridge bind a TURN channel to that address, which installs the relay permission; the phone's real
    checks then arrive and are learned as peer-reflexive candidates. The placeholder port (9) is never used.
    A bad, private or loopback address is ignored."""
    try:
        ip = ipaddress.ip_address(str(peer_ip).strip())
    except ValueError:
        return sdp
    if not ip.is_global or ip.is_multicast:
        return sdp
    line = f"a=candidate:peerhint 1 udp 1 {ip} 9 typ srflx raddr {'::' if ip.version == 6 else '0.0.0.0'} rport 0"
    if line in sdp:
        return sdp
    out: list[str] = []
    for raw in sdp.splitlines():
        out.append(raw)
        if raw.startswith("a=ice-pwd:"):
            out.append(line)
    return "\r\n".join(out) + "\r\n"


def ice_report(pc) -> str:
    """One line on how ICE got on (counts and types only, never addresses). Never raises."""
    try:
        conn = pc.sctp.transport.transport._connection
    except Exception:  # noqa: BLE001
        return "ice report unavailable (no transport)"
    try:
        import collections
        states = collections.Counter(str(getattr(p, "state", "?")).split(".")[-1].lower() for p in conn._check_list)
        remote = collections.Counter(c.type for c in conn.remote_candidates)
        picked = ", ".join(f"{p.local_candidate.type}->{p.remote_candidate.type}" for p in conn._nominated.values()) or "none"
        return (f"ice report: pairs {dict(states) or 'none'}; remote candidates {dict(remote) or 'none'}; "
                f"learned from inbound checks (prflx) {remote.get('prflx', 0)}; nominated {picked}")
    except Exception as exc:  # noqa: BLE001
        return f"ice report failed: {type(exc).__name__}"


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
        self.key, self.model = api_key, model
        self.setup = prepare_setup(setup, model)
        self.progress_enabled = PROGRESS_TOOL and str(model).startswith(ASYNC_MODEL_PREFIXES)
        self.work: dict[str, dict] = {}     # call id -> what Hermes was asked and has actually reported
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
            if call.get("name") == PROGRESS and self.progress_enabled:
                report = self.progress_report()
                self.debug("progress_check", report=report)
                response = {"result": report}
                if PROGRESS_SCHEDULING:
                    response["scheduling"] = PROGRESS_SCHEDULING
                await self.gsend({"toolResponse": {"functionResponses": [
                    {"id": call["id"], "name": PROGRESS, "response": response}]}})
                continue
            if call.get("name") != DELEGATE:
                await self.gsend({"toolResponse": {"functionResponses": [
                    {"id": call["id"], "name": call["name"], "response": {"error": "Unknown function"}}]}})
                continue
            cid = call["id"]
            request = str((call.get("args") or {}).get("request") or "").strip()[:400]
            twin = self.running_twin(request) if cid not in self.seen else None
            if twin:
                # The model re-asked for work that is already running: never start it twice.
                self.seen.add(cid)
                log.info("%s duplicate request ignored (same as running %s)", self.id, twin)
                self.debug("duplicate_request", request=request, of=twin)
                await self.gsend({"toolResponse": {"functionResponses": [{
                    "id": cid, "name": DELEGATE,
                    "response": {"result": "That request is already running in the background. Use "
                                           "check_hermes_progress to see how it is going."}}]}})
                continue
            if cid in self.seen:
                # A replayed call (e.g. after a provider resume) must never reach the client twice.
                log.info("%s duplicate call %s ignored", self.id, cid)
                if cid in self.results:  # already answered: repeat the same answer once, no new work
                    await self.gsend({"toolResponse": {"functionResponses": [{
                        "id": cid, "name": DELEGATE, "response": {"result": self.results[cid]}}]}})
                continue
            self.seen.add(cid)
            self.pending[cid] = {"chunks": [], "last": 0.0, "think": 0.0, "start": time.time()}
            self.work[cid] = {"request": request, "started": time.time(), "state": "running", "notes": [],
                              "result": None, "finished": 0.0}
            self.debug("delegate", request=request)
            self.emit({"type": "session.delegation.created",
                       "delegation": {"id": cid, "type": "client", "target": "hermes"}})
        for cid in (m.get("toolCallCancellation") or {}).get("ids", []):
            self.pending.pop(cid, None)
            self.cancelled.add(cid)
            if cid in self.work and self.work[cid]["state"] == "running":
                self.work[cid].update(state="cancelled", finished=time.time())

    def debug(self, what: str, **info) -> None:
        if DEBUG_EVENTS:
            self.emit({"type": "session.debug", "what": what, **info})

    @staticmethod
    def _norm(text: str) -> str:
        return " ".join("".join(ch if ch.isalnum() else " " for ch in text.lower()).split())

    def running_twin(self, request: str) -> str | None:
        """Id of a still-running task asking for (nearly) the same thing, else None."""
        import difflib
        want = self._norm(request)
        if not want:
            return None
        for cid, w in self.work.items():
            if w["state"] == "running" and difflib.SequenceMatcher(None, want, self._norm(w["request"])).ratio() >= DUPLICATE_REQUEST_RATIO:
                return cid
        return None

    def progress_report(self) -> str:
        """What Hermes has ACTUALLY reported, for the model to read aloud. Built only from recorded facts."""
        if not self.work:
            return "No work has been handed to Hermes in this conversation."
        now = time.time()
        lines = ["Real state of the work handed to Hermes (newest first). Say only what this states."]
        for cid, w in list(self.work.items())[::-1][:5]:
            ask = w["request"] or "(request text unavailable)"
            if w["state"] == "running":
                note = w["notes"][-1] if w["notes"] else "no progress note yet"
                lines.append(f'- STILL RUNNING for {int(now - w["started"])} seconds: "{ask}". Latest note from Hermes: '
                             f'{note}. Hermes has NOT reported a result yet, so it is not known whether it worked.')
            elif w["state"] == "done":
                lines.append(f'- FINISHED {int(now - w["finished"])} seconds ago: "{ask}". Hermes reported: {w["result"][:700]}')
            else:
                lines.append(f'- CANCELLED: "{ask}". It did not finish.')
        return "\n".join(lines)

    async def respond(self, cid: str, text: str) -> None:
        self.pending.pop(cid, None)
        if cid in self.done or cid in self.cancelled:  # one answer per call, none for a cancelled one
            return
        self.done[cid] = time.time()
        self.results[cid] = (text or "Hermes finished without anything to report.")[:6000]
        if cid in self.work:
            self.work[cid].update(state="done", result=self.results[cid], finished=time.time())
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
                w = self.work.get(cid or next(iter(self.pending), ""))
                if w is not None and text.strip():
                    w["notes"] = (w["notes"] + [text.strip()[:300]])[-6:]
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
            log.warning("%s %s", self.id, ice_report(self.pc))
            await self.close("never_connected")

    async def answer(self, sdp: str, stun: str, peer_ip: str = "") -> str:
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
            if pc.connectionState == "connected":
                log.info("%s %s", self.id, ice_report(pc))
            if pc.connectionState in ("failed", "closed", "disconnected") and not self.closed:
                await self.close("connection_lost")

        @pc.on("iceconnectionstatechange")
        async def on_ice():
            log.info("%s ice=%s gathering=%s", self.id, pc.iceConnectionState, pc.iceGatheringState)

        log.info("%s offer candidates: %s", self.id, _cand_summary(sdp))
        if peer_ip:
            hinted = peer_hint_sdp(sdp, peer_ip)
            log.info("%s peer address hint %s", self.id, "added" if hinted != sdp else "ignored (not a public address)")
            sdp = hinted
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
        answer = await session.answer(sdp, body.get("stun", STUN), str(body.get("peer_ip") or ""))
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
