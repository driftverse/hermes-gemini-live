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
- Resume qualification observed three delegation events for two synthetic utterances.
  The cause is not established; do not assume exactly-once execution or allow destructive
  actions without normal Hermes approval and application-level idempotency.
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
