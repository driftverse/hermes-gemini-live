"""GPT-Live voice chat mode: the full-duplex voice frontend that delegates to Hermes.

``voice.voice_chat_mode: gpt-live`` replaces the chained STT → turn → TTS loop with ONE
full-duplex voice model (OpenAI ``gpt-live-1``) that owns the microphone and the speaker and
delegates every real request to Hermes as its *client-delegation* backend. Hermes stays the
agent: whatever model/provider the session has selected answers, with the full toolset.

Division of labour (the Live API has no tools of its own in client mode):

* the desktop renderer holds the WebRTC media session (mic in, speech out) and the data channel;
* this module resolves WHICH credentials/voice/persona to use and performs the one server-side
  step the API requires — exchanging the browser's SDP offer for an answer with the project key
  (``POST /v1/live/sessions``), so the key never reaches the client;
* the renderer turns each ``session.delegation.created`` into a normal ``prompt.submit`` on the
  active session (surface ``voice-live``) and streams the reply back as
  ``session.commentary.append`` — Hermes' answer is what the voice speaks.

Vendor contract: https://developers.openai.com/api/docs/guides/live (+ live-delegation,
voice-webrtc). Billing is $0.05/min of session time on the OpenAI key, separate from the
Hermes turn.
"""

from __future__ import annotations

import datetime
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

GPT_LIVE_MODE = "gpt-live"
CHAINED_MODE = "chained"
DEFAULT_LIVE_MODEL = "gpt-live-1"
DEFAULT_LIVE_VOICE = "marin"
DEFAULT_LIVE_BASE_URL = "https://api.openai.com/v1"
OPENAI_PROVIDER = "openai"
GEMINI_PROVIDER = "gemini"
DEFAULT_GEMINI_VOICE = "Aoede"
# Failover order for the Gemini Live provider. The chain is tried top to bottom per session
# start; a model that just failed is skipped for GEMINI_COOLDOWN_SECONDS. Only models that were
# seen delegating a factual question through ``delegate_to_hermes`` belong here. Tested and left
# OUT (add via ``voice.gpt_live.gemini.models`` if wanted): ``gemini-3.8-live-extended-thinking``
# (says it will check, then never calls the function) and
# ``gemini-2.5-flash-native-audio-preview-09-2025`` (invented a different disk figure each run
# instead of delegating). When this whole chain fails the desktop falls back to chained voice.
DEFAULT_GEMINI_MODELS = (
    "gemini-3.8-live",
    "gemini-3.1-flash-live-preview",
    "gemini-2.5-flash-native-audio-latest",
    "gemini-2.5-flash-native-audio-preview-12-2025",
)
GEMINI_COOLDOWN_SECONDS = 300
GEMINI_LIVE_WS = ("wss://generativelanguage.googleapis.com/ws/"
                  "google.ai.generativelanguage.v1alpha.GenerativeService.BidiGenerateContentConstrained")
GEMINI_TOKENS_URL = "https://generativelanguage.googleapis.com/v1alpha/auth_tokens"
# Voices the vendor lists for gpt-live-1 (live-conversations guide) plus the realtime defaults it
# accepts; free text stays allowed for custom voices.
GPT_LIVE_VOICES = (
    "marin", "cedar", "quartz", "ripple", "vesper", "willow", "stone", "gleam", "meridian",
    "bossa", "tempo", "beacon", "delta", "cinder",
)

# Persona for the voice layer. Short on purpose: the live model has a small context window and
# the vendor guide asks for role + style + a labelled delegation policy, nothing more. The
# backend (Hermes) carries the real instructions, tools and memory.
LIVE_PERSONA = (
    "You are Hermes, a calm and friendly voice assistant. Speak naturally at an unhurried pace. "
    "Be clear and direct, not overly cheerful. If the user is frustrated, acknowledge it briefly "
    "and focus on the next helpful step.\n\n"
    "Backchannel policy: Use moderate backchannels. Acknowledge naturally without competing with "
    "the main response.\n\n"
    "Interruption policy: Stop speaking when the user interrupts. Listen to what they say.\n\n"
    "Delegation policy:\n"
    "Backend tools:\n"
    "- Hermes agent: a full AI agent with tools — it can run commands, read and edit files, "
    "browse the web, search, remember things across sessions, schedule tasks, and reason "
    "carefully about anything. It is the one who actually does work and knows facts.\n\n"
    "Delegate to the backend when:\n"
    "- The user asks a question that needs facts, current information, or careful reasoning.\n"
    "- The user asks you to do, check, find, make, fix, run or remember anything.\n"
    "- A correction changes work already requested.\n\n"
    "Do not delegate to the backend when:\n"
    "- The user greets you, makes small talk, or asks you to repeat a result already provided.\n"
    "- You need a brief clarification to understand the request.\n\n"
    "Delegate before giving an answer that depends on backend work. Do not guess the result "
    "while waiting; say briefly that you are checking, then wait for the result."
)

# Per-turn note prepended to the MODEL INPUT (never the byte-stable system prompt) when a turn
# arrives from the live voice layer. Same seam as the HUD note.
VOICE_LIVE_TURN_NOTE = (
    "[Note: this message is a delegation from a live spoken conversation. The text is a voice "
    "transcript (it may contain mis-hearings, hesitations and later corrections; use the latest "
    "intent). Your reply will be spoken aloud by a voice model that paraphrases it: answer in plain "
    "conversational sentences, keep it short (a few sentences unless the user asked for detail), no "
    "markdown, no lists, no code blocks, no URLs read out character by character. Do the work with "
    "your tools as usual; only the final facts need to be spoken. Do not claim an action succeeded "
    "before it actually did.]"
)


def voice_live_turn_note(context: str = "") -> str:
    """The per-turn note plus, when the client sent one, the recent spoken exchange the delegation
    refers to (the user's last words alone are often "yes" or "Thursday, not Friday")."""
    context = context.strip()
    if not context:
        return VOICE_LIVE_TURN_NOTE
    return f"{VOICE_LIVE_TURN_NOTE}\n[Recent spoken conversation, newest last:\n{context}]"


def _voice_section() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config
        voice = load_config().get("voice")
    except Exception:
        return {}
    return voice if isinstance(voice, dict) else {}


def _live_section(voice: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    section = (voice if voice is not None else _voice_section()).get("gpt_live")
    return section if isinstance(section, dict) else {}


def voice_chat_mode(voice: Optional[dict[str, Any]] = None) -> str:
    """``chained`` (default) or ``gpt-live``. Accepts the underscore spelling too."""
    raw = (voice if voice is not None else _voice_section()).get("voice_chat_mode")
    mode = str(raw or CHAINED_MODE).strip().lower().replace("_", "-")
    return GPT_LIVE_MODE if mode in {GPT_LIVE_MODE, "gptlive", "live"} else CHAINED_MODE


def _resolve_credentials(live: dict[str, Any]) -> tuple[str, str]:
    """``(api_key, base_url)`` — ``voice.gpt_live.api_key`` first, else the same OpenAI audio
    chain the STT/TTS providers use (``VOICE_TOOLS_OPENAI_KEY`` → ``OPENAI_API_KEY`` → pool).

    The Nous-managed audio proxy does not carry ``/live/sessions``; this mode is direct-key only.
    """
    from tools.tool_backend_helpers import resolve_openai_audio_api_key
    api_key = str(live.get("api_key") or "").strip() or resolve_openai_audio_api_key()
    base_url = str(live.get("base_url") or DEFAULT_LIVE_BASE_URL).strip().rstrip("/")
    return api_key, base_url


def live_instructions(live: Optional[dict[str, Any]] = None) -> str:
    extra = str((live if live is not None else _live_section()).get("instructions") or "").strip()
    return f"{LIVE_PERSONA}\n\n{extra}" if extra else LIVE_PERSONA


def live_provider(live: Optional[dict[str, Any]] = None) -> str:
    """``openai`` (default, WebRTC ``gpt-live-1``) or ``gemini`` (Google Live API over a WebSocket
    the renderer opens itself with a single-use ephemeral token)."""
    raw = str((live if live is not None else _live_section()).get("provider") or OPENAI_PROVIDER)
    return GEMINI_PROVIDER if raw.strip().lower() in {"gemini", "google"} else OPENAI_PROVIDER


def resolve_gpt_live_status(openai_shape: bool = False) -> dict[str, Any]:
    """Non-secret readiness verdict for the client: which mode is selected and whether Live can
    start (a key resolves for the selected provider). Never returns a key.

    ``openai_shape``: for clients that only know the OpenAI contract (the phone app) answer with
    exactly the OpenAI-era fields; the Gemini provider then serves them through the WebRTC bridge."""
    voice = _voice_section()
    mode = voice_chat_mode(voice)
    live = _live_section(voice)
    if live_provider(live) == GEMINI_PROVIDER:
        gem = _gemini_section(live)
        api_key = _resolve_gemini_key(gem)
        models = gemini_models(gem)
        if openai_shape:
            ok = bool(api_key and models and gemini_bridge_healthy())
            return {
                "mode": mode,
                "available": ok,
                "reason": None if ok else "Gemini Live bridge is not running or no Gemini API key",
                "model": str(live.get("model") or DEFAULT_LIVE_MODEL),
                "voice": str(live.get("voice") or DEFAULT_LIVE_VOICE),
            }
        return {
            "mode": mode,
            "provider": GEMINI_PROVIDER,
            "available": bool(api_key and models),
            "reason": None if (api_key and models) else "no Gemini API key (set GEMINI_API_KEY) or no Live models configured",
            "model": models[0] if models else "",
            "models": models,
            "voice": str(gem.get("voice") or DEFAULT_GEMINI_VOICE),
        }
    api_key, _base = _resolve_credentials(live)
    return {
        "mode": mode,
        "provider": OPENAI_PROVIDER,
        "available": bool(api_key),
        "reason": None if api_key else "no OpenAI API key (set OPENAI_API_KEY or voice.gpt_live.api_key)",
        "model": str(live.get("model") or DEFAULT_LIVE_MODEL),
        "voice": str(live.get("voice") or DEFAULT_LIVE_VOICE),
    }


def build_session_config(history: Optional[list] = None) -> dict[str, Any]:
    """The ``session`` object for ``POST /v1/live/sessions`` (client delegation, WebRTC — the
    transport negotiates the audio format, so none is set)."""
    live = _live_section()
    config: dict[str, Any] = {
        "model": str(live.get("model") or DEFAULT_LIVE_MODEL),
        "instructions": live_instructions(live),
        "audio": {"output": {"voice": str(live.get("voice") or DEFAULT_LIVE_VOICE)}},
        "delegation": {"type": "client"},
    }
    if history:
        config["input"] = history
    return config


def create_webrtc_session(sdp_offer: str, history: Optional[list] = None) -> dict[str, Any]:
    """Exchange the renderer's SDP offer for the Live session answer.

    Returns the vendor response ``{"session": {"id": ...}, "transport": {"type": "webrtc",
    "sdp": ...}}``. Raises ``ValueError`` for a missing key and ``RuntimeError`` (with the vendor
    status/detail) for a rejected request.
    """
    live = _live_section()
    api_key, base_url = _resolve_credentials(live)
    if not api_key:
        raise ValueError("GPT-Live needs an OpenAI API key (OPENAI_API_KEY or voice.gpt_live.api_key)")
    body = json.dumps({
        "session": build_session_config(history),
        "transport": {"type": "webrtc", "sdp": sdp_offer},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/live/sessions", data=body, method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:600]
        logger.warning("GPT-Live session creation failed: %s %s", exc.code, detail)
        raise RuntimeError(f"GPT-Live session creation failed ({exc.code}): {detail}") from exc


# ---------------------------------------------------------------------------------------------
# Gemini Live provider (``voice.gpt_live.provider: gemini``)
#
# Same division of labour as the OpenAI path: the renderer owns mic + speaker, this module owns
# credentials. The Google API key never leaves this host: the renderer receives a single-use
# ephemeral token (valid ~2 minutes to START a session) plus the exact ``setup`` message to send.
# Gemini has no "client delegation" mode, so delegation is one blocking function,
# ``delegate_to_hermes(request)``; the renderer answers it with Hermes' reply text.
# ---------------------------------------------------------------------------------------------

GEMINI_PERSONA = (
    "You are Penny, a calm, warm voice assistant with a dry sense of humour. Introduce yourself "
    "only as Penny, never as Gemini, a model, or a Google product. Speak naturally and briefly, "
    "at an unhurried pace, in plain spoken sentences.\n\n"
    "You have no knowledge or tools of your own about the user's machine, files, projects, "
    "calendar, messages, memory or the outside world. The Hermes agent behind you does: it can "
    "run commands, read and edit files, browse the web, remember things across sessions and "
    "schedule tasks. Call delegate_to_hermes for ALL of that.\n\n"
    "Call delegate_to_hermes when:\n"
    "- The user asks a question that needs facts, current information or careful reasoning.\n"
    "- The user asks you to do, check, find, make, fix, run, open, send or remember anything.\n"
    "- The user corrects or changes a request already given.\n"
    "Pass the user's full request in your own clear words, including any correction.\n\n"
    "Do not call it for greetings, small talk, thanks, a request to repeat something you already "
    "said, or a short clarifying question. Do not call it for background noise, coughs or "
    "half-heard fragments; stay quiet or ask them to repeat.\n\n"
    "Never guess an answer that depends on the machine or on facts. When you delegate, say at most "
    "one very short line such as 'one moment' first, then wait for the result and tell the user "
    "what it says in your own words, keeping it short. Never claim something was done before the "
    "result says so. When you are interrupted, stop talking and listen."
)

DELEGATE_FUNCTION = {
    "name": "delegate_to_hermes",
    "description": (
        "Ask the Hermes agent to do real work or look anything up: facts, current information, "
        "the user's machine, files, projects, web, memory, scheduling, running commands. Returns "
        "Hermes' answer as text to say to the user."),
    "parameters": {
        "type": "OBJECT",
        "properties": {"request": {"type": "STRING",
                                   "description": "The user's full request, in clear words."}},
        "required": ["request"],
    },
}

_cooldowns: dict[str, float] = {}
_cooldown_lock = threading.Lock()


def _gemini_section(live: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    section = (live if live is not None else _live_section()).get("gemini")
    return section if isinstance(section, dict) else {}


def gemini_models(gem: Optional[dict[str, Any]] = None) -> list[str]:
    raw = (gem if gem is not None else _gemini_section()).get("models")
    items = raw if isinstance(raw, (list, tuple)) else list(DEFAULT_GEMINI_MODELS)
    out: list[str] = []
    for item in items:
        name = str(item or "").strip().removeprefix("models/")
        if name and name not in out:
            out.append(name)
    return out


def _resolve_gemini_key(gem: dict[str, Any]) -> str:
    key = str(gem.get("api_key") or "").strip()
    if key:
        return key
    try:
        from hermes_cli.config import get_env_value
        return str(get_env_value("GEMINI_API_KEY") or get_env_value("GOOGLE_API_KEY") or "").strip()
    except Exception:
        return ""


def gemini_instructions(live: Optional[dict[str, Any]] = None) -> str:
    extra = str((live if live is not None else _live_section()).get("instructions") or "").strip()
    return f"{GEMINI_PERSONA}\n\n{extra}" if extra else GEMINI_PERSONA


def _history_to_gemini(history: Optional[list]) -> str:
    """Flatten the OpenAI-shaped seed history into a short transcript for the system prompt."""
    lines: list[str] = []
    for item in history or []:
        try:
            role = "User" if item.get("role") == "user" else "Penny"
            text = " ".join(str(part.get("text") or "") for part in (item.get("content") or [])).strip()
        except AttributeError:
            continue
        if text:
            lines.append(f"{role}: {text[:600]}")
    return "\n".join(lines[-24:])


def build_gemini_setup(model: str, history: Optional[list] = None,
                       resume_handle: Optional[str] = None) -> dict[str, Any]:
    """The first WebSocket message (``{"setup": ...}``) for ``model``."""
    live = _live_section()
    gem = _gemini_section(live)
    instructions = gemini_instructions(live)
    transcript = _history_to_gemini(history)
    if transcript:
        instructions += ("\n\nRecent text conversation with this user (for context only):\n" + transcript)
    generation: dict[str, Any] = {
        "responseModalities": ["AUDIO"],
        "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {
            "voiceName": str(gem.get("voice") or DEFAULT_GEMINI_VOICE)}}},
    }
    if "extended-thinking" in model:
        generation["thinkingConfig"] = {"thinkingLevel": str(gem.get("thinking_level") or "low").lower()}
    setup: dict[str, Any] = {
        "model": f"models/{model}",
        "generationConfig": generation,
        "systemInstruction": {"parts": [{"text": instructions}]},
        "tools": [{"functionDeclarations": [DELEGATE_FUNCTION]}],
        "contextWindowCompression": {"slidingWindow": {}},
        "sessionResumption": ({"handle": resume_handle} if resume_handle else {}),
        "inputAudioTranscription": {},
        "outputAudioTranscription": {},
    }
    return {"setup": setup}


def mark_gemini_failure(model: str) -> None:
    """Skip ``model`` for GEMINI_COOLDOWN_SECONDS (called when a client reports a failed start)."""
    with _cooldown_lock:
        _cooldowns[model] = time.monotonic() + GEMINI_COOLDOWN_SECONDS


def _cooling(model: str) -> bool:
    with _cooldown_lock:
        until = _cooldowns.get(model, 0.0)
        if until and until <= time.monotonic():
            _cooldowns.pop(model, None)
            return False
        return until > 0.0


def pick_gemini_model(models: list[str], exclude: Optional[list[str]] = None,
                      pinned: Optional[str] = None) -> Optional[str]:
    """First configured model that is neither excluded for this start attempt nor cooling down.
    When every remaining model is cooling the cooldown is ignored once (a stale cooldown must not
    make Live unusable); ``exclude`` always wins so one attempt never loops."""
    excluded = {str(m).removeprefix("models/") for m in (exclude or [])}
    if pinned:
        pinned = pinned.removeprefix("models/")
        return pinned if pinned in models and pinned not in excluded else None
    remaining = [m for m in models if m not in excluded]
    for model in remaining:
        if not _cooling(model):
            return model
    return remaining[0] if remaining else None


def _mint_gemini_token(api_key: str) -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    body = json.dumps({"uses": 1,
                       "expireTime": iso(now + datetime.timedelta(minutes=30)),
                       "newSessionExpireTime": iso(now + datetime.timedelta(minutes=2))}).encode("utf-8")
    req = urllib.request.Request(
        GEMINI_TOKENS_URL, data=body, method="POST",
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            name = str(json.loads(resp.read().decode("utf-8")).get("name") or "")
    except urllib.error.HTTPError as exc:
        # Never include the request headers or the key; the vendor body is short and key-free.
        detail = exc.read().decode("utf-8", "replace")[:300]
        logger.warning("Gemini Live token mint failed: %s %s", exc.code, detail)
        raise RuntimeError(f"Gemini Live token request failed ({exc.code})") from None
    if not name:
        raise RuntimeError("Gemini Live token response had no token")
    return name


def create_gemini_session(history: Optional[list] = None, failed: Optional[list] = None,
                          model: Optional[str] = None,
                          resume_handle: Optional[str] = None) -> dict[str, Any]:
    """Pick the next Live model in the chain and hand the renderer what it needs to dial it.

    ``failed``: ``[{"model": ..., "reason": ...}]`` models that already failed to start in THIS
    attempt (they are excluded and put on cooldown). ``model`` + ``resume_handle`` re-dial the
    same model after a dropped connection. Raises ``ValueError`` (no key / no models) or
    ``RuntimeError`` (chain exhausted, token mint failed).
    """
    live = _live_section()
    gem = _gemini_section(live)
    api_key = _resolve_gemini_key(gem)
    models = gemini_models(gem)
    if not api_key:
        raise ValueError("Gemini Live needs a Gemini API key (GEMINI_API_KEY)")
    if not models:
        raise ValueError("voice.gpt_live.gemini.models is empty")
    exclude: list[str] = []
    for item in failed or []:
        name = str((item or {}).get("model") or "").removeprefix("models/")
        if name:
            exclude.append(name)
            mark_gemini_failure(name)
    chosen = pick_gemini_model(models, exclude, pinned=model)
    if not chosen:
        raise RuntimeError("Every Gemini Live model failed to start: " + ", ".join(exclude or models))
    token = _mint_gemini_token(api_key)
    return {
        "session": {"provider": GEMINI_PROVIDER, "model": chosen, "id": None},
        "transport": {
            "type": "websocket",
            "url": f"{GEMINI_LIVE_WS}?access_token={token}",
            "setup": build_gemini_setup(chosen, history, resume_handle),
            "remaining": [m for m in models if m != chosen and m not in exclude],
        },
    }


# ---------------------------------------------------------------------------------------------
# WebRTC bridge for clients that only speak the OpenAI Live contract (e.g. the phone app).
# A loopback relay (``live_bridge.py``, own venv/service) terminates their WebRTC and holds the
# Gemini socket; this side just walks the model chain against it.
# ---------------------------------------------------------------------------------------------

DEFAULT_BRIDGE_URL = "http://127.0.0.1:8890"


def _bridge_url(gem: Optional[dict[str, Any]] = None) -> str:
    return str((gem if gem is not None else _gemini_section()).get("bridge_url") or DEFAULT_BRIDGE_URL).rstrip("/")


def gemini_bridge_healthy() -> bool:
    try:
        with urllib.request.urlopen(f"{_bridge_url()}/health", timeout=2) as resp:
            return bool(json.loads(resp.read().decode("utf-8")).get("ok"))
    except Exception:
        return False


def create_gemini_bridge_session(sdp_offer: str, history: Optional[list] = None, peer_ip: str = "") -> dict[str, Any]:
    """Answer an OpenAI-style WebRTC offer through the bridge, failing over down the model chain.
    Returns the same shape as ``create_webrtc_session``."""
    live = _live_section()
    gem = _gemini_section(live)
    models = gemini_models(gem)
    if not _resolve_gemini_key(gem):
        raise ValueError("Gemini Live needs a Gemini API key (GEMINI_API_KEY)")
    if not models:
        raise ValueError("voice.gpt_live.gemini.models is empty")
    failed: list[str] = []
    last = ""
    while True:
        model = pick_gemini_model(models, failed)
        if not model:
            raise RuntimeError("Every Gemini Live model failed to start: " + ", ".join(failed or models)
                               + (f" ({last})" if last else ""))
        payload: dict[str, Any] = {"sdp": sdp_offer, "model": model, "setup": build_gemini_setup(model, history)}
        if peer_ip:
            # The caller's public address lets the bridge open its TURN relay for a phone behind NAT.
            payload["peer_ip"] = peer_ip
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(f"{_bridge_url(gem)}/offer", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                out = json.loads(resp.read().decode("utf-8"))
            return {"session": {"id": out["session"]["id"]},
                    "transport": {"type": "webrtc", "sdp": out["sdp"]}}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            if exc.code == 502:  # this model would not start: next one
                logger.warning("Gemini Live bridge: %s failed to start: %s", model, detail)
                failed.append(model)
                mark_gemini_failure(model)
                last = detail[:120]
                continue
            raise RuntimeError(f"Gemini Live bridge error ({exc.code}): {detail}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise RuntimeError(f"Gemini Live bridge is not reachable: {exc}") from None
