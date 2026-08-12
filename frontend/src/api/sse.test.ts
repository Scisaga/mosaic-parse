import { setApiKey } from './client'
import { consumeSseStream, EventStreamParser, runSseLoop } from './sse'

const encoder = new TextEncoder()

function eventStream(chunks: string[]): ReadableStream<Uint8Array> {
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk))
      controller.close()
    },
  })
}

describe('fetch SSE transport', () => {
  afterEach(() => {
    setApiKey('')
    vi.restoreAllMocks()
  })

  it('parses split CRLF frames, comments, ids, retry, and multiline data', () => {
    const parser = new EventStreamParser()
    expect(parser.push(': heartbeat\r')).toEqual([])
    const frames = parser.push('\nevent: job.progress\r\nid: 41\r\ndata: {"current":\r\ndata: 2}\r\nretry: 1200\r\n\r\n')
    expect(frames).toEqual([{
      id: '41',
      event: 'job.progress',
      data: '{"current":\n2}',
      retry: 1200,
    }])
    expect(JSON.parse(frames[0].data)).toEqual({ current: 2 })

    expect(parser.push('id: ignored\0value\ndata: tail')).toEqual([])
    expect(parser.finish()).toEqual([{
      id: '41',
      event: 'message',
      data: 'tail',
      retry: 1200,
    }])

    const resetParser = new EventStreamParser('previous-id')
    expect(resetParser.push('id:\ndata: reset\n\n')[0].id).toBe('')
  })

  it('sends the session API key as a header and never puts it in the URL', async () => {
    setApiKey('top-secret-token')
    const calls: Array<{ url: string; headers: Headers }> = []
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), headers: new Headers(init?.headers) })
      return new Response(eventStream(['event: heartbeat\ndata: {"type":"heartbeat"}\n\n']), {
        status: 200,
        headers: { 'Content-Type': 'text/event-stream' },
      })
    }) as unknown as typeof fetch
    const frames: string[] = []
    await consumeSseStream({
      url: '/v1/documents/jobs/job_1/events',
      signal: new AbortController().signal,
      fetchImpl,
      onFrame: (frame) => frames.push(frame.event),
    })
    expect(frames).toEqual(['heartbeat'])
    expect(calls[0].headers.get('X-API-Key')).toBe('top-secret-token')
    expect(calls[0].headers.get('Accept')).toBe('text/event-stream')
    expect(calls[0].url).toBe('/v1/documents/jobs/job_1/events')
    expect(calls[0].url).not.toContain('top-secret-token')
  })

  it('reconnects with Last-Event-ID and exponential backoff until aborted', async () => {
    setApiKey('session-key')
    const controller = new AbortController()
    const calls: Headers[] = []
    let call = 0
    const fetchImpl = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      calls.push(new Headers(init?.headers))
      call += 1
      if (call === 1) {
        return new Response(eventStream(['id: 7\nevent: job.progress\ndata: {"current":1}\n\n']), { status: 200 })
      }
      if (call === 2) return new Response(null, { status: 503 })
      return new Response(eventStream(['id: 8\nevent: job.completed\ndata: {"status":"completed"}\n\n']), { status: 200 })
    }) as unknown as typeof fetch
    const delays: number[] = []
    const eventIds: Array<string | undefined> = []
    await runSseLoop({
      url: '/v1/documents/jobs/job_1/events',
      signal: controller.signal,
      fetchImpl,
      baseDelay: 10,
      maximumDelay: 100,
      sleep: async (delay) => { delays.push(delay) },
      onState: vi.fn(),
      onFrame: (frame) => {
        eventIds.push(frame.id)
        if (frame.id === '8') controller.abort()
      },
    })
    expect(eventIds).toEqual(['7', '8'])
    expect(delays).toEqual([10, 20])
    expect(calls).toHaveLength(3)
    expect(calls[1].get('Last-Event-ID')).toBe('7')
    expect(calls[2].get('Last-Event-ID')).toBe('7')
    expect(calls.every((headers) => headers.get('X-API-Key') === 'session-key')).toBe(true)
  })
})
