import { getApiKey } from './client'

export interface SseFrame {
  id?: string
  event: string
  data: string
  retry?: number
}

export type SseConnectionState = 'connecting' | 'live' | 'fallback'

/** Incremental WHATWG event-stream parser, including CRLF and split UTF-8 chunks. */
export class EventStreamParser {
  private buffer = ''
  private dataLines: string[] = []
  private eventType = ''
  private eventId: string
  private reconnectTime?: number

  constructor(lastEventId = '') {
    this.eventId = lastEventId
  }

  get lastEventId(): string {
    return this.eventId
  }

  get retryMilliseconds(): number | undefined {
    return this.reconnectTime
  }

  push(chunk: string): SseFrame[] {
    this.buffer += chunk
    return this.readLines(false)
  }

  finish(): SseFrame[] {
    const frames = this.readLines(true)
    if (this.buffer) {
      this.processLine(this.buffer, frames)
      this.buffer = ''
    }
    this.dispatch(frames)
    return frames
  }

  private readLines(final: boolean): SseFrame[] {
    const frames: SseFrame[] = []
    while (this.buffer) {
      const lf = this.buffer.indexOf('\n')
      const cr = this.buffer.indexOf('\r')
      const newline = lf < 0 ? cr : cr < 0 ? lf : Math.min(lf, cr)
      if (newline < 0) break
      if (!final && this.buffer[newline] === '\r' && newline === this.buffer.length - 1) break
      const line = this.buffer.slice(0, newline)
      const consume = this.buffer[newline] === '\r' && this.buffer[newline + 1] === '\n' ? 2 : 1
      this.buffer = this.buffer.slice(newline + consume)
      this.processLine(line, frames)
    }
    return frames
  }

  private processLine(line: string, frames: SseFrame[]): void {
    if (line === '') {
      this.dispatch(frames)
      return
    }
    if (line.startsWith(':')) return
    const colon = line.indexOf(':')
    const field = colon < 0 ? line : line.slice(0, colon)
    let value = colon < 0 ? '' : line.slice(colon + 1)
    if (value.startsWith(' ')) value = value.slice(1)
    if (field === 'event') this.eventType = value
    else if (field === 'data') this.dataLines.push(value)
    else if (field === 'id' && !value.includes('\0')) this.eventId = value
    else if (field === 'retry' && /^\d+$/.test(value)) this.reconnectTime = Number(value)
  }

  private dispatch(frames: SseFrame[]): void {
    if (this.dataLines.length > 0) {
      frames.push({
        id: this.eventId,
        event: this.eventType || 'message',
        data: this.dataLines.join('\n'),
        retry: this.reconnectTime,
      })
    }
    this.dataLines = []
    this.eventType = ''
  }
}

interface ConsumeSseOptions {
  url: string
  signal: AbortSignal
  lastEventId?: string
  onFrame: (frame: SseFrame) => void
  onOpen?: () => void
  fetchImpl?: typeof fetch
}

interface ConsumeSseResult {
  lastEventId?: string
  retryMilliseconds?: number
}

export class SseConnectionError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'SseConnectionError'
    this.status = status
  }
}

function abortError(): DOMException {
  return new DOMException('The operation was aborted', 'AbortError')
}

/** Opens one authenticated event-stream connection and consumes it to EOF. */
export async function consumeSseStream({
  url,
  signal,
  lastEventId,
  onFrame,
  onOpen,
  fetchImpl = fetch,
}: ConsumeSseOptions): Promise<ConsumeSseResult> {
  const headers = new Headers({ Accept: 'text/event-stream', 'Cache-Control': 'no-cache' })
  const apiKey = getApiKey()
  if (apiKey) headers.set('X-API-Key', apiKey)
  if (lastEventId) headers.set('Last-Event-ID', lastEventId)
  const response = await fetchImpl(url, {
    method: 'GET',
    headers,
    credentials: 'same-origin',
    cache: 'no-store',
    signal,
  })
  if (!response.ok) throw new SseConnectionError(response.status, `SSE HTTP ${response.status}`)
  if (!response.body) throw new SseConnectionError(response.status, 'SSE response body is unavailable')
  onOpen?.()

  const parser = new EventStreamParser(lastEventId)
  const decoder = new TextDecoder()
  const reader = response.body.getReader()
  try {
    for (;;) {
      if (signal.aborted) throw abortError()
      const { done, value } = await reader.read()
      if (done) break
      for (const frame of parser.push(decoder.decode(value, { stream: true }))) onFrame(frame)
    }
    for (const frame of parser.push(decoder.decode())) onFrame(frame)
    for (const frame of parser.finish()) onFrame(frame)
  } finally {
    reader.releaseLock()
  }
  return {
    lastEventId: parser.lastEventId,
    retryMilliseconds: parser.retryMilliseconds,
  }
}

export function reconnectDelay(attempt: number, baseMilliseconds = 750, maximumMilliseconds = 15_000): number {
  const safeAttempt = Math.max(0, Math.floor(attempt))
  return Math.min(maximumMilliseconds, baseMilliseconds * (2 ** safeAttempt))
}

function abortableDelay(milliseconds: number, signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.reject(abortError())
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(() => {
      signal.removeEventListener('abort', abort)
      resolve()
    }, milliseconds)
    const abort = () => {
      window.clearTimeout(timer)
      reject(abortError())
    }
    signal.addEventListener('abort', abort, { once: true })
  })
}

interface RunSseLoopOptions {
  url: string
  signal: AbortSignal
  onFrame: (frame: SseFrame) => void
  onState: (state: SseConnectionState) => void
  onError?: (error: unknown) => void
  onReconnect?: (detail: { attempt: number; delay: number; lastEventId?: string }) => void
  fetchImpl?: typeof fetch
  sleep?: (milliseconds: number, signal: AbortSignal) => Promise<void>
  baseDelay?: number
  maximumDelay?: number
}

/** Maintains an authenticated stream; callers keep polling independently as the durable source of truth. */
export async function runSseLoop({
  url,
  signal,
  onFrame,
  onState,
  onError,
  onReconnect,
  fetchImpl,
  sleep = abortableDelay,
  baseDelay = 750,
  maximumDelay = 15_000,
}: RunSseLoopOptions): Promise<void> {
  let attempt = 0
  let lastEventId: string | undefined
  let serverRetry: number | undefined
  while (!signal.aborted) {
    onState('connecting')
    try {
      const result = await consumeSseStream({
        url,
        signal,
        lastEventId,
        fetchImpl,
        onOpen: () => {
          // A healthy connection starts a fresh backoff series. Without this,
          // unrelated disconnects hours apart would eventually wait the cap.
          attempt = 0
          onState('live')
        },
        onFrame: (frame) => {
          if (frame.id !== undefined) lastEventId = frame.id
          if (frame.retry !== undefined) serverRetry = frame.retry
          onFrame(frame)
        },
      })
      if (result.lastEventId !== undefined) lastEventId = result.lastEventId
      if (result.retryMilliseconds !== undefined) serverRetry = result.retryMilliseconds
    } catch (error) {
      if (signal.aborted || (error instanceof DOMException && error.name === 'AbortError')) return
      onError?.(error)
    }
    if (signal.aborted) return
    onState('fallback')
    const delay = reconnectDelay(attempt, serverRetry ?? baseDelay, maximumDelay)
    onReconnect?.({ attempt: attempt + 1, delay, lastEventId })
    attempt += 1
    try {
      await sleep(delay, signal)
    } catch (error) {
      if (signal.aborted || (error instanceof DOMException && error.name === 'AbortError')) return
      throw error
    }
  }
}
