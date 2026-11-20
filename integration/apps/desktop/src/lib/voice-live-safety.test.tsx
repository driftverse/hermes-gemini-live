import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// Action-safety regression tests for the voice-Live conversation hook: replayed provider call ids
// must not run twice, and ending/losing the voice session must interrupt a started Hermes turn.

const state = vi.hoisted(() => ({ sessions: [] as any[] }))

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: { notifications: { voice: { liveEnded: 'Live ended', liveEndedClosed: 'closed', liveEndedConnectionLost: 'lost' } } }
  })
}))
vi.mock('@/store/notifications', () => ({ notify: vi.fn(), notifyError: vi.fn() }))
vi.mock('@/app/chat/composer/scope', () => ({
  useComposerScope: () => ({ connectionId: 'synthetic-connection', profile: 'default' })
}))
vi.mock('@/lib/voice-live', () => ({
  fetchVoiceLiveStatus: async () => ({ provider: 'gemini' }),
  VoiceLiveSession: class {
    activeDelegationId = null
    sessionId = 'synthetic'
    connected = true
    constructor(public handlers: any) {
      state.sessions.push(this)
    }
    async start() {}
    close = vi.fn()
    speak = vi.fn()
    finishDelegation = vi.fn()
    think = vi.fn()
    setMuted = vi.fn()
    instruct = vi.fn()
  }
}))

import { useVoiceLiveConversation } from '@/app/chat/composer/hooks/use-voice-live-conversation'

beforeEach(() => {
  state.sessions = []
})
afterEach(() => cleanup())

async function fixture() {
  const onInterrupt = vi.fn()
  const onSubmit = vi.fn()
  const onFatalError = vi.fn()

  const options = {
    busy: false,
    consumePendingResponse: vi.fn(),
    enabled: true,
    onFatalError,
    onInterrupt,
    onSubmit,
    pendingResponse: () => null,
    seedHistory: () => []
  }

  const hook = renderHook(props => useVoiceLiveConversation(props), { initialProps: options })
  await act(async () => {
    await hook.result.current.start()
  })
  await waitFor(() => expect(state.sessions.length).toBe(1))

  return { hook, onFatalError, onInterrupt, onSubmit, options, session: state.sessions[0] }
}

const delegate = (session: any, id = 'same-call') =>
  session.handlers.onDelegation(id, [{ endMs: 10, speaker: 'user', startMs: 0, text: 'Do one harmless counter action.' }], 'Do one harmless counter action.')

describe('voice-Live action safety', () => {
  it('submits a fresh delegation', async () => {
    const f = await fixture()
    act(() => delegate(f.session))
    expect(f.onSubmit).toHaveBeenCalledTimes(1)
  })

  it('does not submit a replayed call id twice', async () => {
    const f = await fixture()
    act(() => {
      delegate(f.session)
      delegate(f.session)
    })
    expect(f.onSubmit).toHaveBeenCalledTimes(1)
  })

  it('a newer request interrupts a busy turn', async () => {
    const f = await fixture()
    act(() => delegate(f.session, 'c1'))
    f.hook.rerender({ ...f.options, busy: true })
    act(() => delegate(f.session, 'c2'))
    expect(f.onInterrupt).toHaveBeenCalledTimes(1)
  })

  it('ending voice interrupts the running turn', async () => {
    const f = await fixture()
    act(() => delegate(f.session))
    f.hook.rerender({ ...f.options, busy: true })
    await act(async () => {
      await f.hook.result.current.end()
    })
    expect(f.onInterrupt).toHaveBeenCalledTimes(1)
  })

  it('a lost connection interrupts the running turn', async () => {
    const f = await fixture()
    act(() => delegate(f.session))
    f.hook.rerender({ ...f.options, busy: true })
    act(() => f.session.handlers.onClosed('connection_lost', 3))
    expect(f.onInterrupt).toHaveBeenCalledTimes(1)
  })

  it('unmounting interrupts the running turn', async () => {
    const f = await fixture()
    act(() => delegate(f.session))
    f.hook.rerender({ ...f.options, busy: true })
    f.hook.unmount()
    expect(f.onInterrupt).toHaveBeenCalledTimes(1)
  })

  it('a spoken stop interrupts the running turn', async () => {
    const f = await fixture()
    act(() => delegate(f.session))
    f.hook.rerender({ ...f.options, busy: true })
    act(() => f.session.handlers.onTranscript({ endMs: 10, speaker: 'user', startMs: 0, text: 'stop' }))
    await new Promise(resolve => setTimeout(resolve, 1700))
    expect(f.onInterrupt).toHaveBeenCalledTimes(1)
  })

  it('ending with nothing running does not interrupt', async () => {
    const f = await fixture()
    await act(async () => {
      await f.hook.result.current.end()
    })
    expect(f.onInterrupt).not.toHaveBeenCalled()
  })
})
