"""TEST ONLY: run the copied bridge with only relay candidate protocols.
Uses aioice internals, pinned by requirements.txt. Not a deployment configuration.
No LAN/VPN/direct ICE sockets survive gathering. Never run inside a live process.
"""
import asyncio
import sys
from pathlib import Path
import aioice

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'bridge'))
import live_bridge

original=aioice.Connection.get_component_candidates
async def relay_only(self,*args,**kwargs):
    candidates=await original(self,*args,**kwargs)
    keep=[]
    for protocol in self._protocols:
        if protocol.local_candidate.type == 'relay': keep.append(protocol)
        else: protocol.transport.close()
    self._protocols=keep
    relays=[c for c in candidates if c.type=='relay']
    if not relays: raise RuntimeError('Relay-only test has no TURN allocation')
    return relays

aioice.Connection.get_component_candidates=relay_only

# Optional real-provider reconnect exercise, only in this isolated test server.
import os
resume_after = float(os.environ.get('LIVE_TEST_RESUME_AFTER', '0'))
if resume_after:
    original_answer = live_bridge.Session.answer
    async def answer_with_resume(self, *args, **kwargs):
        answer = await original_answer(self, *args, **kwargs)
        async def force_drop():
            await asyncio.sleep(resume_after)
            live_bridge.log.info('%s test socket close; resume_available=%s', self.id, bool(self.resume))
            if self.ws is not None and not self.closed:
                await self.ws.close()
        self.tasks.append(asyncio.create_task(force_drop()))
        return answer
    live_bridge.Session.answer = answer_with_resume

if __name__=='__main__': live_bridge.main()
