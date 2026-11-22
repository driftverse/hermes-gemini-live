# Verification record — 2026-11-18

This is an experimental integration with bounded evidence, not a production certification.

## Offline checks (copied publication artifact)

| Check | Result | Scope |
|---|---|---|
| Bridge Python tests | 20 passed, 0 failed | PCM, transcripts, barge-in queue clear, delegation mapping, mute, close cleanup, timeout, resume logic, TURN cache/filtering |
| Provider Python tests | 16 passed, 0 failed | Configured chain, cooldown, setup, missing keys, history and provider selection |
| Desktop renderer tests | 9 passed, 0 failed | Mock WebSocket/AudioContext, failover, delegation, cancellation, resume, playback interruption |
| Renderer TypeScript | Passed | Reconstructed pinned Hermes tree; existing installed dependencies reused |
| Touched renderer ESLint | Passed | Gemini transport/tests, transport wrapper and voice hooks |
| Integration patch | Passed | `git apply --check`, actual apply to pristine allowlisted base files, reconstructed bytes match snapshots |
| Production source hashes | Unchanged | All 11 allowlisted source files compared against initial snapshot |
| Publication scan | Passed | Known secrets from two local credential files, private deployment identifiers, common token/private-key patterns; no matching values printed |

The first TypeScript attempt lacked the shared workspace dependency link in the isolated
checkout. Adding the matching dependency link fixed it; the final check exited zero.
Tests used a reconstructed source tree, not a modified running Hermes installation.

## Real native-phone acceptance

User reported successful voice operation. Corroborating server events on November 18:
offer received 02:38:30 CST, ICE completed 02:38:36, connected 02:38:37, clean
`close_requested` at 02:38:50. Model: `gemini-3.8-live`.
The offer contained host candidates only; the answer included relay candidates. The
selected pair was not logged in that call. This is NOT proof that the phone used TURN,
that Tailscale was off, or that real tools/memory were exercised.

## Real-provider synthetic probes

These send only locally synthesized test speech to Google. The delegated answer is an
explicit synthetic stub, not a Hermes agent response and not an actual disk measurement.
Earlier disk-space demos used hard-coded numbers; they must not be cited as real tool evidence.

A forced-relay server disables non-relay aioice protocols in a separate process. It is
not the production service. A 155-second run selected **host → relay**, returned real
Gemini audio, stayed connected and closed cleanly. Its overall strict probe verdict was
**FAIL**: the identical repeated question did not trigger a second delegation. The
configured persona allows repeating prior answers without another call; transport
success must not be relabeled as a two-delegation pass.

A follow-up launch collided with the still-running temporary test listener; it is not
counted as a valid test. A fresh-port rerun used a distinct second request and **passed**:
170 seconds, two delegations, 158,400 non-silent decoded audio samples, a nominated
**host → relay** pair, connected state retained and clean close. This proves the server
can operate through TURN without a direct LAN/Tailscale media path; it does not establish
the selected route of the user's physical-phone call.

A second 170-second real-provider run forced the Gemini socket closed at 40 seconds.
The bridge logged a valid resume handle, resumed successfully, returned audio after the
later new request, retained the **host → relay** media path and closed cleanly. It
emitted three delegation events for two spoken utterances; whether this was replay or
an extra provider call is not established. Exactly-once agent action execution is NOT
proven and destructive-action testing was deliberately excluded. Full structured
results, including earlier failed probes, are in `results.json`.

## Not established

- Physical phone with Tailscale switched off on a separate network.
- Physical microphone interruption quality, echo and cancellation of actual agent work.
- Real phone tool and cross-session memory round trips.
- Hours-long soak, real abrupt-network recovery or all-model provider availability.
- Full upstream Hermes regression suite or clean-machine desktop/mobile installation.

No independent reviewer or penetration test was performed. Publishing source under MIT
is not a support, reliability or security guarantee. CI runs only offline checks; no
production credentials are uploaded to GitHub Actions.

## v0.1.1 qualification — 2026-11-20

Findings from adversarial testing of v0.1.0, then fixes, each checked red before green.

**Found in v0.1.0 (reproduced by tests):** a replayed call ID was dispatched twice (also after completion);
a duplicate wiped a partial reply and caused a second provider answer; a cancelled call's late reply could
still be spoken; in the desktop hook, a replayed call ID was submitted twice and ending voice, losing the
connection, unmounting or saying "stop" did not interrupt a running turn (hook-level mocks; other parts of
the app may add their own cancellation).

**Fixed in v0.1.1:** call-ID de-duplication, single answer per call, cancel suppression (bridge); call-ID
de-duplication and turn interruption on end/close/unmount/stop (desktop hook); resume retry with backoff
after observing Google's 1008 "already connected" rejection (bridge).

| Check | Result |
|---|---|
| Bridge unit tests | 22 passed (20 existing + 2 resume-retry) |
| Adversarial bridge gates | 10 passed (6 failed on v0.1.0) |
| Renderer tests | 17 passed (9 existing + 8 safety); the 5 call-ID/interrupt tests failed before the hook fix |
| TypeScript / ESLint on touched renderer files | passed |
| Patch | regenerated; `git apply --check` and byte reconstruction passed |
| Real provider, forced TURN, no drop, 170 s | 2 requests heard, exactly 2 delegations, host→relay, clean close |
| Real provider, forced TURN, provider socket dropped at 40 s | resumed on attempt 2 after a 1008 rejection; both requests heard; exactly 2 delegations (v0.1.0: 3); clean close |
| Real Hermes tool through voice (synthetic speech, real agent run, canary file) | tool ran and returned the exact token; audio independently transcribed by local Whisper matched exactly |

One additional forced-drop run did not hear the second request after resuming (unexplained, not
reproduced) and two relay-only test starts failed TURN allocation (see LIMITATIONS). These runs are
kept in the evidence directory, not discarded.

Still not established: physical phone with Tailscale off, real phone tool/memory requests, hours-long calls
and genuine network changes, long-term-memory recall through voice (a headless session had no recall tool and
correctly declined), and forwarding of provider cancels to the native client.

## v0.2.0 async delegation and progress tool - 2026-11-22

Method: synthetic speech to an isolated copy of the bridge using the real `gemini-3.8-live`; a scripted
slow "Hermes" (22-40 s) answers delegations and sends progress notes; questions are asked while it is still
working and after it finished. A status question counts as checked when the bridge saw a progress check (or a
delegation) within 7 s. Sessions that failed to start (HTTP 502 at 12 concurrent sessions) are excluded.

| Situation | Previous bridge | New bridge, tool + policy | Shipped wording |
|---|---|---|---|
| Hermes still working | 6/24 checked | 24/24 | 9/9 |
| Hermes finished | 4/28 checked | 20/32 | 12/12 |

"Answered done while still running": 0 in every variant. Answers matched the recorded state (for example
"still restarting, waiting on the health check"). A production-path round trip after deployment gave a truthful
mid-task answer and spoke the result when it arrived.

Regression with the shipped wording (3 repeats): factual 3/3, server question 3/3, question without the name
3/3, follow-up 3/3, small talk (no delegation) 3/3, status scenarios 9/9 valid sessions; the older "stay silent
on unaddressed speech" scenarios were unchanged and weak (side talk 1/3, TV 0/3). 47 offline tests pass (32 existing
+ 15 new; 12 of the 15 fail against the previous bridge). `gemini-3.1-flash-live-preview` (sync-only) 3/3 unchanged.

Not established: a real phone call (synthetic speech only), real Hermes timing and progress notes, long calls with
several concurrent tasks, the duplicate guard on real wording, and behaviour with other voices or noisy rooms.

## v0.2.1 NAT phone relay hint - 2026-11-22

Reproduction first: a phone-like offer (two private host candidates, no public address) against a relay-only bridge
produced the same server log as the real phone: `offer candidates: host/udp x2`, ICE `checking`, `never_connected` after
30 s. Then the fix, tested with the same client:

| Case (relay-only, synthetic speech) | Result |
|---|---|
| Control: real client candidates (include public address) | connected 3/3 valid runs (1 run excluded: known intermittent TURN allocation failure in the test server) |
| NATed client, no hint | 0/4 connected |
| NATed client, correct public address hint | 3/3 connected, audio both ways, packets learned as peer-reflexive |
| NATed client, valid but wrong public address | 0/2 connected |

A reserved documentation address given as a "wrong" hint is ignored by the bridge by design, so that run is not counted.
54 offline bridge tests pass (7 new: hint insertion, rejection of private/loopback/CGNAT/invalid input, idempotence, aiortc
parses the hinted offer, ICE report without addresses; all 7 fail on the previous bridge). The Hermes-side edit has 8 tests
(address selection and forwarding to a fake bridge); 7 fail on an unpatched tree. The 16 existing Gemini provider tests
still pass.

Not established: a real phone on a real Wi-Fi with the VPN off (see LIMITATIONS).
