"""Offline behavioral tests; no credentials, external services or Hermes actions."""
import asyncio
import base64
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

spec = importlib.util.spec_from_file_location('bridge', Path(__file__).resolve().parents[1] / 'bridge/live_bridge.py')
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)


class Channel:
    readyState = 'open'
    def __init__(self): self.events = []
    def send(self, s): self.events.append(json.loads(s))


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.s = b.Session('synthetic-test-key', 'test-model', {'setup': {}})
        self.s.channel = Channel()
        self.s.ws = SimpleNamespace(send=AsyncMock(), close=AsyncMock())
        b.SESSIONS.clear()

    async def test_delegation_then_result(self):
        await self.s.on_gemini({'toolCall': {'functionCalls': [{'id': 'c1', 'name': b.DELEGATE}]}})
        self.assertEqual(self.s.channel.events[-1]['type'], 'session.delegation.created')
        await self.s.on_client(json.dumps({'type':'session.commentary.append','delegation_id':'c1','content':'Synthetic result.'}))
        await self.s.respond('c1', ' '.join(self.s.pending['c1']['chunks']))
        msg = json.loads(self.s.ws.send.call_args.args[0])
        self.assertEqual(msg['toolResponse']['functionResponses'][0]['response']['result'], 'Synthetic result.')
        self.assertNotIn('c1', self.s.pending)

    async def test_cancel_removes_pending(self):
        self.s.pending['c1'] = {}
        await self.s.on_gemini({'toolCallCancellation': {'ids': ['c1']}})
        self.assertNotIn('c1', self.s.pending)

    async def test_barge_in_clears_pcm(self):
        self.s.out.buf.extend(b'1234')
        await self.s.on_gemini({'serverContent': {'interrupted': True}})
        self.assertEqual(self.s.out.buf, b'')

    async def test_audio_pcm_decodes(self):
        pcm = b'\x01\x10' * 2400
        await self.s.on_gemini({'serverContent': {'modelTurn': {'parts': [{'inlineData': {'data': base64.b64encode(pcm).decode()}}]}}})
        self.assertGreater(len(self.s.out.buf), 0)
        f = await self.s.out.recv()
        self.assertEqual(f.sample_rate, 48000)
        self.assertEqual(f.samples, 960)

    async def test_mute_and_unmute(self):
        await self.s.on_client('{"type":"session.input_audio.mute"}')
        self.assertTrue(self.s.muted)
        self.assertEqual(json.loads(self.s.ws.send.call_args.args[0]), {'realtimeInput': {'audioStreamEnd': True}})
        await self.s.on_client('{"type":"session.input_audio.unmute"}')
        self.assertFalse(self.s.muted)

    async def test_transcripts(self):
        await self.s.on_gemini({'serverContent': {'inputTranscription': {'text':'synthetic question'}, 'outputTranscription': {'text':'synthetic answer'}}})
        self.assertEqual([x['type'] for x in self.s.channel.events], ['session.input_transcript.delta', 'session.output_transcript.delta'])

    async def test_unknown_function_returns_error(self):
        await self.s.on_gemini({'toolCall': {'functionCalls': [{'id':'u1','name':'not_supported'}]}})
        self.assertIn('error',json.loads(self.s.ws.send.call_args.args[0])['toolResponse']['functionResponses'][0]['response'])

    async def test_close_releases_peer_socket_and_tasks(self):
        self.s.pc = SimpleNamespace(close=AsyncMock())
        task = asyncio.create_task(asyncio.sleep(60))
        self.s.tasks.append(task)
        b.SESSIONS[self.s.id] = self.s
        await self.s.close('close_requested')
        self.assertTrue(self.s.closed)
        self.assertNotIn(self.s.id, b.SESSIONS)
        self.s.ws.close.assert_awaited_once()
        self.s.pc.close.assert_awaited_once()
        self.assertTrue(task.cancelled())
        self.assertEqual(self.s.channel.events[-1]['reason'], 'close_requested')
        await self.s.close('again')
        self.s.ws.close.assert_awaited_once()

    async def test_never_connected_times_out(self):
        self.s.pc = SimpleNamespace(connectionState='connecting', iceConnectionState='checking')
        self.s.close = AsyncMock()
        await self.s.connect_timeout(0)
        self.s.close.assert_awaited_once_with('never_connected')

    async def test_connected_survives_timeout(self):
        self.s.pc = SimpleNamespace(connectionState='connected')
        self.s.close = AsyncMock()
        await self.s.connect_timeout(0)
        self.s.close.assert_not_awaited()

    async def test_resume_handle_and_goaway(self):
        await self.s.on_gemini({'sessionResumptionUpdate': {'newHandle':'synthetic-handle','resumable':True}})
        self.assertEqual(self.s.resume, 'synthetic-handle')
        await self.s.on_gemini({'goAway': {'timeLeft':'1s'}})
        self.s.ws.close.assert_awaited_once()

    async def test_dial_setup_with_resume(self):
        ws = SimpleNamespace(send=AsyncMock(),recv=AsyncMock(return_value='{"setupComplete":{}}'),close=AsyncMock())
        with patch.object(b.websockets,'connect',AsyncMock(return_value=ws)):
            await self.s.dial('synthetic-handle')
        self.assertEqual(json.loads(ws.send.call_args.args[0])['setup']['sessionResumption']['handle'], 'synthetic-handle')

    async def test_setup_rejected_closes_socket(self):
        ws = SimpleNamespace(send=AsyncMock(),recv=AsyncMock(return_value='{"error":{}}'),close=AsyncMock())
        with patch.object(b.websockets,'connect',AsyncMock(return_value=ws)):
            with self.assertRaises(RuntimeError): await self.s.dial()
        ws.close.assert_awaited_once()

    async def test_broken_socket_without_resume_closes(self):
        class Socket:
            def __aiter__(self): return self
            async def __anext__(self): raise StopAsyncIteration
        self.s.ws = Socket()
        self.s.close = AsyncMock()
        await self.s.gemini_reader()
        self.s.close.assert_awaited_once_with('connection_lost')

    async def test_socket_resume_then_close(self):
        class Socket:
            def __aiter__(self): return self
            async def __anext__(self): raise StopAsyncIteration
        self.s.ws = Socket()
        self.s.resume = 'synthetic-handle'
        async def dial(handle): self.s.closed = True
        self.s.dial = AsyncMock(side_effect=dial)
        await self.s.gemini_reader()
        self.s.dial.assert_awaited_once_with('synthetic-handle')

    async def test_ice_cache(self):
        with patch.dict(b._ice_cache, {'at':b.time.time(),'servers':[{'urls':['turn:example.invalid']}]}), patch.object(b,'_fetch_ice') as fetch:
            self.assertTrue(await b.ice_servers())
            fetch.assert_not_called()

    async def test_ice_failure_falls_back(self):
        with patch.dict(b._ice_cache, {'at':0,'servers':[]}), patch.object(b,'_fetch_ice',side_effect=OSError('synthetic')):
            self.assertEqual(await b.ice_servers(), [])

    async def test_bad_json_ignored(self):
        await self.s.on_client('not json')
        self.s.ws.send.assert_not_awaited()

    def test_candidate_log_has_no_address(self):
        s = 'a=candidate:1 1 UDP 123 192.0.2.1 234 typ relay\r\na=candidate:2 1 UDP 23 192.0.2.2 456 typ host\r\n'
        self.assertEqual(b._cand_summary(s), 'host/udpx1, relay/udpx1')
        self.assertNotIn('192.0.2', b._cand_summary(s))

    def test_turn_headers_and_filter(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps({'iceServers':[{'urls':['stun:example.invalid','turn:example.invalid?transport=udp','turn:example.invalid?transport=tcp','turns:example.invalid'],'username':'synthetic','credential':'synthetic'}]}).encode()
        with patch.object(b,'_turn_secrets',return_value=('synthetic-id','synthetic-token')), patch.object(b.urllib.request,'urlopen',return_value=response) as call:
            servers = b._fetch_ice()
        self.assertEqual(len(servers[0]['urls']), 2)
        self.assertEqual(call.call_args.args[0].get_header('User-agent'), 'live-bridge/1.0')


if __name__ == '__main__': unittest.main()
