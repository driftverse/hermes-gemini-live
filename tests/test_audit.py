"""Offline tests for the delegation backstop: when the model answers a substantive request itself, the bridge
starts the delegation, and the result goes back as text (never as a toolResponse for a call that does not exist).
No credentials, no network, no Hermes actions."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

SOURCE = Path(os.environ.get('BRIDGE_UNDER_TEST') or Path(__file__).resolve().parents[1] / 'bridge/live_bridge.py')
spec = importlib.util.spec_from_file_location('bridge_audit', SOURCE)
assert spec is not None and spec.loader is not None
assert spec and spec.loader
b = importlib.util.module_from_spec(spec)
assert spec is not None and spec.loader is not None
spec.loader.exec_module(b)
b.AUDIT = True   # the backstop is opt-in (LIVE_BRIDGE_AUDIT=1); these tests exercise it switched on

SETUP = {'setup': {'model': 'models/gemini-3.8-live', 'tools': [{'functionDeclarations': []}], 'systemInstruction': {'parts': [{'text': 'x'}]}}}


class Channel:
    readyState = 'open'
    def __init__(self): self.events = []
    def send(self, s): self.events.append(json.loads(s))


def session():
    s = b.Session('synthetic', 'gemini-3.8-live', SETUP)
    s.channel = Channel()
    s.gsend = AsyncMock()
    return s


def run(coro):
    return asyncio.run(coro)


def turn(s, heard, said, call=False):
    async def go():
        await s.on_gemini({'serverContent': {'inputTranscription': {'text': heard}}})
        if call:
            await s.on_gemini({'toolCall': {'functionCalls': [{'id': 'c1', 'name': 'delegate_to_hermes', 'args': {'request': heard}}]}})
        if said:
            await s.on_gemini({'serverContent': {'outputTranscription': {'text': said}}})
        await s.on_gemini({'serverContent': {'turnComplete': True}})
    run(go())


def created(s):
    return [e for e in s.channel.events if e.get('type') == 'session.delegation.created']


class NeedsCheckTests(unittest.TestCase):
    def test_chatter_is_not_checked(self):
        for heard in ('Hi Penny!', 'Thanks, bye!', 'Okay thank you', "What's up?", 'Good morning Penny', 'yes please', 'ok cool thanks',
                      'Hey, how are you?', 'Are you there', 'no worries'):
            self.assertFalse(b.Session.needs_check(heard, 'Sure.'), heard)

    def test_substantive_requests_are_checked(self):
        for heard in ('What is the capital of France?', 'Is the backup finished yet?', 'How much disk space is free?',
                      'Restart the web server', 'Did that work?', 'Thanks, now what is the server status?'):
            self.assertTrue(b.Session.needs_check(heard, 'Sure.'), heard)

    def test_short_or_silent_is_not_checked(self):
        self.assertFalse(b.Session.needs_check('Restart it', 'Sure.'))       # two words
        self.assertFalse(b.Session.needs_check('What is the disk space', ''))  # the model said nothing


class BackstopTests(unittest.TestCase):
    def test_model_answered_itself_starts_a_delegation(self):
        s = session()
        turn(s, 'Is the backup finished yet?', 'Yes it finished an hour ago.')
        ev = created(s)
        self.assertEqual(len(ev), 1)
        cid = ev[0]['delegation']['id']
        self.assertTrue(cid.startswith('audit_'))
        self.assertIn(cid, s.pending)
        self.assertEqual(s.work[cid]['request'], 'Is the backup finished yet?')
        self.assertTrue(s.work[cid]['audit'])

    def test_no_backstop_when_the_model_delegated(self):
        s = session()
        turn(s, 'Is the backup finished yet?', 'One moment.', call=True)
        self.assertEqual([e['delegation']['id'] for e in created(s)], ['c1'])
        self.assertFalse(any(k.startswith('audit_') for k in s.work))

    def test_no_backstop_for_chatter(self):
        s = session()
        turn(s, 'Thanks Penny, bye!', "You're welcome.")
        self.assertEqual(created(s), [])

    def test_no_backstop_when_model_said_nothing(self):
        s = session()
        turn(s, 'Somebody else was talking about the backup here', '')
        self.assertEqual(created(s), [])

    def test_turn_state_resets(self):
        s = session()
        turn(s, 'Is the backup finished yet?', 'Yes.')
        self.assertEqual((s.turn_in, s.turn_out, s.turn_called), ('', '', False))
        turn(s, 'Thanks!', 'Welcome.')
        self.assertEqual(len(created(s)), 1)

    def test_running_twin_is_not_started_again(self):
        s = session()
        run(s.on_gemini({'toolCall': {'functionCalls': [{'id': 'c1', 'name': 'delegate_to_hermes', 'args': {'request': 'restart the web server'}}]}}))
        s.turn_called = False
        turn(s, 'restart the web server', 'Okay.')
        self.assertFalse(any(k.startswith('audit_') for k in s.work))

    def test_cap_per_session(self):
        s = session()
        s.audits = b.AUDIT_MAX_PER_SESSION
        turn(s, 'Is the backup finished yet?', 'Yes.')
        self.assertEqual(created(s), [])

    def test_switch_off(self):
        s = session()
        with patch.object(b, 'AUDIT', False):
            turn(s, 'Is the backup finished yet?', 'Yes.')
        self.assertEqual(created(s), [])

    def test_interrupted_turn_drops_what_the_model_had_said(self):
        s = session()
        async def go():
            await s.on_gemini({'serverContent': {'inputTranscription': {'text': 'Is the backup finished yet?'}}})
            await s.on_gemini({'serverContent': {'outputTranscription': {'text': 'Yes it'}}})
            await s.on_gemini({'serverContent': {'interrupted': True}})
            await s.on_gemini({'serverContent': {'turnComplete': True}})
        run(go())
        self.assertEqual(created(s), [])


class ResultPathTests(unittest.TestCase):
    def test_result_is_text_never_a_tool_response(self):
        s = session()
        turn(s, 'Is the backup finished yet?', 'Yes it finished an hour ago.')
        cid = created(s)[0]['delegation']['id']
        run(s.on_client(json.dumps({'type': 'session.commentary.append', 'delegation_id': cid, 'content': 'The backup failed at 03:10.'})))
        s.pending[cid]['last'] = 0.0
        run(s.respond(cid, 'The backup failed at 03:10.'))
        sent = [c.args[0] for c in s.gsend.await_args_list]
        self.assertFalse(any('toolResponse' in m for m in sent))
        texts = [m['realtimeInput']['text'] for m in sent if 'realtimeInput' in m]
        self.assertEqual(len(texts), 1)
        self.assertIn('The backup failed at 03:10.', texts[0])
        self.assertIn('correct yourself', texts[0])

    def test_real_delegation_still_uses_tool_response(self):
        s = session()
        run(s.on_gemini({'toolCall': {'functionCalls': [{'id': 'c9', 'name': 'delegate_to_hermes', 'args': {'request': 'restart it now please'}}]}}))
        run(s.respond('c9', 'done'))
        sent = [c.args[0] for c in s.gsend.await_args_list]
        self.assertTrue(any('toolResponse' in m for m in sent))

    def test_progress_report_includes_backstop_work(self):
        s = session()
        turn(s, 'Is the backup finished yet?', 'Yes.')
        self.assertIn('STILL RUNNING', s.progress_report())



class DefaultTests(unittest.TestCase):
    def test_off_unless_enabled(self):
        spec2 = importlib.util.spec_from_file_location('bridge_audit_default', SOURCE)
        assert spec2 is not None and spec2.loader is not None
        fresh = importlib.util.module_from_spec(spec2)
        os.environ.pop('LIVE_BRIDGE_AUDIT', None)
        spec2.loader.exec_module(fresh)
        self.assertFalse(fresh.AUDIT)


if __name__ == '__main__':
    unittest.main()
