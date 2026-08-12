import { useEffect, useRef, useState, type KeyboardEvent, type PointerEvent, type ReactNode } from 'react'

interface SplitWorkspaceProps {
  preview: ReactNode
  result: ReactNode
  resultAttention?: boolean
}

function useNarrowScreen() {
  const [narrow, setNarrow] = useState(() => typeof window !== 'undefined' && window.matchMedia('(max-width: 820px)').matches)
  useEffect(() => {
    const media = window.matchMedia('(max-width: 820px)')
    const change = () => setNarrow(media.matches)
    media.addEventListener('change', change)
    return () => media.removeEventListener('change', change)
  }, [])
  return narrow
}

export function SplitWorkspace({ preview, result, resultAttention = false }: SplitWorkspaceProps) {
  const narrow = useNarrowScreen()
  const containerRef = useRef<HTMLDivElement>(null)
  const mobileTabRefs = useRef<Array<HTMLButtonElement | null>>([])
  const [previewFraction, setPreviewFraction] = useState(50)
  const [mobileTab, setMobileTab] = useState<'preview' | 'result'>('preview')

  useEffect(() => {
    if (narrow && resultAttention) setMobileTab('result')
  }, [narrow, resultAttention])

  const updateFromPointer = (clientX: number) => {
    const bounds = containerRef.current?.getBoundingClientRect()
    if (!bounds?.width) return
    setPreviewFraction(Math.max(28, Math.min(72, ((clientX - bounds.left) / bounds.width) * 100)))
  }

  const beginDrag = (event: PointerEvent<HTMLDivElement>) => {
    event.preventDefault()
    event.currentTarget.setPointerCapture(event.pointerId)
    updateFromPointer(event.clientX)
  }

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'ArrowLeft') setPreviewFraction((value) => Math.max(28, value - 2))
    else if (event.key === 'ArrowRight') setPreviewFraction((value) => Math.min(72, value + 2))
    else if (event.key === 'Home') setPreviewFraction(50)
    else return
    event.preventDefault()
  }

  const onMobileTabKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    let next = index
    if (event.key === 'ArrowLeft' || event.key === 'ArrowUp' || event.key === 'Home') next = 0
    else if (event.key === 'ArrowRight' || event.key === 'ArrowDown' || event.key === 'End') next = 1
    else return
    event.preventDefault()
    setMobileTab(next === 0 ? 'preview' : 'result')
    mobileTabRefs.current[next]?.focus()
  }

  if (narrow) {
    return (
      <section className="mobile-workspace">
        <div className="mobile-workspace-tabs" role="tablist" aria-label="工作区视图">
          <button ref={(node) => { mobileTabRefs.current[0] = node }} id="workspace-preview-tab" type="button" role="tab" aria-selected={mobileTab === 'preview'} aria-controls="workspace-mobile-panel" tabIndex={mobileTab === 'preview' ? 0 : -1} className={mobileTab === 'preview' ? 'active' : ''} onClick={() => setMobileTab('preview')} onKeyDown={(event) => onMobileTabKeyDown(event, 0)}>文档预览</button>
          <button ref={(node) => { mobileTabRefs.current[1] = node }} id="workspace-result-tab" type="button" role="tab" aria-selected={mobileTab === 'result'} aria-controls="workspace-mobile-panel" tabIndex={mobileTab === 'result' ? 0 : -1} className={mobileTab === 'result' ? 'active' : ''} onClick={() => setMobileTab('result')} onKeyDown={(event) => onMobileTabKeyDown(event, 1)}>解析结果{resultAttention ? <i /> : null}</button>
        </div>
        <div id="workspace-mobile-panel" className="mobile-workspace-content" role="tabpanel" aria-labelledby={mobileTab === 'preview' ? 'workspace-preview-tab' : 'workspace-result-tab'}>{mobileTab === 'preview' ? preview : result}</div>
      </section>
    )
  }

  return (
    <section
      className="split-workspace"
      ref={containerRef}
      style={{ gridTemplateColumns: `calc(${previewFraction}% - 6px) 12px calc(${100 - previewFraction}% - 6px)` }}
    >
      <div className="split-panel">{preview}</div>
      <div
        className="splitter"
        role="separator"
        aria-label="调整预览和结果宽度"
        aria-orientation="vertical"
        aria-valuemin={28}
        aria-valuemax={72}
        aria-valuenow={Math.round(previewFraction)}
        tabIndex={0}
        onPointerDown={beginDrag}
        onPointerMove={(event) => event.currentTarget.hasPointerCapture(event.pointerId) && updateFromPointer(event.clientX)}
        onDoubleClick={() => setPreviewFraction(50)}
        onKeyDown={onKeyDown}
      ><span /></div>
      <div className="split-panel">{result}</div>
    </section>
  )
}
