"""Apply the 'forward the phone's public address to the bridge' edit to a Hermes tree.

Usage: apply_peerip_core.py <hermes_root>      (a scratch copy or the real checkout; idempotent)

Edits two files by exact-string replacement and refuses to run if the expected text is not found, so a Hermes
update that changed these lines fails loudly instead of corrupting anything.
"""
import sys
from pathlib import Path

root = Path(sys.argv[1])
audio = root / 'hermes_cli/web_routers/audio.py'
live = root / 'tools/voice_live.py'


def edit(path: Path, old: str, new: str, marker: str) -> None:
    text = path.read_text()
    if marker in text:
        print(f'{path.name}: already applied ({marker!r})')
        return
    if text.count(old) != 1:
        raise SystemExit(f'{path.name}: expected text found {text.count(old)} times; refusing to edit')
    path.write_text(text.replace(old, new))
    print(f'{path.name}: edited')


# ---- tools/voice_live.py: optional peer_ip forwarded to the bridge --------------------------------------------
edit(live,
     'def create_gemini_bridge_session(sdp_offer: str, history: Optional[list] = None) -> dict[str, Any]:',
     'def create_gemini_bridge_session(sdp_offer: str, history: Optional[list] = None, peer_ip: str = "") -> dict[str, Any]:',
     'peer_ip: str = ""')
edit(live,
     '''        body = json.dumps({"sdp": sdp_offer, "model": model,
                           "setup": build_gemini_setup(model, history)}).encode("utf-8")''',
     '''        payload: dict[str, Any] = {"sdp": sdp_offer, "model": model, "setup": build_gemini_setup(model, history)}
        if peer_ip:
            # The caller's public address lets the bridge open its TURN relay for a phone behind NAT.
            payload["peer_ip"] = peer_ip
        body = json.dumps(payload).encode("utf-8")''',
     'payload["peer_ip"] = peer_ip')

# ---- hermes_cli/web_routers/audio.py: read the address the Cloudflare edge saw ---------------------------------
edit(audio,
     '''@router.post("/api/audio/voice-live/session")
async def create_voice_live_session(''',
     '''def _caller_public_ip(request: Request) -> str:
    """The caller's public address, else "": CF-Connecting-IP (set by the Cloudflare edge) or, when the dashboard
    already rewrote ``request.client`` from trusted forwarding headers, that address. Only a public address is
    returned. It is a hint that lets the media bridge open its TURN relay for a phone behind NAT; the bridge
    validates it again, it grants nothing beyond a relay permission, and it is never logged.
    """
    import ipaddress

    header = (request.headers.get("cf-connecting-ip") or "").strip()
    host = (getattr(getattr(request, "client", None), "host", "") or "").strip()
    for candidate in (header, host):
        try:
            if candidate and ipaddress.ip_address(candidate).is_global:
                return candidate
        except ValueError:
            continue
    return ""


@router.post("/api/audio/voice-live/session")
async def create_voice_live_session(''',
     'def _caller_public_ip(')
edit(audio,
     '''                result = await _run_config_scoped(profile, lambda: create_gemini_bridge_session(
                    payload.sdp, payload.history))''',
     '''                result = await _run_config_scoped(profile, lambda: create_gemini_bridge_session(
                    payload.sdp, payload.history, _caller_public_ip(request)))''',
     '_caller_public_ip(request)))')
