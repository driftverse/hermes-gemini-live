"""Offline tests for the NAT-phone fix: the public-address hint and the ICE diagnostics line.
No credentials, no network, no Hermes actions."""
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import unittest

SOURCE = Path(os.environ.get('BRIDGE_UNDER_TEST') or Path(__file__).resolve().parents[1] / 'bridge/live_bridge.py')
spec = importlib.util.spec_from_file_location('bridge_peer', SOURCE)
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)

# A phone-like offer: two media sections (audio + data channel), private host candidates only, no srflx.
OFFER = "\r\n".join([
    "v=0", "o=- 1 2 IN IP4 127.0.0.1", "s=-", "t=0 0", "a=group:BUNDLE 0 1",
    "m=audio 9 UDP/TLS/RTP/SAVPF 111", "c=IN IP4 0.0.0.0", "a=mid:0",
    "a=ice-ufrag:abcd", "a=ice-pwd:0123456789012345678901",
    "a=candidate:1 1 udp 2122260223 192.168.1.20 40000 typ host",
    "a=candidate:2 1 udp 2122194687 10.0.0.9 40001 typ host",
    "a=fingerprint:sha-256 " + ":".join(["AA"] * 32), "a=setup:actpass", "a=rtcp-mux", "a=rtpmap:111 opus/48000/2", "a=sendrecv",
    "m=application 9 UDP/DTLS/SCTP webrtc-datachannel", "c=IN IP4 0.0.0.0", "a=mid:1",
    "a=ice-ufrag:abcd", "a=ice-pwd:0123456789012345678901",
    "a=fingerprint:sha-256 " + ":".join(["AA"] * 32), "a=setup:actpass", "a=sctp-port:5000", ""])


class PeerHintTests(unittest.TestCase):
    def test_public_ipv4_is_added_once_per_media_section(self):
        out = b.peer_hint_sdp(OFFER, "8.8.4.4")
        self.assertEqual(out.count("a=candidate:peerhint 1 udp 1 8.8.4.4 9 typ srflx raddr 0.0.0.0 rport 0"), 2)
        for original in ("192.168.1.20 40000", "10.0.0.9 40001"):
            self.assertIn(original, out)            # real candidates untouched
        self.assertTrue(out.endswith("\r\n"))

    def test_public_ipv6_is_added(self):
        out = b.peer_hint_sdp(OFFER, "2001:4860:4860::8888")
        self.assertIn("2001:4860:4860::8888 9 typ srflx raddr :: rport 0", out)

    def test_not_public_or_invalid_is_ignored(self):
        for bad in ("", "   ", "not-an-ip", "192.168.1.20", "10.1.2.3", "172.16.0.5", "127.0.0.1", "169.254.1.1",
                    "100.64.0.1", "0.0.0.0", "224.0.0.1", "203.0.113.9", "::1", "fe80::1", "8.8.8.8/32", "8.8.8.8; a=evil"):
            self.assertEqual(b.peer_hint_sdp(OFFER, bad), OFFER, bad)

    def test_idempotent(self):
        once = b.peer_hint_sdp(OFFER, "8.8.4.4")
        self.assertEqual(b.peer_hint_sdp(once, "8.8.4.4"), once)

    def test_aiortc_parses_the_hinted_offer(self):
        from aiortc.sdp import SessionDescription
        hinted = SessionDescription.parse(b.peer_hint_sdp(OFFER, "8.8.4.4"))
        found = [c for m in hinted.media for c in m.ice_candidates if c.ip == "8.8.4.4"]
        self.assertTrue(found)
        self.assertEqual(found[0].type, "srflx")
        plain = SessionDescription.parse(OFFER)
        self.assertEqual(sum(len(m.ice_candidates) for m in plain.media), 2)   # unchanged original


class IceReportTests(unittest.TestCase):
    def test_no_transport_does_not_raise(self):
        self.assertIn("unavailable", b.ice_report(SimpleNamespace(sctp=None)))

    def test_counts_types_and_states_without_addresses(self):
        cand = lambda t: SimpleNamespace(type=t, host="203.0.113.50")  # noqa: E731
        conn = SimpleNamespace(
            _check_list=[SimpleNamespace(state="CandidatePair.State.FAILED"), SimpleNamespace(state="State.IN_PROGRESS")],
            remote_candidates=[cand("host"), cand("prflx")],
            _nominated={1: SimpleNamespace(local_candidate=cand("relay"), remote_candidate=cand("prflx"))})
        pc = SimpleNamespace(sctp=SimpleNamespace(transport=SimpleNamespace(transport=SimpleNamespace(_connection=conn))))
        line = b.ice_report(pc)
        self.assertIn("prflx) 1", line)
        self.assertIn("relay->prflx", line)
        self.assertNotIn("203.0.113.50", line)


if __name__ == "__main__":
    unittest.main()
