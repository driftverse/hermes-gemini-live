"""Action-safety regression gates for the bridge: replay, duplicate, cancel and cleanup behavior.
All actions are in-memory counters; nothing external is touched.
"""
import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import unittest

SOURCE=Path(__file__).resolve().parents[1]/'bridge/live_bridge.py'
spec=importlib.util.spec_from_file_location('qualified_bridge', SOURCE)
b=importlib.util.module_from_spec(spec); spec.loader.exec_module(b)

class Channel:
    readyState='open'
    def __init__(self): self.events=[]
    def send(self,raw): self.events.append(json.loads(raw))

class SafetyGates(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.s=b.Session('synthetic-key','synthetic-model',{'setup':{}})
        self.s.channel=Channel()
        self.s.ws=SimpleNamespace(send=AsyncMock(),close=AsyncMock())
    async def call(self,cid='same-call'):
        await self.s.on_gemini({'toolCall':{'functionCalls':[{'id':cid,'name':b.DELEGATE,'args':{'request':'Count one harmless test action.'}}]}})
    def submissions(self): return [e for e in self.s.channel.events if e['type']=='session.delegation.created']

    async def test_active_call_replay_submits_once(self):
        await self.call(); await self.call()
        self.assertEqual(len(self.submissions()),1,'Repeated provider call ID would be delivered to the client twice')
    async def test_completed_call_replay_submits_once(self):
        await self.call(); await self.s.respond('same-call','Harmless counter incremented.')
        await self.call()
        self.assertEqual(len(self.submissions()),1,'Completed call ID is dispatched again')
    async def test_duplicate_does_not_erase_partial_reply(self):
        await self.call()
        await self.s.on_client(json.dumps({'type':'session.commentary.append','delegation_id':'same-call','content':'Partial result.'}))
        await self.call()
        self.assertEqual(self.s.pending['same-call']['chunks'],['Partial result.'])
    async def test_cancelled_reply_cannot_be_spoken_later(self):
        await self.call()
        await self.s.on_gemini({'toolCallCancellation':{'ids':['same-call']}})
        await self.s.on_client(json.dumps({'type':'session.commentary.append','delegation_id':'same-call','content':'Late cancelled action result.'}))
        self.assertEqual(self.s.late,[],'Cancelled result re-enters late-speech queue')
    async def test_duplicate_function_response_sent_once(self):
        await self.call(); await self.s.respond('same-call','one'); await self.s.respond('same-call','one')
        self.assertEqual(self.s.ws.send.await_count,1)
    async def test_cancelled_call_gets_no_provider_answer(self):
        await self.call()
        await self.s.on_gemini({'toolCallCancellation':{'ids':['same-call']}})
        await self.s.respond('same-call','late')
        self.s.ws.send.assert_not_awaited()
    async def test_replay_after_completion_repeats_same_answer_once(self):
        await self.call(); await self.s.respond('same-call','Harmless counter incremented.')
        await self.call()
        self.assertEqual(self.s.ws.send.await_count,2)
        self.assertEqual(json.loads(self.s.ws.send.call_args.args[0])['toolResponse']['functionResponses'][0]['response']['result'],'Harmless counter incremented.')
        self.assertEqual(len(self.submissions()),1)
    # Not testable server-side: the native phone app's contract has no documented cancel event,
    # so provider cancels are NOT forwarded (an unknown event could crash that closed-source client).
    async def test_close_releases_bridge_owned_resources(self):
        task=asyncio.create_task(asyncio.sleep(5)); self.s.tasks.append(task)
        self.s.pc=SimpleNamespace(close=AsyncMock())
        await self.s.close('close_requested')
        self.assertTrue(task.cancelled()); self.s.ws.close.assert_awaited_once(); self.s.pc.close.assert_awaited_once()
    async def test_barge_in_clears_output_audio(self):
        self.s.out.buf.extend(b'1234')
        await self.s.on_gemini({'serverContent':{'interrupted':True}})
        self.assertEqual(self.s.out.buf,b'')
    async def test_mute_ends_provider_audio_stream(self):
        await self.s.on_client('{"type":"session.input_audio.mute"}')
        self.assertTrue(self.s.muted)
        self.assertEqual(json.loads(self.s.ws.send.call_args.args[0]),{'realtimeInput':{'audioStreamEnd':True}})

if __name__=='__main__': unittest.main()
