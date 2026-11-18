import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { type VoiceLiveHandlers, VoiceLiveSession } from './voice-live'
import {
  base64ToBytes,
  bytesToBase64,
  floatToPcm16,
  GeminiLiveSession,
  LiveChainExhaustedError,
  pcm16ToFloat
} from './voice-live-gemini'

// ---- fakes ------------------------------------------------------------------------------------

class FakeSocket {
  static instances: FakeSocket[] = []
  static OPEN = 1
  static behaviour: Array<'close' | 'ok'> = []
  readyState = 0
  sent: Array<Record<string, any>> = []
  private listeners: Record<string, Array<(event: any) => void>> = {}

  constructor(public url: string) {
    FakeSocket.instances.push(this)
    const mode = FakeSocket.behaviour.shift() ?? 'ok'

    queueMicrotask(() => {
      if (mode === 'close') {
        this.readyState = 3
        this.emit('close', { code: 1011, reason: 'quota exhausted' })

        return
      }

      this.readyState = 1
      this.emit('open', {})
    })
  }

  addEventListener(type: string, fn: (event: any) => void) {
    ;(this.listeners[type] ??= []).push(fn)
  }

  emit(type: string, event: unknown) {
    for (const fn of this.listeners[type] ?? []) {
      fn(event)
    }
  }

  send(data: string) {
    const message = JSON.parse(data)
    this.sent.push(message)

    if (message.setup) {
      queueMicrotask(() => this.serve({ setupComplete: {} }))
    }
  }

  serve(message: unknown) {
    this.emit('message', { data: JSON.stringify(message) })
  }

  close() {
    this.readyState = 3
  }
}

class FakeNode {
  gain = { value: 1 }
  onaudioprocess: ((event: unknown) => void) | null = null
  connect() {
    return this
  }
  disconnect() {}
}

class FakeSource {
  static all: FakeSource[] = []
  buffer: { duration: number } | null = null
  onended: (() => void) | null = null
  started: null | number = null
  stopped = false
  constructor() {
    FakeSource.all.push(this)
  }
  connect() {}
  start(at: number) {
    this.started = at
  }
  stop() {
    this.stopped = true
  }
}

class FakeAudioContext {
  currentTime = 0
  destination = {}
  constructor(public options: { sampleRate: number }) {}
  get sampleRate() {
    return this.options.sampleRate
  }
  createMediaStreamSource() {
    return new FakeNode()
  }
  createScriptProcessor() {
    return new FakeNode()
  }
  createGain() {
    return new FakeNode()
  }
  createBuffer(_channels: number, length: number, rate: number) {
    return { copyToChannel() {}, duration: length / rate }
  }
  createBufferSource() {
    return new FakeSource()
  }
  async resume() {}
  async close() {}
}

const SETUP_RESPONSE = (model: string, remaining: string[] = []) => ({
  ok: true,
  session: { model, provider: 'gemini' },
  transport: { remaining, setup: { setup: { model: `models/${model}` } }, type: 'websocket', url: `wss://x/ws?access_token=t-${model}` }
})

function install(api: ReturnType<typeof vi.fn>) {
  Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: { api } })
  Object.defineProperty(globalThis, 'WebSocket', { configurable: true, value: FakeSocket })
  Object.defineProperty(globalThis, 'AudioContext', { configurable: true, value: FakeAudioContext })
  Object.defineProperty(window.navigator, 'mediaDevices', {
    configurable: true,
    value: { getUserMedia: async () => ({ getAudioTracks: () => [{ enabled: true }], getTracks: () => [{ stop() {} }] }) }
  })
}

function handlers(over: Partial<VoiceLiveHandlers> = {}): VoiceLiveHandlers {
  return { onClosed: vi.fn(), onDelegation: vi.fn(), onError: vi.fn(), ...over }
}

const tick = () => new Promise(resolve => setTimeout(resolve, 0))

beforeEach(() => {
  FakeSocket.instances = []
  FakeSocket.behaviour = []
  FakeSource.all = []
})

afterEach(() => {
  Reflect.deleteProperty(window, 'hermesDesktop')
  Reflect.deleteProperty(window.navigator, 'mediaDevices')
})

// ---- tests ------------------------------------------------------------------------------------

describe('pcm helpers', () => {
  it('round-trips base64 and pcm16, resampling by ratio', () => {
    const bytes = new Uint8Array([0, 1, 2, 250, 255])
    expect([...base64ToBytes(bytesToBase64(bytes))]).toEqual([...bytes])

    const pcm = floatToPcm16(new Float32Array([0, 0.5, -0.5, 1, -1, 2]), 16_000, 16_000)
    const back = pcm16ToFloat(pcm)
    expect(back[1]).toBeCloseTo(0.5, 2)
    expect(back[2]).toBeCloseTo(-0.5, 2)
    expect(back[5]).toBeCloseTo(1, 2) // clamped

    // 48 kHz → 16 kHz keeps one sample in three
    expect(floatToPcm16(new Float32Array(48), 48_000).length).toBe(16 * 2)
  })
})

describe('Gemini Live chain and delegation', () => {
  it('moves down the chain, reporting each failed model, then answers a delegation', async () => {
    // First two models close before setup (quota), the third connects.
    FakeSocket.behaviour = ['close', 'close', 'ok']
    const models = ['m1', 'm2', 'm3']

    const api = vi.fn(async (request: any) => {
      const failed: Array<{ model: string }> = request.body?.failed ?? []

      return SETUP_RESPONSE(models[failed.length]!, models.slice(failed.length + 1))
    })

    install(api)

    const onDelegation = vi.fn()
    const session = new GeminiLiveSession(handlers({ onDelegation }), null)
    await session.start([])

    expect(api).toHaveBeenCalledTimes(3)
    expect(api.mock.calls[0]![0].body.failed).toEqual([])
    expect(api.mock.calls[1]![0].body.failed.map((f: any) => f.model)).toEqual(['m1'])
    expect(api.mock.calls[2]![0].body.failed.map((f: any) => f.model)).toEqual(['m1', 'm2'])
    expect(api.mock.calls[2]![0].body.failed[0].reason).toContain('1011')
    expect(FakeSocket.instances).toHaveLength(3)
    expect(session.connected).toBe(true)

    // The renderer sends exactly the setup the gateway returned (key never involved).
    expect(FakeSocket.instances[2]!.sent[0]).toEqual({ setup: { model: 'models/m3' } })
    expect(FakeSocket.instances[2]!.url).toContain('access_token=')

    // Transcript then a delegate_to_hermes call.
    const socket = FakeSocket.instances[2]!
    socket.serve({ serverContent: { inputTranscription: { text: 'what is free space' } } })
    socket.serve({ toolCall: { functionCalls: [{ args: { request: 'How much disk space is free?' }, id: 'call-1', name: 'delegate_to_hermes' }] } })
    await tick()

    expect(onDelegation).toHaveBeenCalledTimes(1)
    const [id, context, request] = onDelegation.mock.calls[0]!
    expect(id).toBe('call-1')
    expect(request).toBe('How much disk space is free?')
    expect(context.map((f: any) => f.text).join('')).toContain('what is free space')

    // Hermes streams sentences, then settles: ONE function response carries the whole reply.
    session.speak('call-1', 'The disk is 29 percent full.')
    session.speak('call-1', 'There are 643 gigabytes free.')
    expect(socket.sent.some(m => m.toolResponse)).toBe(false)
    session.finishDelegation('call-1')
    const response = socket.sent.find(m => m.toolResponse)!.toolResponse.functionResponses[0]
    expect(response).toEqual({
      id: 'call-1',
      name: 'delegate_to_hermes',
      response: { result: 'The disk is 29 percent full. There are 643 gigabytes free.' }
    })

    // A second finish for the same id is ignored (no duplicate responses).
    session.finishDelegation('call-1')
    expect(socket.sent.filter(m => m.toolResponse)).toHaveLength(1)
  })

  it('throws LiveChainExhaustedError when the gateway says every model failed', async () => {
    FakeSocket.behaviour = ['close']

    const api = vi.fn(async (request: any) => {
      if ((request.body?.failed ?? []).length) {
        throw new Error('Every Gemini Live model failed to start: m1')
      }

      return SETUP_RESPONSE('m1')
    })

    install(api)

    const session = new GeminiLiveSession(handlers(), null)
    await expect(session.start([])).rejects.toBeInstanceOf(LiveChainExhaustedError)
    expect(api).toHaveBeenCalledTimes(2)
  })

  it('plays model audio and drops the queue on barge-in', async () => {
    const api = vi.fn(async () => SETUP_RESPONSE('m1'))
    install(api)
    const onSpeakingChange = vi.fn()
    const session = new GeminiLiveSession(handlers({ onSpeakingChange }), null)
    await session.start([])
    const socket = FakeSocket.instances[0]!

    const chunk = bytesToBase64(new Uint8Array(4800)) // 2400 samples = 100 ms at 24 kHz
    socket.serve({ serverContent: { modelTurn: { parts: [{ inlineData: { data: chunk, mimeType: 'audio/pcm;rate=24000' } }] } } })
    socket.serve({ serverContent: { modelTurn: { parts: [{ inlineData: { data: chunk, mimeType: 'audio/pcm;rate=24000' } }] } } })
    await tick()

    expect(FakeSource.all).toHaveLength(2)
    expect(FakeSource.all[1]!.started!).toBeGreaterThan(FakeSource.all[0]!.started!) // queued back to back
    expect(onSpeakingChange).toHaveBeenLastCalledWith(true)

    socket.serve({ serverContent: { interrupted: true } })
    await tick()
    expect(FakeSource.all.every(source => source.stopped)).toBe(true)
  })

  it('ignores a delegation that the model cancelled, and mute stops sending audio', async () => {
    const api = vi.fn(async () => SETUP_RESPONSE('m1'))
    install(api)
    const session = new GeminiLiveSession(handlers(), null)
    await session.start([])
    const socket = FakeSocket.instances[0]!

    socket.serve({ toolCall: { functionCalls: [{ args: { request: 'x' }, id: 'c9', name: 'delegate_to_hermes' }] } })
    await tick()
    socket.serve({ toolCallCancellation: { ids: ['c9'] } })
    await tick()
    session.speak('c9', 'late')
    session.finishDelegation('c9')
    expect(socket.sent.some(m => m.toolResponse)).toBe(false)

    session.setMuted(true)
    expect(socket.sent.at(-1)).toEqual({ realtimeInput: { audioStreamEnd: true } })
  })

  it('answers an unknown function with an error instead of hanging the model', async () => {
    const api = vi.fn(async () => SETUP_RESPONSE('m1'))
    install(api)
    const session = new GeminiLiveSession(handlers(), null)
    await session.start([])
    const socket = FakeSocket.instances[0]!
    socket.serve({ toolCall: { functionCalls: [{ args: {}, id: 'u1', name: 'do_something_else' }] } })
    await tick()
    expect(socket.sent.at(-1)!.toolResponse.functionResponses[0].response.error).toBeTruthy()
  })

  it('resumes the same model with the saved handle after an unexpected close', async () => {
    const api = vi.fn(async (request: any) => SETUP_RESPONSE(request.body?.model ?? 'm1'))
    install(api)
    const onClosed = vi.fn()
    const session = new GeminiLiveSession(handlers({ onClosed }), null)
    await session.start([])
    const first = FakeSocket.instances[0]!
    first.serve({ sessionResumptionUpdate: { newHandle: 'h-42', resumable: true } })
    await tick()

    first.readyState = 3
    first.emit('close', { code: 1006, reason: '' })
    await tick()
    await tick()

    const resume = api.mock.calls.at(-1)![0].body
    expect(resume).toEqual({ model: 'm1', resume_handle: 'h-42' })
    expect(FakeSocket.instances).toHaveLength(2)
    expect(session.connected).toBe(true)
    expect(onClosed).not.toHaveBeenCalled()
  })

  it('ends with connection_lost when it drops and there is nothing to resume', async () => {
    const api = vi.fn(async () => SETUP_RESPONSE('m1'))
    install(api)
    const onClosed = vi.fn()
    const session = new GeminiLiveSession(handlers({ onClosed }), null)
    await session.start([])
    const first = FakeSocket.instances[0]!
    first.readyState = 3
    first.emit('close', { code: 1006, reason: '' })
    await tick()
    expect(onClosed).toHaveBeenCalledWith('connection_lost', expect.any(Number))
  })

  it('VoiceLiveSession picks the Gemini transport from the provider hint', async () => {
    const api = vi.fn(async () => SETUP_RESPONSE('m1'))
    install(api)
    const session = new VoiceLiveSession(handlers(), null, 'gemini')
    await session.start([])
    expect(FakeSocket.instances).toHaveLength(1)
    expect(session.connected).toBe(true)
    session.close()
  })
})
