"""Opt-in real Google/Cloudflare probe against an already running bridge.
Only use synthetic WAV input. Delegated answer is a TEST STUB, not a Hermes agent run.
--relay-only removes non-relay remote candidates, excluding LAN/VPN/direct paths.
"""
import argparse
import asyncio
from fractions import Fraction
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
from unittest.mock import patch

import av
import numpy as np
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'integration'))
from tools import voice_live as vl


class Mic(MediaStreamTrack):
    kind = 'audio'
    def __init__(self, pcm, repeat_at, repeat_pcm=None):
        super().__init__()
        self.pcm, self.repeat_at = pcm, repeat_at
        self.repeat_pcm = repeat_pcm if repeat_pcm is not None else pcm
        self.ts, self.t0 = 0, None
    async def recv(self):
        if self.t0 is None: self.t0 = time.monotonic()
        await asyncio.sleep(max(0, self.t0 + self.ts/48000 - time.monotonic()))
        start = self.ts - 48000
        pcm = self.pcm
        if self.repeat_at and self.ts >= self.repeat_at*48000:
            start = self.ts - int(self.repeat_at*48000)
            pcm = self.repeat_pcm
        chunk = pcm[start:start+960] if start >= 0 else np.zeros(960, np.int16)
        chunk = np.pad(chunk, (0,max(0,960-len(chunk))))
        frame = av.AudioFrame.from_ndarray(chunk.reshape(1,-1), format='s16', layout='mono')
        frame.sample_rate, frame.pts, frame.time_base = 48000, self.ts, Fraction(1,48000)
        self.ts += 960
        return frame


def fake_nat_offer(sdp):
    """Emulate a phone behind NAT: advertise only two private host candidates that are NOT the addresses the
    traffic really leaves from (a relay or STUN server would see the NAT's public address instead)."""
    out, kept = [], 0
    for line in sdp.split('\r\n'):
        if line.startswith('a=candidate:'):
            parts = line.split(' ')
            if parts[2].lower() == 'udp' and parts[7] == 'host' and ':' not in parts[4] and kept < 2:
                parts[4] = f'192.168.77.{10 + kept}'
                kept += 1
                out.append(' '.join(parts))
            continue
        out.append(line)
    if not kept: raise RuntimeError('fake-nat: no IPv4 host candidate to rewrite')
    return '\r\n'.join(out)


async def run(args):
    wav = Path(args.wav)
    if not wav.is_file(): raise ValueError('Missing WAV fixture; refusing a silent test')
    raw = subprocess.run(['ffmpeg','-v','error','-i',str(wav),'-f','s16le','-ar','48000','-ac','1','-'],capture_output=True,check=True).stdout
    if not raw or len(raw) > 48000*2*30: raise ValueError('Fixture must be nonempty and under 30 seconds')
    repeat_pcm = None
    if args.repeat_wav:
        second = subprocess.run(['ffmpeg','-v','error','-i',args.repeat_wav,'-f','s16le','-ar','48000','-ac','1','-'],capture_output=True,check=True).stdout
        if not second or len(second) > 48000*2*30: raise ValueError('Second fixture must be nonempty and under 30 seconds')
        repeat_pcm = np.frombuffer(second,dtype=np.int16)
    pc = RTCPeerConnection()
    pc.addTrack(Mic(np.frombuffer(raw,dtype=np.int16), args.repeat_at, repeat_pcm))
    ch = pc.createDataChannel('oai-events')
    result = {'synthetic_stub':True,'relay_only':args.relay_only,'duration_requested':args.duration,'delegations':0,'audio_samples':0,'input_transcript':'','output_transcript':'','connected':False,'closed_cleanly':False}
    tasks = []
    @pc.on('connectionstatechange')
    async def state():
        if pc.connectionState == 'connected': result['connected'] = True
    @pc.on('track')
    def track(t):
        async def sink():
            try:
                while True:
                    f = await t.recv()
                    a = f.to_ndarray()
                    if np.abs(a.astype(np.int32)).max() > 500: result['audio_samples'] += f.samples
            except Exception: pass
        tasks.append(asyncio.create_task(sink()))
    @ch.on('message')
    def message(raw):
        m=json.loads(raw); kind=m.get('type')
        if kind == 'session.input_transcript.delta': result['input_transcript'] += m['delta']
        elif kind == 'session.output_transcript.delta': result['output_transcript'] += m['delta']
        elif kind == 'session.delegation.created':
            result['delegations'] += 1
            ch.send(json.dumps({'type':'session.commentary.append','delegation_id':m['delegation']['id'],'content':'Synthetic verification phrase: violet river. This is a test stub, not a server measurement.'}))
        elif kind == 'session.closed': result['closed_cleanly'] = m.get('reason') == 'close_requested'
    try:
        await pc.setLocalDescription(await pc.createOffer())
        with patch.object(vl,'_voice_section',return_value={'gpt_live':{'provider':'gemini'}}): setup=vl.build_gemini_setup(args.model)
        offer_sdp=pc.localDescription.sdp
        if args.fake_nat:
            offer_sdp=fake_nat_offer(offer_sdp)
            result['fake_nat']=True
        body={'sdp':offer_sdp,'model':args.model,'setup':setup}
        if args.peer_ip:
            body['peer_ip']=urllib.request.urlopen('https://api.ipify.org',timeout=10).read().decode().strip() if args.peer_ip=='auto' else args.peer_ip
            result['peer_ip_hint']=True
        def post():
            req=urllib.request.Request(args.bridge.rstrip('/')+'/offer',data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
            with urllib.request.urlopen(req,timeout=45) as resp: return json.load(resp)
        answer=await asyncio.to_thread(post)
        result['session_id']=answer['session']['id']
        sdp=answer['sdp']
        if args.relay_only:
            sdp='\r\n'.join(line for line in sdp.splitlines() if not line.startswith('a=candidate:') or ' typ relay' in line)+'\r\n'
            if not any(line.startswith('a=candidate:') and ' typ relay' in line for line in sdp.splitlines()): raise RuntimeError('No relay candidate available')
        await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp,type='answer'))
        await asyncio.sleep(args.duration)
        result['still_connected_before_close']=pc.connectionState == 'connected'
        connection = pc.sctp.transport.transport._connection
        result['selected_pairs'] = [{'local':p.local_candidate.type,'remote':p.remote_candidate.type} for p in connection._nominated.values()]
        ch.send('{"type":"session.close"}')
        await asyncio.sleep(1)
    finally:
        await pc.close()
        for task in tasks: task.cancel()
    expected=2 if args.repeat_at else 1
    result['passed']=bool(result['connected'] and result['closed_cleanly'] and result['still_connected_before_close'] and result['delegations']>=expected and result['audio_samples']>0 and 'violet' in result['output_transcript'].lower() and (not args.relay_only or (result['selected_pairs'] and all('relay' in (p['local'], p['remote']) for p in result['selected_pairs']))))
    print(json.dumps(result,indent=2))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('wav'); p.add_argument('--bridge',default='http://127.0.0.1:8890')
    p.add_argument('--model',default='gemini-3.8-live'); p.add_argument('--duration',type=float,default=40)
    p.add_argument('--repeat-at',type=float,default=0); p.add_argument('--repeat-wav'); p.add_argument('--relay-only',action='store_true')
    p.add_argument('--peer-ip',help="send the bridge a public-address hint for the client ('auto' = this machine's public IPv4)")
    p.add_argument('--fake-nat',action='store_true',help='offer only fake private host candidates (emulates a phone behind NAT)')
    sys.exit(asyncio.run(run(p.parse_args())))
