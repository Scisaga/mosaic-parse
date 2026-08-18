import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { App } from './App'

vi.mock('pdfjs-dist', () => ({
  GlobalWorkerOptions: { workerSrc: '' },
  getDocument: vi.fn(),
}))
vi.mock('pdfjs-dist/build/pdf.worker.min.mjs?url', () => ({ default: '/pdf.worker.mjs' }))

function renderApp() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(<QueryClientProvider client={queryClient}><App /></QueryClientProvider>)
}

describe('App workspace submission', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  afterEach(() => vi.restoreAllMocks())

  it('always submits through the jobs endpoint', async () => {
    const requested: string[] = []
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input)
      requested.push(`${init?.method ?? 'GET'} ${url}`)
      if (url.endsWith('/health')) return new Response(JSON.stringify({ status: 'ok' }), { headers: { 'Content-Type': 'application/json' } })
      if (url.endsWith('/ready')) return new Response(JSON.stringify({ status: 'ready', ready: true }), { headers: { 'Content-Type': 'application/json' } })
      if (url.endsWith('/v1/backends')) return new Response(JSON.stringify({ backends: [], queue: { active: 0, capacity: 8 } }), { headers: { 'Content-Type': 'application/json' } })
      if (url.endsWith('/v1/content/jobs') && init?.method === 'POST') {
        return new Response(JSON.stringify({
          id: 'job_async_only',
          status: 'completed',
          filename: 'report.png',
          progress: { current: 1, total: 1 },
        }), { status: 202, headers: { 'Content-Type': 'application/json' } })
      }
      if (url.includes('/v1/content/jobs/job_async_only/result')) {
        return new Response('', { headers: { 'Content-Type': url.includes('format=markdown') ? 'text/markdown' : 'text/plain' } })
      }
      throw new Error(`Unexpected request: ${url}`)
    })

    const user = userEvent.setup()
    const { container } = renderApp()
    const input = container.querySelector('input[type="file"]') as HTMLInputElement
    await user.upload(input, new File(['png'], 'report.png', { type: 'image/png' }))
    await user.click(screen.getByRole('button', { name: '开始解析' }))

    expect(await screen.findByText('任务 job_async_only 已创建')).toBeInTheDocument()
    expect(requested).toContain('POST /v1/content/jobs')
    expect(requested.some((request) => request.includes('/v1/content/parse'))).toBe(false)
  })
})
