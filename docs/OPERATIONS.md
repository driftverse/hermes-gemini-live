# Operations and rollback

## Start and verify

Review every path and install dependencies in a dedicated venv, not the Hermes runtime.
The example user unit intentionally requires editing two paths. After reviewing it:

```sh
install -m 644 deploy/hermes-gemini-live.service ~/.config/systemd/user/hermes-gemini-live.service
systemctl --user daemon-reload
systemctl --user enable --now hermes-gemini-live.service
curl --fail http://127.0.0.1:8890/health
```

`ok` is process health, `key` is key presence, `relay_configured` is TURN-key presence.
None proves a Google session or relay allocation. A connected call should show
`ice=completed` and `connection=connected`; a clean stop shows `close_requested`.
The bridge logs candidate counts/types without addresses. It does not log the selected
pair; the opt-in probe additionally inspects nominated-pair types.

A missing relay may fall back to direct connectivity. Do not report relay success from
`ice servers: cloudflare-relay` alone. TURN allocation typically adds about five seconds
to session creation on the tested host; timings vary by interfaces and network.

## Updates

Stop new voice calls before updating. Back up source/config and retain the previous
venv/build. Do not apply this patch over a later Hermes version without checking it.
This repo does not update production automatically. Gateway/desktop restart procedures
belong to the matching Hermes release and can interrupt active chats.

## Fast rollback

```sh
hermes config set voice.voice_chat_mode chained
```

Then verify the actual client has switched to the speech-to-text → agent → speech engine.
If no users still require the bridge, stop only its service:

```sh
systemctl --user disable --now hermes-gemini-live.service
```

To remove the source integration, check first:

```sh
git apply --reverse --check /path/to/hermes-gemini-live/patches/hermes-gemini-live.patch
```

Only reverse-apply after reviewing local modifications, backing up and stopping affected
apps. Rebuild the old desktop if replacing a patched binary. Never reset an entire
Hermes checkout to undo only this integration.

## Diagnostics

An SDP answer means signaling succeeded, not that audio connected. A `never_connected`
close occurs after the 30-second media-connection timeout. It prevents failed handshakes
from holding provider sessions open for hours. Network reconnection, mute and close are
separate from cancellation of actual agent work.

Avoid raw SDP/token logging. When reporting results include app version, transport path,
non-secret state transitions, probe mode and whether the answer was a stub or a real tool.
