import { buildParseForm, fetchServiceSnapshot, parseJobEvent } from './client'
import { DEFAULT_OPTIONS } from '../hooks/usePersistedOptions'

describe('API request helpers', () => {
  afterEach(() => vi.restoreAllMocks())

  it('builds the multipart contract for a file without setting Content-Type itself', () => {
    const file = new File(['pdf'], '报告.pdf', { type: 'application/pdf' })
    const form = buildParseForm(
      { kind: 'file', file, url: '' },
      { ...DEFAULT_OPTIONS, mode: 'ocr', pageRange: '2-4', enableVlmFallback: true },
    )
    expect(form.get('file')).toBeInstanceOf(File)
    expect((form.get('file') as File).name).toBe('报告.pdf')
    expect(form.get('mode')).toBe('ocr')
    expect(form.get('page_range')).toBe('2-4')
    expect(form.get('enable_vlm_fallback')).toBe('true')
    expect(form.get('include_pages')).toBe('true')
  })

  it('parses SSE JSON and ignores malformed messages', () => {
    const event = new MessageEvent('job.progress', { data: '{"current":3,"total":10}' })
    expect(parseJobEvent(event)).toMatchObject({ type: 'job.progress', current: 3, total: 10 })
    expect(parseJobEvent(new MessageEvent('message', { data: 'nope' }))).toBeNull()
  })

  it('maps real backend state/detail and queue values without treating configuration as readiness', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      const data = url.endsWith('/health')
        ? { status: 'ok' }
        : url.endsWith('/ready')
          ? { status: 'ready', ready: true }
        : {
          backends: [
            { name: 'docling-standard', state: 'ready', enabled: true },
            { name: 'glm-ocr-remote', state: 'unavailable', enabled: true, detail: 'connection refused' },
            { name: 'ollama-vlm', state: 'disabled', enabled: false },
          ],
          queue: { active: 1, capacity: 8 },
        }
      return new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } })
    })
    const snapshot = await fetchServiceSnapshot()
    expect(snapshot.api).toBe('ready')
    expect(snapshot.backends.find((backend) => backend.id === 'docling')?.status).toBe('ready')
    expect(snapshot.backends.find((backend) => backend.id === 'glm')).toMatchObject({ status: 'unavailable', message: 'connection refused' })
    expect(snapshot.backends.find((backend) => backend.id === 'vlm')?.status).toBe('disabled')
    expect(snapshot.queue).toEqual({ active: 1, capacity: 8 })
  })

  it('does not report API Ready when liveness succeeds but readiness fails', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/ready')) {
        return new Response(JSON.stringify({ status: 'not_ready', ready: false }), {
          status: 503,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      const body = url.endsWith('/health')
        ? { status: 'degraded' }
        : { backends: [], queue: { active: 0, capacity: 8 } }
      return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
    })
    const snapshot = await fetchServiceSnapshot()
    expect(snapshot.api).toBe('unavailable')
    expect(snapshot.ready).toBe(false)
  })
})
