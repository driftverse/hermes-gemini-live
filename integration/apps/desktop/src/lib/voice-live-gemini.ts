import { type OwnerScope, ownerScoped } from '@/api/client'
import { hermesApi } from '@/hermes'

import type { LiveHistoryMessage, LiveTranscriptFragment, LiveVoiceSession, VoiceLiveHandlers } from './voice-live'

/**
 * Gemini Live transport for the app's Live mode (`voice.gpt_live.provider: gemini`).
 *
 * Same contract as the OpenAI transport (`lib/voice-live.ts`): the voice model owns the mic and
 * the speaker and hands real work to Hermes. Differences this file absorbs:
 *
 * - Transport is a WebSocket the renderer opens itself, with a SINGLE-USE EPHEMERAL TOKEN minted
 *   by the gateway (`tools/voice_live.py`). The Google API key never reaches the renderer.
 * - The gateway picks the model. A model that fails to start is reported back and the next one
 *   in the configured chain is dialed; when the whole chain fails the caller falls back to the
 *   chained speech-to-text engine (`LiveChainExhaustedError`).
 * - Audio is raw PCM: 16 kHz mono in, 24 kHz mono out. Barge-in arrives as `interrupted`.
 * - Delegation is one blocking function call, `delegate_to_hermes(request)`. It is answered with
 *   the text of Hermes' reply once the turn settles (`finishDelegation`).
 * - Dropped connections and `goAway` re-dial the same model with the session-resumption handle.
 */

export class LiveChainExhaustedError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'LiveChainExhaustedError'
  }
}

interface GeminiTransport {
  remaining?: string[]
  setup: Record<string, unknown>
  type: string
  url: string
}

interface GeminiSessionResponse {
  ok: boolean
  session?: { model?: string; provider?: string }
  transport?: GeminiTransport
}

interface GeminiMessage {
  goAway?: { timeLeft?: string }
  serverContent?: {
    inputTranscription?: { text?: string }
    interrupted?: boolean
    modelTurn?: { parts?: Array<{ inlineData?: { data?: string; mimeType?: string } }> }
    outputTranscription?: { text?: string }
    turnComplete?: boolean
  }
  sessionResumptionUpdate?: { newHandle?: string; resumable?: boolean }
  setupComplete?: Record<string, unknown>
  toolCall?: { functionCalls?: Array<{ args?: { request?: string }; id: string; name: string }> }
  toolCallCancellation?: { ids?: string[] }
  error?: { message?: string }
}

const DELEGATE_FUNCTION = 'delegate_to_hermes'
const INPUT_RATE = 16_000
const OUTPUT_RATE = 24_000
const SETUP_TIMEOUT_MS = 12_000
const MAX_START_ATTEMPTS = 8
const CONTEXT_WINDOW_MS = 5 * 60_000
const CONTEXT_MAX_FRAGMENTS = 80
const RESULT_CHAR_LIMIT = 6_000
const SPEAKING_TAIL_MS = 250

export function bytesToBase64(bytes: Uint8Array): string {
  let binary = ''
  const step = 0x8000

  for (let index = 0; index < bytes.length; index += step) {
    binary += String.fromCharCode(...bytes.subarray(index, index + step))
  }

  return btoa(binary)
}

export function base64ToBytes(data: string): Uint8Array {
  const binary = atob(data)
  const bytes = new Uint8Array(binary.length)

  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index)
  }

  return bytes
}

/** Float32 [-1, 1] → little-endian PCM16, resampled by decimation/linear interpolation when the
 *  context rate differs from the 16 kHz the API expects. */
export function floatToPcm16(input: Float32Array, fromRate: number, toRate = INPUT_RATE): Uint8Array {
  const ratio = fromRate / toRate
  const length = Math.max(0, Math.floor(input.length / ratio))
  const out = new Int16Array(length)

  for (let index = 0; index < length; index += 1) {
    const position = index * ratio
    const lower = Math.floor(position)
    const upper = Math.min(lower + 1, input.length - 1)
    const sample = input[lower]! + (input[upper]! - input[lower]!) * (position - lower)
    const clamped = Math.max(-1, Math.min(1, sample))
    out[index] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff
  }

  return new Uint8Array(out.buffer)
}

export function pcm16ToFloat(bytes: Uint8Array): Float32Array {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
  const out = new Float32Array(Math.floor(bytes.byteLength / 2))

  for (let index = 0; index < out.length; index += 1) {
    out[index] = view.getInt16(index * 2, true) / 0x8000
  }

  return out
}

export class GeminiLiveSession implements LiveVoiceSession {
  private readonly owner: null | OwnerScope
  private readonly handlers: VoiceLiveHandlers
  private socket: null | WebSocket = null
  private model = ''
  private resumeHandle: null | string = null
  private ready = false
  private finalized = false
  private reconnecting = false
  private muted = false
  private startedAt = 0
  private transcript: LiveTranscriptFragment[] = []
  private pending = new Set<string>()
  private buffers = new Map<string, string[]>()
  private microphone: null | MediaStream = null
  private inputContext: null | AudioContext = null
  private processor: null | ScriptProcessorNode = null
  private micSource: null | MediaStreamAudioSourceNode = null
  private outputContext: null | AudioContext = null
  private nextPlayTime = 0
  private playing = new Set<AudioBufferSourceNode>()
  private speaking = false
  private speakingTimer: null | number = null
  sessionId: null | string = null
  activeDelegationId: null | string = null

  constructor(handlers: VoiceLiveHandlers, owner: null | OwnerScope = null) {
    this.handlers = handlers
    this.owner = owner
  }

  get connected(): boolean {
    return this.ready && this.socket?.readyState === WebSocket.OPEN
  }

  contextWindow(): LiveTranscriptFragment[] {
    const last = this.transcript.at(-1)

    if (!last) {
      return []
    }

    const floor = last.endMs - CONTEXT_WINDOW_MS

    return this.transcript.filter(fragment => fragment.endMs >= floor).slice(-CONTEXT_MAX_FRAGMENTS)
  }

  async start(history: LiveHistoryMessage[]): Promise<void> {
    if (this.socket || this.startedAt) {
      throw new Error('Live session already started')
    }

    if (!navigator.mediaDevices?.getUserMedia) {
      throw new DOMException('Microphone capture is unavailable here', 'NotSupportedError')
    }

    this.startedAt = Date.now()

    // Mic first: the permission prompt must not eat into the per-model connect timeout.
    this.microphone = await navigator.mediaDevices.getUserMedia({
      audio: { autoGainControl: true, echoCancellation: true, noiseSuppression: true }
    })

    this.outputContext = new AudioContext({ sampleRate: OUTPUT_RATE })
    void this.outputContext.resume().catch(() => undefined)

    const failed: Array<{ model: string; reason: string }> = []

    for (let attempt = 0; attempt < MAX_START_ATTEMPTS; attempt += 1) {
      let response: GeminiSessionResponse

      try {
        response = await this.requestSession({ failed: [...failed], history })
      } catch (error) {
        // Once models have failed, a refusal from the gateway means the chain is spent.
        if (failed.length && !(error instanceof LiveChainExhaustedError)) {
          throw new LiveChainExhaustedError(error instanceof Error ? error.message : String(error))
        }

        throw error
      }

      const transport = response.transport
      this.model = response.session?.model ?? ''

      if (!transport?.url || !transport.setup) {
        throw new Error('Live session creation failed')
      }

      try {
        await this.openSocket(transport)
        this.armMicrophone()

        return
      } catch (error) {
        if (this.finalized) {
          return
        }

        failed.push({ model: this.model, reason: error instanceof Error ? error.message : String(error) })
        this.socket = null
        this.ready = false
      }
    }

    throw new LiveChainExhaustedError(`Live models failed to start: ${failed.map(f => f.model).join(', ')}`)
  }

  private async requestSession(body: {
    failed?: Array<{ model: string; reason: string }>
    history?: LiveHistoryMessage[]
    model?: string
    resume_handle?: string
  }): Promise<GeminiSessionResponse> {
    try {
      const response = await hermesApi<GeminiSessionResponse>({
        ...ownerScoped(this.owner ?? undefined),
        body,
        method: 'POST',
        path: '/api/audio/voice-live/session',
        timeoutMs: 45_000
      })

      if (!response?.ok) {
        throw new Error('Live session creation failed')
      }

      return response
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)

      if (/every gemini live model failed/i.test(message)) {
        throw new LiveChainExhaustedError(message)
      }

      throw error
    }
  }

  /** Dial one model: resolves once `setupComplete` arrives, rejects on close/error/timeout. */
  private openSocket(transport: GeminiTransport): Promise<void> {
    return new Promise<void>((resolve, reject) => {
      const socket = new WebSocket(transport.url)
      this.socket = socket
      this.ready = false
      let settled = false

      const fail = (reason: string) => {
        if (settled) {
          return
        }

        settled = true
        window.clearTimeout(timer)

        try {
          socket.close()
        } catch {
          // already closed
        }

        reject(new Error(reason))
      }

      const timer = window.setTimeout(() => fail('timed out waiting for setup'), SETUP_TIMEOUT_MS)

      socket.addEventListener('open', () => socket.send(JSON.stringify(transport.setup)))
      socket.addEventListener('error', () => fail('socket error'))
      socket.addEventListener('close', event => {
        if (!settled) {
          fail(`closed ${event.code} ${event.reason}`.trim())

          return
        }

        if (this.socket === socket) {
          this.onSocketClosed(event.code, event.reason)
        }
      })
      socket.addEventListener('message', event => {
        void this.decode(event.data).then(message => {
          if (!message || this.socket !== socket) {
            return
          }

          if (message.setupComplete && !settled) {
            settled = true
            window.clearTimeout(timer)
            this.ready = true
            resolve()

            return
          }

          this.handleMessage(message)
        })
      })
    })
  }

  private async decode(data: unknown): Promise<GeminiMessage | null> {
    try {
      const text = typeof data === 'string' ? data : data instanceof Blob ? await data.text() : ''

      return text ? (JSON.parse(text) as GeminiMessage) : null
    } catch {
      return null
    }
  }

  private send(message: Record<string, unknown>): boolean {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN || !this.ready) {
      return false
    }

    this.socket.send(JSON.stringify(message))

    return true
  }

  private armMicrophone(): void {
    if (!this.microphone || this.processor) {
      return
    }

    // ScriptProcessor, not an AudioWorklet: no worker/blob script, so no CSP surface to open.
    const context = new AudioContext({ sampleRate: INPUT_RATE })
    this.inputContext = context
    this.micSource = context.createMediaStreamSource(this.microphone)
    const processor = context.createScriptProcessor(2048, 1, 1)
    this.processor = processor
    const silence = context.createGain()
    silence.gain.value = 0

    processor.onaudioprocess = event => {
      if (this.muted || !this.connected) {
        return
      }

      const pcm = floatToPcm16(event.inputBuffer.getChannelData(0), context.sampleRate)

      if (pcm.length) {
        this.send({ realtimeInput: { audio: { data: bytesToBase64(pcm), mimeType: `audio/pcm;rate=${INPUT_RATE}` } } })
      }
    }

    this.micSource.connect(processor)
    processor.connect(silence)
    silence.connect(context.destination)
    void context.resume().catch(() => undefined)
  }

  private elapsed(): number {
    return Date.now() - this.startedAt
  }

  private addFragment(speaker: 'assistant' | 'user', text: string): void {
    if (!text) {
      return
    }

    const now = this.elapsed()
    const fragment: LiveTranscriptFragment = { endMs: now, speaker, startMs: now, text }
    this.transcript.push(fragment)

    if (this.transcript.length > 2_000) {
      this.transcript.splice(0, this.transcript.length - 1_500)
    }

    this.handlers.onTranscript?.(fragment)
  }

  private handleMessage(message: GeminiMessage): void {
    if (message.sessionResumptionUpdate?.newHandle && message.sessionResumptionUpdate.resumable !== false) {
      this.resumeHandle = message.sessionResumptionUpdate.newHandle
    }

    if (message.goAway) {
      void this.reconnect('go_away')
    }

    const content = message.serverContent

    if (content) {
      if (content.interrupted) {
        this.stopPlayback()
      }

      for (const part of content.modelTurn?.parts ?? []) {
        if (part.inlineData?.data) {
          this.enqueueAudio(base64ToBytes(part.inlineData.data))
        }
      }

      this.addFragment('user', content.inputTranscription?.text ?? '')
      this.addFragment('assistant', content.outputTranscription?.text ?? '')
    }

    for (const call of message.toolCall?.functionCalls ?? []) {
      if (call.name !== DELEGATE_FUNCTION) {
        this.send({
          toolResponse: { functionResponses: [{ id: call.id, name: call.name, response: { error: 'Unknown function' } }] }
        })

        continue
      }

      this.pending.add(call.id)
      this.activeDelegationId = call.id
      this.handlers.onDelegation(call.id, this.contextWindow(), call.args?.request ?? '')
    }

    for (const id of message.toolCallCancellation?.ids ?? []) {
      this.pending.delete(id)
      this.buffers.delete(id)

      if (this.activeDelegationId === id) {
        this.activeDelegationId = null
      }
    }

    if (message.error?.message) {
      this.handlers.onError(message.error.message, false)
    }
  }

  // ---- playback ----------------------------------------------------------------------------

  private enqueueAudio(bytes: Uint8Array): void {
    const context = this.outputContext

    if (!context || !bytes.length) {
      return
    }

    const samples = pcm16ToFloat(bytes)
    const buffer = context.createBuffer(1, samples.length, OUTPUT_RATE)
    buffer.copyToChannel(samples as Float32Array<ArrayBuffer>, 0)
    const source = context.createBufferSource()
    source.buffer = buffer
    source.connect(context.destination)
    const startAt = Math.max(context.currentTime + 0.03, this.nextPlayTime)
    source.start(startAt)
    this.nextPlayTime = startAt + buffer.duration
    this.playing.add(source)
    this.setSpeaking(true)

    source.onended = () => {
      this.playing.delete(source)

      if (!this.playing.size) {
        this.scheduleSpeakingOff()
      }
    }
  }

  /** Barge-in: drop everything queued so the voice stops at once. */
  private stopPlayback(): void {
    for (const source of this.playing) {
      source.onended = null

      try {
        source.stop()
      } catch {
        // not started / already stopped
      }
    }

    this.playing.clear()
    this.nextPlayTime = 0
    this.scheduleSpeakingOff()
  }

  private setSpeaking(speaking: boolean): void {
    if (this.speakingTimer) {
      window.clearTimeout(this.speakingTimer)
      this.speakingTimer = null
    }

    if (speaking !== this.speaking) {
      this.speaking = speaking
      this.handlers.onSpeakingChange?.(speaking)
    }
  }

  private scheduleSpeakingOff(): void {
    if (this.speakingTimer) {
      window.clearTimeout(this.speakingTimer)
    }

    this.speakingTimer = window.setTimeout(() => {
      this.speakingTimer = null

      if (!this.playing.size) {
        this.setSpeaking(false)
      }
    }, SPEAKING_TAIL_MS)
  }

  // ---- reconnect ---------------------------------------------------------------------------

  private onSocketClosed(code: number, reason: string): void {
    if (this.finalized) {
      return
    }

    this.ready = false
    void this.reconnect(`closed ${code} ${reason}`.trim())
  }

  /** Re-dial the same model with the resumption handle; one try, then the session ends. */
  private async reconnect(why: string): Promise<void> {
    if (this.finalized || this.reconnecting) {
      return
    }

    if (!this.resumeHandle || !this.model) {
      this.finish(why.startsWith('closed') ? 'connection_lost' : why, null)

      return
    }

    this.reconnecting = true
    const previous = this.socket

    try {
      const response = await this.requestSession({ model: this.model, resume_handle: this.resumeHandle })

      if (!response.transport) {
        throw new Error('no transport')
      }

      this.socket = null
      await this.openSocket(response.transport)
      previous?.close()
    } catch {
      previous?.close()
      this.finish('connection_lost', null)
    } finally {
      this.reconnecting = false
    }
  }

  // ---- LiveVoiceSession ---------------------------------------------------------------------

  /** Quiet progress is not a thing in a blocking function call; kept so the hook is engine-agnostic. */
  think(): void {
    // Intentionally empty.
  }

  speak(delegationId: null | string, content: string): void {
    if (!delegationId || !content.trim()) {
      return
    }

    const chunks = this.buffers.get(delegationId) ?? []
    chunks.push(content.trim())
    this.buffers.set(delegationId, chunks)
  }

  finishDelegation(delegationId: null | string, fallback = ''): void {
    if (!delegationId || !this.pending.has(delegationId)) {
      return
    }

    const text = (this.buffers.get(delegationId) ?? []).join(' ').trim() || fallback.trim()
    this.buffers.delete(delegationId)
    this.pending.delete(delegationId)

    if (this.activeDelegationId === delegationId) {
      this.activeDelegationId = null
    }

    this.send({
      toolResponse: {
        functionResponses: [
          {
            id: delegationId,
            name: DELEGATE_FUNCTION,
            response: { result: (text || 'Hermes finished that request without anything to report.').slice(0, RESULT_CHAR_LIMIT) }
          }
        ]
      }
    })
  }

  instruct(): void {
    // The model detects end of speech itself (server VAD); there is nothing to nudge.
  }

  setMuted(muted: boolean): void {
    this.muted = muted

    for (const track of this.microphone?.getAudioTracks() ?? []) {
      track.enabled = !muted
    }

    if (muted) {
      this.send({ realtimeInput: { audioStreamEnd: true } })
    }
  }

  close(): void {
    this.finish('close_requested', null)
  }

  private finish(reason: string, usageSeconds: null | number): void {
    if (this.finalized) {
      return
    }

    this.finalized = true
    this.ready = false

    if (this.speakingTimer) {
      window.clearTimeout(this.speakingTimer)
      this.speakingTimer = null
    }

    this.stopPlayback()
    this.processor?.disconnect()

    if (this.processor) {
      this.processor.onaudioprocess = null
    }

    this.micSource?.disconnect()
    void this.inputContext?.close().catch(() => undefined)
    void this.outputContext?.close().catch(() => undefined)
    this.microphone?.getTracks().forEach(track => track.stop())

    try {
      this.socket?.close(1000)
    } catch {
      // already closed
    }

    const seconds = usageSeconds ?? (this.startedAt ? Math.round(this.elapsed() / 1000) : null)
    this.handlers.onClosed(reason, reason === 'close_requested' ? null : seconds)
  }
}
