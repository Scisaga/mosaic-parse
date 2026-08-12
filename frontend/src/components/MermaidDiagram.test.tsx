import { act, render, screen, waitFor } from '@testing-library/react'

const mermaidMock = vi.hoisted(() => ({
  initialize: vi.fn(),
  render: vi.fn(),
  bindFunctions: vi.fn(),
}))

vi.mock('mermaid', () => ({ default: mermaidMock }))

import { MarkdownRenderer } from './MarkdownRenderer'
import { sanitizeMermaidSvg, validateMermaidSource } from './mermaidSecurity'

const safeSvg = `
  <svg xmlns="http://www.w3.org/2000/svg" width="640" height="240" viewBox="0 0 640 240" role="graphics-document">
    <style>#node { fill: #eef5f3; stroke: #356d67; }</style>
    <g id="node"><rect width="120" height="48" /><text x="8" y="28">安全流程</text></g>
  </svg>
`

describe('MermaidDiagram', () => {
  beforeEach(() => {
    mermaidMock.render.mockReset()
    mermaidMock.bindFunctions.mockReset()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('lazily renders a Mermaid fence with strict configuration in an isolated SVG host', async () => {
    mermaidMock.render.mockResolvedValue({ svg: safeSvg, bindFunctions: mermaidMock.bindFunctions })
    render(<MarkdownRenderer>{'```mermaid\nflowchart LR\n  A --> B\n```'}</MarkdownRenderer>)

    const diagram = await screen.findByRole('img', { name: 'Mermaid 图表' })
    expect(mermaidMock.initialize).toHaveBeenCalledWith(expect.objectContaining({
      startOnLoad: false,
      securityLevel: 'strict',
      suppressErrorRendering: true,
      htmlLabels: false,
      maxTextSize: 20_000,
      maxEdges: 500,
    }))
    expect(mermaidMock.render).toHaveBeenCalledWith(expect.stringMatching(/^docling-mermaid-\d+$/), 'flowchart LR\n  A --> B')
    expect(mermaidMock.bindFunctions).not.toHaveBeenCalled()
    expect(diagram.shadowRoot?.querySelector('svg')).toHaveAttribute('aria-hidden', 'true')
    expect(diagram.shadowRoot?.querySelector('svg')).toHaveAttribute('focusable', 'false')
    expect(diagram.shadowRoot?.querySelector('svg')).not.toHaveAttribute('width')
    expect(diagram.shadowRoot?.querySelector('svg')).not.toHaveAttribute('height')
    const caption = screen.getByText('AI 图表重建 · 请与原页核对')
    expect(caption).toBeInTheDocument()
    expect(diagram).toHaveAttribute('aria-describedby', caption.id)
  })

  it('does not load or render Mermaid before the diagram approaches the viewport', async () => {
    const callbacks: IntersectionObserverCallback[] = []
    const observe = vi.fn()
    const disconnect = vi.fn()
    class DeferredIntersectionObserver {
      readonly root = null
      readonly rootMargin = ''
      readonly thresholds = []
      constructor(callback: IntersectionObserverCallback) { callbacks.push(callback) }
      observe = observe
      unobserve = vi.fn()
      disconnect = disconnect
      takeRecords = vi.fn(() => [])
    }
    vi.stubGlobal('IntersectionObserver', DeferredIntersectionObserver)
    mermaidMock.render.mockResolvedValue({ svg: safeSvg })

    render(<MarkdownRenderer>{'```mermaid\nsequenceDiagram\n  A->>B: Hello\n```'}</MarkdownRenderer>)
    expect(observe).toHaveBeenCalledTimes(1)
    expect(mermaidMock.render).not.toHaveBeenCalled()

    act(() => callbacks[0]([{ isIntersecting: true } as IntersectionObserverEntry], {} as IntersectionObserver))
    expect(await screen.findByRole('img', { name: 'Mermaid 图表' })).toBeInTheDocument()
    expect(disconnect).toHaveBeenCalled()
  })

  it.each([
    ['frontmatter', '---\nconfig:\n  securityLevel: loose\n---\nflowchart LR\nA-->B'],
    ['init directive', '%%{init: {"securityLevel":"loose"}}%%\nflowchart LR\nA-->B'],
    ['click URL', 'flowchart LR\nA-->B\nclick A "https://example.com"'],
    ['embedded image', 'flowchart LR\nA[![x](https://example.com/x.png)]'],
  ])('rejects unsafe %s input before Mermaid runs', async (_name, source) => {
    const { container } = render(<MarkdownRenderer>{`\`\`\`mermaid\n${source}\n\`\`\``}</MarkdownRenderer>)

    await waitFor(() => expect(container.querySelector('.mermaid-error')).toBeInTheDocument())
    expect(screen.getByText(/不允许 Mermaid/)).toBeInTheDocument()
    expect(container.querySelector('code.language-mermaid')?.textContent).toBe(source)
    expect(mermaidMock.render).not.toHaveBeenCalled()
  })

  it('sanitizes active SVG content and rejects external CSS references', () => {
    const sanitized = sanitizeMermaidSvg(`
      <svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">
        <a href="https://example.com"><text>link</text></a>
        <image href="https://example.com/a.png" />
        <foreignObject><div xmlns="http://www.w3.org/1999/xhtml">HTML</div></foreignObject>
        <path onclick="alert(1)" d="M0 0L1 1" />
      </svg>
    `)
    expect(sanitized).not.toMatch(/<(?:a|image|foreignObject)\b/i)
    expect(sanitized).not.toMatch(/(?:href|onclick)=/i)
    expect(sanitized).not.toContain('https://')
    expect(() => sanitizeMermaidSvg('<svg xmlns="http://www.w3.org/2000/svg"><style>path{fill:url(https://example.com/a.svg)}</style></svg>')).toThrow()
  })

  it('shows an explicit escaped source fallback when rendering fails and preserves ordinary code fences', async () => {
    mermaidMock.render.mockRejectedValue(new Error('parser internals must not leak'))
    const { container, rerender } = render(<MarkdownRenderer>{'```mermaid\nflowchart LR\n  broken[\n```'}</MarkdownRenderer>)

    expect(await screen.findByText('Mermaid 图表无法安全渲染，已显示源码。')).toBeInTheDocument()
    expect(container.querySelector('code.language-mermaid')?.textContent).toBe('flowchart LR\n  broken[')
    expect(screen.queryByText(/parser internals/)).not.toBeInTheDocument()

    rerender(<MarkdownRenderer>{'```ts\nconst safe = true\n```'}</MarkdownRenderer>)
    expect(container.querySelector('pre > code.language-ts')).toHaveTextContent('const safe = true')
    expect(container.querySelector('.mermaid-diagram')).not.toBeInTheDocument()
  })

  it('keeps generated render IDs unique across diagrams', async () => {
    mermaidMock.render.mockResolvedValue({ svg: safeSvg })
    render(
      <MarkdownRenderer>
        {'```mermaid\nflowchart LR\nA-->B\n```\n\n```mermaid\nflowchart TD\nC-->D\n```'}
      </MarkdownRenderer>,
    )
    await waitFor(() => expect(mermaidMock.render).toHaveBeenCalledTimes(2))
    const firstId = mermaidMock.render.mock.calls[0][0]
    const secondId = mermaidMock.render.mock.calls[1][0]
    expect(firstId).not.toBe(secondId)
  })
})

describe('validateMermaidSource', () => {
  it('accepts ordinary diagram syntax without URLs or author-controlled styles', () => {
    expect(validateMermaidSource('flowchart LR\n  Input --> Parse --> Output')).toBeNull()
  })
})
