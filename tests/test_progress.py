"""Offline tests for the async-delegation additions: progress tool, honest state report, duplicate guard.
No credentials, no network, no Hermes actions: in-memory sessions and synthetic requests only."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

SOURCE = Path(os.environ.get('BRIDGE_UNDER_TEST') or Path(__file__).resolve().parents[1] / 'bridge/live_bridge.py')
spec = importlib.util.spec_from_file_location('bridge_progress', SOURCE)
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)

BASE_SETUP = {'setup': {
    'model': 'models/gemini-3.8-live',
    'tools': [{'functionDeclarations': [{'name': 'delegate_to_hermes', 'description': 'x',
                                         'parameters': {'type': 'OBJECT', 'properties': {}}}]}],
    'systemInstruction': {'parts': [{'text': 'You are Penny.'}]},
}}


class Channel:
    readyState = 'open'
    def __init__(self): self.events = []
    def send(self, s): self.events.append(json.loads(s))


def call(cid, name, request=None):
    c = {'id': cid, 'name': name}
    if request is not None:
        c['args'] = {'request': request}
    return {'toolCall': {'functionCalls': [c]}}


class PrepareSetupTests(unittest.TestCase):
    def test_async_model_gets_tool_policy_and_behavior(self):
        out = b.prepare_setup(BASE_SETUP, 'gemini-3.8-live')['setup']
        names = {d['name']: d for d in out['tools'][0]['functionDeclarations']}
        self.assertIn('check_hermes_progress', names)
        self.assertEqual(names['delegate_to_hermes']['behavior'], 'NON_BLOCKING')
        self.assertEqual(names['check_hermes_progress']['behavior'], 'NON_BLOCKING')
        text = out['systemInstruction']['parts'][0]['text']
        self.assertTrue(text.startswith('You are Penny.'))
        self.assertIn('DELEGATION POLICY.', text)

    def test_extended_thinking_variant_also_async(self):
        out = b.prepare_setup(BASE_SETUP, 'gemini-3.8-live-extended-thinking')['setup']
        self.assertEqual(len(out['tools'][0]['functionDeclarations']), 2)

    def test_sync_only_model_is_left_alone(self):
        self.assertEqual(b.prepare_setup(BASE_SETUP, 'gemini-3.1-flash-live-preview'), BASE_SETUP)

    def test_idempotent_and_input_not_mutated(self):
        before = json.dumps(BASE_SETUP, sort_keys=True)
        once = b.prepare_setup(BASE_SETUP, 'gemini-3.8-live')
        twice = b.prepare_setup(once, 'gemini-3.8-live')
        self.assertEqual(json.dumps(BASE_SETUP, sort_keys=True), before)
        self.assertEqual(once, twice)

    def test_switch_off(self):
        with patch.object(b, 'PROGRESS_TOOL', False):
            self.assertEqual(b.prepare_setup(BASE_SETUP, 'gemini-3.8-live'), BASE_SETUP)

    def test_empty_setup_does_not_crash(self):
        out = b.prepare_setup({'setup': {}}, 'gemini-3.8-live')['setup']
        self.assertEqual(out['tools'][0]['functionDeclarations'][0]['name'], 'check_hermes_progress')


class ProgressTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.s = b.Session('synthetic-test-key', 'gemini-3.8-live', BASE_SETUP)
        self.s.channel = Channel()
        self.s.ws = SimpleNamespace(send=AsyncMock(), close=AsyncMock())
        b.SESSIONS.clear()

    def sent(self):
        return [json.loads(c.args[0]) for c in self.s.ws.send.await_args_list]

    def delegations(self):
        return [e for e in self.s.channel.events if e['type'] == 'session.delegation.created']

    async def test_report_when_nothing_started(self):
        await self.s.on_gemini(call('p1', 'check_hermes_progress'))
        r = self.sent()[0]['toolResponse']['functionResponses'][0]
        self.assertEqual(r['name'], 'check_hermes_progress')
        self.assertIn('No work has been handed to Hermes', r['response']['result'])
        self.assertEqual(r['response']['scheduling'], 'INTERRUPT')
        self.assertEqual(self.delegations(), [])   # a status check is never a new task

    async def test_running_task_is_reported_as_running_with_its_note(self):
        await self.s.on_gemini(call('c1', 'delegate_to_hermes', 'restart the web server'))
        await self.s.on_client(json.dumps({'type': 'session.thinking.append', 'delegation_id': 'c1',
                                           'content': 'Waiting for the health check.'}))
        await self.s.on_gemini(call('p1', 'check_hermes_progress'))
        text = self.sent()[-1]['toolResponse']['functionResponses'][0]['response']['result']
        self.assertIn('STILL RUNNING', text)
        self.assertIn('restart the web server', text)
        self.assertIn('Waiting for the health check.', text)
        self.assertIn('NOT reported a result', text)

    async def test_finished_task_reports_hermes_result_not_a_guess(self):
        await self.s.on_gemini(call('c1', 'delegate_to_hermes', 'restart the web server'))
        await self.s.respond('c1', 'The restart failed: port 80 is in use.')
        await self.s.on_gemini(call('p1', 'check_hermes_progress'))
        text = self.sent()[-1]['toolResponse']['functionResponses'][0]['response']['result']
        self.assertIn('FINISHED', text)
        self.assertIn('port 80 is in use', text)
        self.assertNotIn('STILL RUNNING', text)

    async def test_cancelled_task_is_reported_cancelled(self):
        await self.s.on_gemini(call('c1', 'delegate_to_hermes', 'restart the web server'))
        await self.s.on_gemini({'toolCallCancellation': {'ids': ['c1']}})
        await self.s.on_gemini(call('p1', 'check_hermes_progress'))
        self.assertIn('CANCELLED', self.sent()[-1]['toolResponse']['functionResponses'][0]['response']['result'])

    async def test_duplicate_request_while_running_is_not_dispatched_twice(self):
        await self.s.on_gemini(call('c1', 'delegate_to_hermes', 'Restart the web server.'))
        await self.s.on_gemini(call('c2', 'delegate_to_hermes', 'restart the web server'))
        self.assertEqual(len(self.delegations()), 1)
        reply = self.sent()[-1]['toolResponse']['functionResponses'][0]
        self.assertEqual(reply['id'], 'c2')
        self.assertIn('already running', reply['response']['result'])

    async def test_different_request_while_running_is_dispatched(self):
        await self.s.on_gemini(call('c1', 'delegate_to_hermes', 'restart the web server'))
        await self.s.on_gemini(call('c2', 'delegate_to_hermes', 'how much free disk space is there'))
        self.assertEqual(len(self.delegations()), 2)

    async def test_same_request_after_it_finished_is_a_new_task(self):
        await self.s.on_gemini(call('c1', 'delegate_to_hermes', 'restart the web server'))
        await self.s.respond('c1', 'done')
        await self.s.on_gemini(call('c2', 'delegate_to_hermes', 'restart the web server'))
        self.assertEqual(len(self.delegations()), 2)

    async def test_sync_model_session_has_no_progress_tool_handling(self):
        s = b.Session('k', 'gemini-3.1-flash-live-preview', BASE_SETUP)
        s.channel, s.ws = Channel(), SimpleNamespace(send=AsyncMock(), close=AsyncMock())
        await s.on_gemini(call('p1', 'check_hermes_progress'))
        reply = json.loads(s.ws.send.await_args.args[0])['toolResponse']['functionResponses'][0]['response']
        self.assertIn('error', reply)

    async def test_unknown_request_text_is_handled(self):
        await self.s.on_gemini(call('c1', 'delegate_to_hermes'))   # no args at all
        await self.s.on_gemini(call('p1', 'check_hermes_progress'))
        self.assertIn('request text unavailable', self.sent()[-1]['toolResponse']['functionResponses'][0]['response']['result'])
        self.assertEqual(len(self.delegations()), 1)


if __name__ == '__main__':
    unittest.main()
