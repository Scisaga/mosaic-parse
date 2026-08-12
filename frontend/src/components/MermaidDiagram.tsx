import { useEffect, useId, useRef, useState } from 'react'
import { MAX_MERMAID_SOURCE_LENGTH, sanitizeMermaidSvg, validateMermaidSource } from './mermaidSecurity'

type MermaidState =
  | { status: 'waiting' | 'loading' }
  | { status: 'ready'; svg: string }
  | { status: 'error'; message: string }

type MermaidApi = typeof import('mermaid')['default']

let mermaidPromise: Promise<MermaidApi> | null = null
let renderQueue: Promise<void> = Promise.resolve()
let renderSequence = 0

function loadMermaid(): Promise<MermaidApi> {
  if (!mermaidPromise) {
    mermaidPromise = import('mermaid').then(({ default: mermaid }) => {
      mermaid.initialize({
        startOnLoad: false,
        securityLevel: 'strict',
        suppressErrorRendering: true,
        htmlLabels: false,
        maxTextSize: MAX_MERMAID_SOURCE_LENGTH,
        maxEdges: 500,
        logLevel: 'fatal',
        flowchart: { htmlLabels: false, useMaxWidth: true },
      })
      return mermaid
    })
  }
  return mermaidPromise
}

function enqueueRender<T>(render: () => Promise<T>): Promise<T> {
  const result = renderQueue.then(render, render)
  renderQueue = result.then(() => undefined, () => undefined)
  return result
}

function mountSvg(host: HTMLDivElement, svg: string): void {
  const shadow = host.shadowRoot ?? host.attachShadow({ mode: 'open' })
  const document = new DOMParser().parseFromString(svg, 'image/svg+xml')
  const style = window.document.createElement('style')
  style.textContent = ':host{display:block}svg{display:block;width:100%;height:auto;max-height:34rem;margin:auto;overflow:visible}'
  shadow.replaceChildren(style, window.document.importNode(document.documentElement, true))
}

function SourceFallback({ source, message, pending = false }: { source: string; message: string; pending?: boolean }) {
  return (
    <div className={`mermaid-fallback ${pending ? 'mermaid-pending' : 'mermaid-error'}`}>
      <strong role={pending ? 'status' : 'alert'}>{message}</strong>
      <pre><code className="language-mermaid">{source}</code></pre>
    </div>
  )
}

export function MermaidDiagram({ source }: { source: string }) {
  const captionId = useId()
  const anchorRef = useRef<HTMLDivElement>(null)
  const svgHostRef = useRef<HTMLDivElement>(null)
  const [nearViewport, setNearViewport] = useState(false)
  const [state, setState] = useState<MermaidState>({ status: 'waiting' })

  useEffect(() => {
    const host = anchorRef.current
    if (!host || nearViewport) return
    if (typeof IntersectionObserver === 'undefined') {
      setNearViewport(true)
      return
    }
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) {
        setNearViewport(true)
        observer.disconnect()
      }
    }, { rootMargin: '240px' })
    observer.observe(host)
    return () => observer.disconnect()
  }, [nearViewport])

  useEffect(() => {
    const validationError = validateMermaidSource(source)
    if (validationError) {
      setState({ status: 'error', message: validationError })
      return
    }
    if (!nearViewport) {
      setState({ status: 'waiting' })
      return
    }
    let disposed = false
    setState({ status: 'loading' })
    const renderId = `docling-mermaid-${++renderSequence}`
    void enqueueRender(async () => {
      const mermaid = await loadMermaid()
      const { svg } = await mermaid.render(renderId, source)
      return sanitizeMermaidSvg(svg)
    }).then((svg) => {
      if (!disposed) setState({ status: 'ready', svg })
    }).catch(() => {
      if (!disposed) setState({ status: 'error', message: 'Mermaid 图表无法安全渲染，已显示源码。' })
    })
    return () => { disposed = true }
  }, [nearViewport, source])

  useEffect(() => {
    if (state.status === 'ready' && svgHostRef.current) mountSvg(svgHostRef.current, state.svg)
  }, [state])

  return (
    <figure className="mermaid-diagram" data-mermaid-state={state.status}>
      <div ref={anchorRef} className="mermaid-viewport-anchor">
        {state.status === 'ready'
          ? <div className="mermaid-svg-host" ref={svgHostRef} role="img" aria-label="Mermaid 图表" aria-describedby={captionId} />
          : <SourceFallback source={source} pending={state.status !== 'error'} message={state.status === 'error' ? state.message : '正在安全渲染 Mermaid 图表…'} />}
      </div>
      {state.status === 'ready' && <figcaption id={captionId}>AI 图表重建 · 请与原页核对</figcaption>}
    </figure>
  )
}
