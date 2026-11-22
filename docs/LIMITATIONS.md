# Known limitations

- Experimental, version-pinned patch. Source snapshots are not a complete Hermes checkout,
  installer, packaged desktop app or native phone application. User-agent detection
  distinguishes Electron from OpenAI-contract clients heuristically, not cryptographically.
- Default model names are historical tested configuration, not a guaranteed current catalog.
  Readiness/key presence does not prove provider entitlement, quota or model availability.
- Phone acceptance on November 18 was user-confirmed plus connected/clean-close log evidence;
  it did not instrument the selected ICE pair. No physical-phone Tailscale-off test is claimed.
- The bridge uses UDP TURN. TCP/TLS TURN is filtered out; UDP-blocking networks may fail.
  A phone offering only host candidates can still connect to a server relay. Host-only
  does NOT prove a client bug. VPN bypass and NAT explanations are hypotheses until measured.
- Bounded 170-second synthetic tests are not hours-long soak testing.
  Resume unit tests and controlled provider socket closure are not proof of recovery
  from every real network interruption.
- Barge-in clears queued bridge audio. Already buffered client audio may remain. Clearing
  audio/cancelling a model function does NOT prove an in-flight Hermes action was cancelled.
  Bridge close stops bridge tasks/socket/peer; action cancellation remains client/Hermes-owned.
- v0.1.1 de-duplicates provider call IDs in the bridge and the desktop hook, so a replayed call is not
  dispatched to Hermes twice (a completed call replayed after a resume is answered again with the SAME
  stored result, without new work). This stops duplicate dispatch of the same call ID; it is not a
  general exactly-once guarantee. Distinct calls are still distinct actions: keep Hermes approvals on and
  do not rely on voice for destructive actions without application-level idempotency.
- Provider cancellations are honoured on the bridge (no answer, no late speech for a cancelled call) but are
  NOT forwarded to the native phone app: its contract has no documented cancel event and an unknown event
  could crash a closed-source client. A running agent turn on that client is not stopped by a provider cancel.
  The patched desktop hook interrupts a running turn when voice ends, drops, unmounts or hears "stop"; this
  ships as a source patch and requires rebuilding the desktop app.
- Resumption races the provider: Google can reject an immediate re-dial with 1008 "already connected to an
  existing client". The bridge now retries with 0.5/1.5/3 s pauses; in testing the second attempt succeeded.
  In one of three forced-drop runs the second spoken request was not heard after the resume and the cause was
  not established; other runs heard both.
- TURN allocation is attempted once per call with a fixed 5-second gather window. In testing, two of roughly
  ten relay-only server starts failed to allocate (four sequential and five concurrent allocations in a
  separate check all succeeded). A call whose allocation fails advertises no relay candidate, which can fail
  for a cross-network phone; retrying the call normally works.
- Spoken readback of codes/IDs is not reliable evidence: one run's transcript spelled a token in words and
  dropped a character; a later run's audio was independently transcribed by local Whisper and matched exactly.
  Do not use voice to relay codes, amounts or other values that must be exact.
- Reply completion on the bridge is inferred from two seconds of silence (four seconds
  after progress). A slow tool may be spoken prematurely, or a late result handled as an update.
  Concurrent/late delegation ownership needs further hardening before broad production use.
- The bridge's model failure loop is driven by HTTP 502 responses. Desktop-only exhausted-chain
  fallback is not a guarantee that native phone apps automatically fall back to chained voice.
- A local synthetic probe uses a clearly labeled stub delegated answer. Earlier disk-space
  demo values were hard-coded test data, not verified server measurements. Real phone
  tool execution and memory retrieval remain separate acceptance tests.
- No independent security audit. Input JSON is lightly validated; there is a 2 MB HTTP body
  cap but no per-user rate limit/session quota. Loopback is a required trust boundary.
- Some provider exceptions are logged/returned. Do not collect verbose logs containing
  credentials or share raw session content. The private TURN file fallback is plaintext on
  disk, protected by filesystem permissions; provider billing remains the owner's responsibility.
- No Windows/macOS bridge qualification, clean-machine full Hermes install or complete
  upstream regression suite is claimed.

## Async delegation and progress tool (v0.2.0)

- The progress report only contains what the client sends. Hermes progress notes arrive as
  `session.thinking.append`; if the client sends none the report says "no progress note yet". The model
  can only relay what is recorded.
- Checking is a model decision. Measured against synthetic speech it checked on 21 of 21 status
  questions with the shipped wording, but a prompt rule cannot guarantee that for every phrasing or voice.
- The duplicate-request guard is fuzzy text matching (similarity >= 0.82) on the model's own rewording of
  a request. It can wrongly merge two genuinely different but similar requests while one is running, and can
  miss a duplicate that was reworded a lot. It is not an exactly-once guarantee for real actions.
- `gemini-3.8-live-extended-thinking` fails in this bridge ("a system error occurred", no delegation) both
  with and without these changes. It is not in the default chain; the cause was not established.
- Only `gemini-3.8-live*` models get the tool. The 2.5 native-audio models are documented as supporting
  async function calls but were left on the previous behaviour untested.
- The model still often speaks back to speech that is not addressed to it (see the turn-taking notes);
  the status work does not change that.
- The desktop WebSocket transport (core TypeScript) is unchanged and does not have the progress tool.

## NAT phone / relay hint (v0.2.1)

- Verified only with an emulated NATed client (private-only candidates, real public address unknown to the bridge) on the
  server itself: 0/4 connected without the hint, 3/3 with the correct public address, 0/2 with a valid but wrong public
  address. Not yet verified on a real phone on a real Wi-Fi.
- The hint must be the address the TURN relay sees for the phone's UDP. It comes from the HTTPS request. If the phone
  reaches the dashboard over IPv6 but its media goes out over IPv4 (or the reverse), or the network uses carrier-grade NAT
  that gives different addresses per protocol, the hint will not match and the call will still fail.
- A network that blocks outbound UDP to the relay entirely cannot be fixed by a hint. The `ice report` log line shows
  whether the phone's checks reached the bridge at all (`prflx` count 0 on a failed call).
- The dashboard edit touches Hermes core files (`audio.py`, `voice_live.py`); a Hermes update that rewrites those lines makes
  `apply_peerip_core.py` refuse, and the bridge simply stops receiving the hint (calls behave as before).
