# Security boundary

This is an experimental loopback service, not a multi-tenant public API. Never expose
its /offer endpoint directly to the internet or LAN. The existing Hermes gateway
must provide authentication, owner/profile scoping and approvals. A local malicious
process can request billed sessions. Do not weaken Hermes action approvals for voice.

Keep Google and TURN credentials in mode-600 local files or a supported secret manager.
Do not paste them into issues, logs or chat. Rotate credentials accidentally disclosed;
removing them from a later commit does not erase Git history or prior copies.

Google receives Live audio, context and returned delegated results. The bridge decodes
audio locally. Cloudflare TURN is a transport relay, not the voice reasoning provider.
No automatic media recording is enabled by this repository.

Credential failures must not be interpreted as successful direct/relay operation. Health
reports configuration presence, not an end-to-end TURN allocation or working phone.
Treat action cancellation separately from voice playback interruption.

Before reporting a vulnerability, remove secrets, personal transcripts, SDP addresses,
resume handles and ephemeral token URLs. Use a private channel when available; do not
open a public issue containing exploit credentials or personal data. No response-time
or support guarantee is made.

Publication review covered the allowlisted source, patch, tests and documentation.
It is a first-party review, not an independent penetration test or legal assurance.
