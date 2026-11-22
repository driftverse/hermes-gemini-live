"""Test the peer-address edit in a Hermes tree. Usage: <hermes python> test_peerip_core.py <tree_with_patched_files>

Loads hermes_cli/web_routers/audio.py and tools/voice_live.py from <tree> (a scratch copy or the real checkout),
resolving every other import from the real Hermes checkout. Synthetic requests and a fake local bridge only.
"""
import importlib.util
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

TREE = Path(sys.argv.pop(1)).resolve()
REAL = Path.home() / '.hermes/hermes-agent'
sys.path.insert(0, str(REAL))


def load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, TREE / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


audio = load('audio_under_test', 'hermes_cli/web_routers/audio.py')
vl = load('voice_live_under_test', 'tools/voice_live.py')


def req(header=None, host=None):
    headers = {'cf-connecting-ip': header} if header is not None else {}
    return SimpleNamespace(headers=SimpleNamespace(get=lambda k, d=None: headers.get(k.lower(), d)),
                           client=SimpleNamespace(host=host) if host is not None else None)


class CallerIpTests(unittest.TestCase):
    def test_cloudflare_header_public_ipv4(self):
        self.assertEqual(audio._caller_public_ip(req('8.8.4.4', '127.0.0.1')), '8.8.4.4')

    def test_public_ipv6(self):
        self.assertEqual(audio._caller_public_ip(req('2001:4860:4860::8888', '127.0.0.1')), '2001:4860:4860::8888')

    def test_falls_back_to_rewritten_client_address(self):
        self.assertEqual(audio._caller_public_ip(req(None, '9.9.9.9')), '9.9.9.9')

    def test_private_or_loopback_or_tailscale_gives_nothing(self):
        for host in ('127.0.0.1', '::1', '10.0.0.5', '192.168.1.9', '169.254.1.1'):
            self.assertEqual(audio._caller_public_ip(req(None, host)), '', host)

    def test_garbage_header_falls_through_to_client(self):
        self.assertEqual(audio._caller_public_ip(req('not-an-ip', '9.9.9.9')), '9.9.9.9')
        self.assertEqual(audio._caller_public_ip(req('8.8.8.8, 1.1.1.1', '127.0.0.1')), '')

    def test_no_client_no_header(self):
        self.assertEqual(audio._caller_public_ip(req(None, None)), '')


class Fake(BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):
        Fake.seen.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
        out = json.dumps({'session': {'id': 'gemlive_x'}, 'sdp': 'answer-sdp'}).encode()
        self.send_response(200); self.send_header('Content-Length', str(len(out))); self.end_headers(); self.wfile.write(out)

    def log_message(self, *a):
        pass


class ForwardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(('127.0.0.1', 0), Fake)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        url = f'http://127.0.0.1:{cls.srv.server_port}'
        vl._live_section = lambda *a, **k: {}
        vl._gemini_section = lambda *a, **k: {'bridge_url': url, 'models': ['m1'], 'api_key': 'synthetic'}
        vl._resolve_gemini_key = lambda gem: 'synthetic'
        vl.build_gemini_setup = lambda model, history=None, resume=None: {'setup': {'model': model}}

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        Fake.seen.clear()

    def test_hint_is_forwarded(self):
        out = vl.create_gemini_bridge_session('offer', None, '8.8.4.4')
        self.assertEqual(Fake.seen[0]['peer_ip'], '8.8.4.4')
        self.assertEqual(Fake.seen[0]['sdp'], 'offer')
        self.assertEqual(out['transport']['sdp'], 'answer-sdp')

    def test_without_hint_the_request_is_unchanged(self):
        vl.create_gemini_bridge_session('offer', None)          # the old two-argument call
        self.assertNotIn('peer_ip', Fake.seen[0])
        self.assertEqual(set(Fake.seen[0]), {'sdp', 'model', 'setup'})


if __name__ == '__main__':
    unittest.main()
