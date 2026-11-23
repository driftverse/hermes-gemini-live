# Hermes Gemini Live

Experimental Gemini Live voice integration for Hermes Agent: a direct desktop
WebSocket transport plus a loopback WebRTC bridge for clients that speak Hermes'
OpenAI-style Live contract.

Independent community integration. Not affiliated with or endorsed by Nous Research,
Google, OpenAI or Cloudflare.

## What it does

```text
Patched desktop ── ephemeral-token WebSocket ── Gemini Live
        │                                      │
        └──────── Hermes agent delegation ─────┘

Compatible phone ── WebRTC audio + data ── local bridge ── Gemini Live
        │                 ↑ TURN candidate       │
        └──────── Hermes agent delegation ────────┘
```

The voice model handles audio; Hermes remains the agent that runs tools and retrieves
memory. The bridge translates PCM/Opus audio, transcripts, interruption signals,
mute/close events and delegated replies. Google API keys remain server-side.
Cloudflare TURN is optional and provides relay candidates for cross-network clients.

### Delegation behaviour (v0.2.0, bridge path)

On `gemini-3.8-live` the voice model works like GPT-Live's client delegation: it acknowledges, keeps
talking and listening while Hermes works in the background, and speaks the result when it arrives.
Gemini has no channel for the backend's progress, so the bridge keeps its own record of what Hermes
actually reported for each task and exposes it through an instant `check_hermes_progress` tool.
The delegation policy tells the model to call that tool for any "is it done / how is it going / did
it work" question instead of answering from memory, and a guard stops a request that is already
running from being started twice. Switch off with `LIVE_BRIDGE_PROGRESS_TOOL=0`. Models that are not
async-capable keep the previous behaviour. The desktop WebSocket path does not have this yet.

### Phones behind NAT without a VPN (v0.2.1)

A phone on home or hotel Wi-Fi offers only private addresses. A TURN relay forwards packets only from addresses
it has been told to expect, so the bridge's relay silently dropped the phone and ICE never connected (`never_connected`
after 30 s). The bridge now accepts an optional `peer_ip` (the caller's public address) and adds it to the offer as a
placeholder candidate, which makes aioice bind a TURN channel for that address and opens the relay. The Hermes
dashboard supplies the address from `CF-Connecting-IP`; `scripts/apply_peerip_core.py` applies that small edit to a
Hermes checkout (idempotent, refuses if the expected lines changed) and `scripts/test_peerip_core.py` tests it.
Failed and successful connections now log an `ice report` line (candidate types and counts, never addresses). Confirmed once on a real phone on home Wi-Fi without a VPN (2026-11-22); see docs/VERIFICATION.md for what that does and does not show.

### Stronger delegation without a second answer (v0.2.2)

Gemini follows the delegation rules only some of the time. v0.2.2 puts the rule where the model weighs it most: the
`delegate_to_hermes` tool description now says it must be called FIRST for every request except hello, thanks, goodbye
and okay, and the system prompt ends with a short final reminder with examples. Nothing runs after the model has
spoken, so there is no second answer. Switch off with `LIVE_BRIDGE_TOOL_FIRST=0`. In a typed-text probe against the real
model, questions that did not resemble the examples were delegated 51/54 times against 48/54 before; a first question
set showed 97/100 against 79/100 but overlapped the examples, so treat it as an upper bound. The one persistent miss is
"which model are you using?", which the model answers in persona. Not measured on audio or over many real calls.

An optional backstop (`LIVE_BRIDGE_AUDIT=1`, off by default) starts a delegation itself when the model answered a real
request without delegating, then hands Hermes's result back as text. It is off in the author's deployment because it
produces a second, spoken answer.

**Status: experimental, source distribution—not a one-click app.** A real native
phone session was user-confirmed working on November 18, 2026, with server-side ICE
completion, connected state and clean close. This does not prove every client/build,
network, tool action or long-call scenario. See [verification](docs/VERIFICATION.md)
and [known limitations](docs/LIMITATIONS.md).

## Repository layout

- `bridge/live_bridge.py`: the deployed bridge snapshot; no credentials included.
- `integration/`: exact allowlisted Hermes source snapshots, including added tests.
- `patches/hermes-gemini-live.patch`: reconstructs the integration against the pinned base.
- `tests/`: offline bridge behavior and action-safety tests (74). Renderer safety tests are in `integration/apps/desktop/src/lib/`.
- `scripts/probe_webrtc.py`: opt-in synthetic-audio integration probe, explicitly using a
  stand-in delegated answer rather than a real Hermes agent turn.
- `scripts/relay_only_test_server.py`: test-only bridge that disables direct ICE protocols.
- `deploy/`: examples requiring review and local path/credential setup.

## Prerequisites

- A source checkout of Hermes Agent. This patch is pinned to
  `ae2a81ccc00e9b7c06301523e5bb09608397149c`; it is not promised to apply to newer revisions.
- Python 3.12+ on Linux (tested), a Gemini API key with access to a compatible Live model,
  and the normal Hermes build dependencies for desktop changes.
- For cross-network operation: a reachable TURN provider. This implementation supports
  Cloudflare Realtime TURN. Account activation/payment requirements and current charges
  must be checked with Cloudflare. Do not assume a subscription grants API access.
- `ffmpeg` for the optional audio probe. No personal audio fixture is distributed.

## Installation (manual, review first)

1. Back up your Hermes checkout/configuration. Inspect the patch and source snapshots.
   Do not patch a running managed install blindly.
2. In the matching Hermes checkout, run:

   ```sh
   git apply --check /path/to/hermes-gemini-live/patches/hermes-gemini-live.patch
   git apply /path/to/hermes-gemini-live/patches/hermes-gemini-live.patch
   ```

   If the check fails, stop; do not force it. Test/review a port to your Hermes revision.
3. In this repository, create a separate bridge environment:

   ```sh
   python3 -m venv .venv
   .venv/bin/python -m pip install -r requirements.txt
   .venv/bin/python -m unittest discover -s tests -v
   PYTHONPATH=integration .venv/bin/python -m unittest discover \
     -s integration/tests/tools -p test_voice_live_gemini.py -v
   ```

4. Enter credentials locally into a mode-600 environment file. Use
   `deploy/bridge.env.example` as a field-name reference, not a real secret.
   The bridge needs `GEMINI_API_KEY` (or `GOOGLE_API_KEY`); the Hermes gateway needs its
   own supported Gemini key configuration. Optional `CF_TURN_KEY_ID` and
   `CF_TURN_API_TOKEN` are used only by the bridge. Never commit these values.
5. Review `deploy/hermes-gemini-live.service`, set `WorkingDirectory` and `ExecStart`
   to the actual absolute repository/venv paths, then install it as a user service.
   Bind stays **127.0.0.1:8890**. Only Hermes' existing authenticated route should call
   `/offer`. Do not expose this process through a public tunnel.
6. Select the provider using Hermes' supported configuration command:

   ```sh
   hermes config set voice.gpt_live.provider gemini
   hermes config set voice.voice_chat_mode gpt-live
   ```

   This integration adds `voice.gpt_live.provider` in code; the pinned Hermes config
   schema may warn that it is unrecognized even while the patched runtime reads it.
   Confirm the actual status/session path instead of treating a saved value as proof.
   Default bridge URL: `http://127.0.0.1:8890`. Models/voice are configured under
   `voice.gpt_live.gemini`; inspect `integration/tools/voice_live.py` for the exact schema.
   The shipped historical model chain is not an availability guarantee: use models
   supported by your account and verify actual delegation, not just spoken output.
7. Rebuild the desktop using your pinned Hermes revision's own instructions. Apply/restart
   the gateway only in a maintenance window. Existing unpatched clients can use the
   WebRTC bridge if they implement the compatible Live/data-channel contract. No native
   phone binary or client modification is included here.

Read [operations and rollback](docs/OPERATIONS.md) before enabling this in production.
Official Hermes documentation: https://hermes-agent.nousresearch.com/docs/
Google Live API: https://ai.google.dev/gemini-api/docs/live
Cloudflare TURN: https://developers.cloudflare.com/realtime/turn/

## Testing the network path

Use only synthetic speech. The opt-in probe sends the supplied WAV to Google and uses
provider bandwidth. It returns a **synthetic stub** answer; it cannot establish that a
Hermes tool or memory call worked. Missing/empty fixtures fail instead of producing a
misleading silent run.

```sh
.venv/bin/python scripts/probe_webrtc.py synthetic.wav --duration 40
```

For stronger TURN evidence, start the test-only server in a separate process with the
required credentials in its environment and a spare loopback port:

```sh
LIVE_BRIDGE_PORT=8892 .venv/bin/python scripts/relay_only_test_server.py
# In a second terminal:
.venv/bin/python scripts/probe_webrtc.py synthetic.wav \
  --bridge http://127.0.0.1:8892 --relay-only --duration 170 --repeat-at 130 \
  --repeat-wav different-new-request.wav
```

The relay-only harness uses pinned aioice internals; never enable it inside the live
service. The probe requires a nominated pair containing a relay candidate, audio,
delegation, the expected stub phrase and a clean close. Close the test server afterwards.
A successful server-side test does not establish a physical phone's Tailscale-off path.

## Privacy and security

The Live provider receives microphone audio and any supplied conversation context.
Delegated results may also be spoken through that provider. TURN sees encrypted WebRTC
traffic plus connection metadata; the bridge terminates WebRTC and can access audio.
The bridge endpoint is unauthenticated by design and safe only behind its loopback trust
boundary; another local process can call it and consume quota. See [security](SECURITY.md).
No credentials, deployment logs, private hostnames, recordings or personal media ship here.

MIT licensed; see [LICENSE](LICENSE) and [provenance](THIRD_PARTY_NOTICES.md).
