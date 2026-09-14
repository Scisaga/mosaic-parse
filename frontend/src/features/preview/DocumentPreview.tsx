import { useCallback, useEffect, useMemo, useRef, useState, type RefObject } from 'react'
import { GlobalWorkerOptions, getDocument, type PDFDocumentProxy } from 'pdfjs-dist'
import pdfWorkerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import { ChevronLeftIcon, ChevronRightIcon, RotateIcon } from '../../components/Icons'
import { WorkspaceState } from '../../components/WorkspaceState'
import type { SourceSelection } from '../../types/api'

GlobalWorkerOptions.workerSrc = pdfWorkerUrl

interface DocumentPreviewProps {
  source: SourceSelection
  activePage?: number | null
  onPageChange?: (page: number) => void
  onMetadata?: (pageCount: number | null) => void
}

interface PdfPageCanvasProps {
  pdf: PDFDocumentProxy
  pageNumber: number
  pageWidth: number
  rotation: number
  current: boolean
  scrollRootRef: RefObject<HTMLDivElement>
}

const DEFAULT_PAGE_SIZE = { width: 612, height: 792 }

function PdfPageCanvas({ pdf, pageNumber, pageWidth, rotation, current, scrollRootRef }: PdfPageCanvasProps) {
  const shellRef = useRef<HTMLDivElement>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const [nearViewport, setNearViewport] = useState(() => typeof IntersectionObserver === 'undefined')
  const [naturalSize, setNaturalSize] = useState(DEFAULT_PAGE_SIZE)
  const [renderError, setRenderError] = useState<string | null>(null)

  useEffect(() => {
    const shell = shellRef.current
    if (!shell) return
    if (typeof IntersectionObserver === 'undefined') {
      setNearViewport(true)
      return
    }
    const observer = new IntersectionObserver(
      ([entry]) => setNearViewport(entry?.isIntersecting ?? false),
      { root: scrollRootRef.current, rootMargin: '700px 0px' },
    )
    observer.observe(shell)
    return () => observer.disconnect()
  }, [pdf, pageNumber, scrollRootRef])

  useEffect(() => {
    setRenderError(null)
    if (!nearViewport || pageWidth <= 0 || !canvasRef.current) return
    let disposed = false
    let renderTask: ReturnType<Awaited<ReturnType<PDFDocumentProxy['getPage']>>['render']> | undefined

    void pdf.getPage(pageNumber).then((pdfPage) => {
      if (disposed || !canvasRef.current) return
      const naturalViewport = pdfPage.getViewport({ scale: 1 })
      setNaturalSize({ width: naturalViewport.width, height: naturalViewport.height })

      const effectiveRotation = (pdfPage.rotate + rotation) % 360
      const unitViewport = pdfPage.getViewport({ scale: 1, rotation: effectiveRotation })
      const viewport = pdfPage.getViewport({ scale: pageWidth / unitViewport.width, rotation: effectiveRotation })
      const deviceRatio = Math.min(window.devicePixelRatio || 1, 2)
      const pixelLimitRatio = Math.sqrt(16_000_000 / Math.max(1, viewport.width * viewport.height))
      const outputRatio = Math.max(.75, Math.min(deviceRatio, pixelLimitRatio))
      const canvas = canvasRef.current
      const context = canvas.getContext('2d')
      if (!context) throw new Error('浏览器无法创建 PDF 画布')

      canvas.width = Math.max(1, Math.floor(viewport.width * outputRatio))
      canvas.height = Math.max(1, Math.floor(viewport.height * outputRatio))
      canvas.style.width = '100%'
      canvas.style.height = 'auto'
      renderTask = pdfPage.render({
        canvasContext: context,
        viewport,
        transform: outputRatio === 1 ? undefined : [outputRatio, 0, 0, outputRatio, 0, 0],
      })
      return renderTask.promise
    }).catch((reason: unknown) => {
      if (!disposed && !(reason instanceof Error && reason.name === 'RenderingCancelledException')) {
        setRenderError(reason instanceof Error ? reason.message : `第 ${pageNumber} 页渲染失败`)
      }
    })

    return () => {
      disposed = true
      renderTask?.cancel()
    }
  }, [nearViewport, pageNumber, pageWidth, pdf, rotation])

  const aspectRatio = rotation % 180 === 0
    ? naturalSize.width / naturalSize.height
    : naturalSize.height / naturalSize.width

  return (
    <div
      ref={shellRef}
      className={`pdf-page-shell ${current ? 'pdf-page-current' : ''}`}
      data-pdf-page={pageNumber}
      role="group"
      aria-label={`PDF 第 ${pageNumber} 页`}
      aria-current={current ? 'page' : undefined}
      style={{ aspectRatio }}
    >
      {nearViewport && !renderError && <canvas ref={canvasRef} aria-hidden="true" />}
      {!nearViewport && <div className="pdf-page-placeholder" aria-hidden="true" />}
      {renderError && <div className="pdf-page-error" role="alert">第 {pageNumber} 页暂时无法显示</div>}
      <span className="pdf-page-number" aria-hidden="true">{pageNumber}</span>
    </div>
  )
}

function useSourceUrl(source: SourceSelection): string | null {
  const [fileUrl, setFileUrl] = useState<string | null>(null)
  useEffect(() => {
    if (source.kind !== 'file' || !source.file) {
      setFileUrl(null)
      return
    }
    const url = URL.createObjectURL(source.file)
    setFileUrl(url)
    return () => URL.revokeObjectURL(url)
  }, [source.file, source.kind])
  return source.kind === 'url' ? source.url.trim() || null : fileUrl
}

function sourceType(source: SourceSelection): 'pdf' | 'image' | 'tiff' | 'video' | 'office' | 'unknown' {
  const filename = source.kind === 'file' ? source.file?.name ?? '' : source.url
  const type = source.kind === 'file' ? source.file?.type ?? '' : ''
  const path = filename.toLowerCase().split(/[?#]/)[0]
  if (type === 'application/pdf' || path.endsWith('.pdf')) return 'pdf'
  if (type === 'image/tiff' || /\.tiff?$/.test(path)) return 'tiff'
  if (type.startsWith('image/') || /\.(png|jpe?g|webp|bmp)$/.test(path)) return 'image'
  if (type.startsWith('video/') || /\.(mp4|mov|mkv|webm|avi)$/.test(path)) return 'video'
  if (/\.(docx|pptx)$/.test(path)) return 'office'
  return 'unknown'
}

export function DocumentPreview({ source, activePage, onPageChange, onMetadata }: DocumentPreviewProps) {
  const sourceUrl = useSourceUrl(source)
  const kind = useMemo(() => sourceType(source), [source])
  const stageRef = useRef<HTMLDivElement>(null)
  const documentRef = useRef<HTMLDivElement>(null)
  const pageRef = useRef(1)
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null)
  const [page, setPage] = useState(1)
  const [pageWidth, setPageWidth] = useState(0)
  const [scale, setScale] = useState(1)
  const [rotation, setRotation] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setPdf(null)
    pageRef.current = 1
    setPage(1)
    setPageWidth(0)
    setScale(1)
    setRotation(0)
    setLoading(false)
    setError(null)
    onMetadata?.(null)
    if (!sourceUrl || kind !== 'pdf') return
    let disposed = false
    setLoading(true)
    const task = getDocument({ url: sourceUrl })
    void task.promise.then((document) => {
      if (disposed) return
      setPdf(document)
      onMetadata?.(document.numPages)
      setLoading(false)
    }).catch((reason: unknown) => {
      if (disposed) return
      setError(source.kind === 'url'
        ? '浏览器无法加载该 PDF 预览（可能受跨域限制），服务端仍可解析此 URL。'
        : reason instanceof Error ? reason.message : 'PDF 预览加载失败')
      setLoading(false)
    })
    return () => {
      disposed = true
      void task.destroy()
    }
  }, [kind, onMetadata, source.kind, sourceUrl])

  useEffect(() => {
    if (!pdf || !documentRef.current) return
    const documentElement = documentRef.current
    let frame = 0
    let resizeTimer = 0
    let measured = false
    const measure = () => {
      window.cancelAnimationFrame(frame)
      frame = window.requestAnimationFrame(() => {
        const width = documentElement.getBoundingClientRect().width
        if (width <= 0) return
        const nextWidth = Math.floor(width)
        if (!measured) {
          measured = true
          setPageWidth(nextWidth)
          return
        }
        window.clearTimeout(resizeTimer)
        resizeTimer = window.setTimeout(() => setPageWidth(nextWidth), 120)
      })
    }
    measure()
    if (typeof ResizeObserver === 'undefined') {
      window.addEventListener('resize', measure)
      return () => {
        window.cancelAnimationFrame(frame)
        window.clearTimeout(resizeTimer)
        window.removeEventListener('resize', measure)
      }
    }
    const observer = new ResizeObserver(measure)
    observer.observe(documentElement)
    return () => {
      window.cancelAnimationFrame(frame)
      window.clearTimeout(resizeTimer)
      observer.disconnect()
    }
  }, [pdf, scale])

  const scrollToPage = useCallback((targetPage: number, behavior: ScrollBehavior = 'smooth') => {
    const stage = stageRef.current
    const target = stage?.querySelector<HTMLElement>(`[data-pdf-page="${targetPage}"]`)
    if (!stage || !target) return
    const top = target.getBoundingClientRect().top - stage.getBoundingClientRect().top + stage.scrollTop - 12
    stage.scrollTo({ top: Math.max(0, top), behavior })
  }, [])

  useEffect(() => {
    if (!activePage || !pdf) return
    const next = Math.max(1, Math.min(pdf.numPages, activePage))
    if (next === pageRef.current) return
    pageRef.current = next
    setPage(next)
    const frame = window.requestAnimationFrame(() => scrollToPage(next))
    return () => window.cancelAnimationFrame(frame)
  }, [activePage, pdf, scrollToPage])

  useEffect(() => {
    const stage = stageRef.current
    if (!stage || !pdf) return
    let frame = 0
    const updateVisiblePage = () => {
      window.cancelAnimationFrame(frame)
      frame = window.requestAnimationFrame(() => {
        const stageBounds = stage.getBoundingClientRect()
        let visiblePage = pageRef.current
        let greatestOverlap = 0
        stage.querySelectorAll<HTMLElement>('[data-pdf-page]').forEach((element) => {
          const bounds = element.getBoundingClientRect()
          const overlap = Math.max(0, Math.min(bounds.bottom, stageBounds.bottom) - Math.max(bounds.top, stageBounds.top))
          if (overlap > greatestOverlap) {
            greatestOverlap = overlap
            visiblePage = Number(element.dataset.pdfPage) || visiblePage
          }
        })
        if (visiblePage !== pageRef.current) {
          pageRef.current = visiblePage
          setPage(visiblePage)
          onPageChange?.(visiblePage)
        }
      })
    }
    stage.addEventListener('scroll', updateVisiblePage, { passive: true })
    updateVisiblePage()
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(updateVisiblePage)
    observer?.observe(stage)
    return () => {
      window.cancelAnimationFrame(frame)
      stage.removeEventListener('scroll', updateVisiblePage)
      observer?.disconnect()
    }
  }, [onPageChange, pdf])

  const goTo = (next: number) => {
    if (!pdf) return
    const safe = Math.max(1, Math.min(pdf.numPages, Math.round(next) || 1))
    pageRef.current = safe
    setPage(safe)
    onPageChange?.(safe)
    window.requestAnimationFrame(() => scrollToPage(safe))
  }

  const empty = !sourceUrl
  const sourceName = source.kind === 'file' ? source.file?.name : source.url.trim()
  const zoomLabel = Math.abs(scale - 1) < .01 ? '适宽' : `${Math.round(scale * 100)}%`

  return (
    <article className="document-preview panel-surface" aria-labelledby="preview-heading">
      <div className="panel-header">
        <div className="panel-heading-copy">
          <span className="eyebrow stage-eyebrow">03 · 原始内容</span>
          <h2 id="preview-heading">内容预览</h2>
          <p title={sourceName || undefined}>{sourceName || '等待选择内容'}</p>
        </div>
        {pdf && (
          <div className="preview-controls" aria-label="PDF 预览控制">
            <button type="button" className="icon-button" onClick={() => goTo(page - 1)} disabled={page <= 1} aria-label="上一页"><ChevronLeftIcon /></button>
            <label className="page-control"><span className="sr-only">当前页</span><input type="number" min={1} max={pdf.numPages} value={page} onChange={(event) => goTo(Number(event.target.value))} aria-label="当前页" /> <i>/ {pdf.numPages}</i></label>
            <button type="button" className="icon-button" onClick={() => goTo(page + 1)} disabled={page >= pdf.numPages} aria-label="下一页"><ChevronRightIcon /></button>
            <button type="button" className="zoom-button" onClick={() => setScale((value) => Math.max(.5, value - .15))} aria-label="缩小">−</button>
            <span className="zoom-value" title="相对于容器适宽">{zoomLabel}</span>
            <button type="button" className="zoom-button" onClick={() => setScale((value) => Math.min(2.5, value + .15))} aria-label="放大">+</button>
            <button type="button" className="icon-button" onClick={() => setRotation((value) => (value + 90) % 360)} aria-label="顺时针旋转"><RotateIcon /></button>
          </div>
        )}
        {!pdf && sourceUrl && kind === 'image' && (
          <div className="preview-controls" aria-label="图片预览控制">
            <button type="button" className="zoom-button" onClick={() => setScale((value) => Math.max(.5, value - .15))} aria-label="缩小">−</button>
            <span className="zoom-value">{Math.round(scale * 100)}%</span>
            <button type="button" className="zoom-button" onClick={() => setScale((value) => Math.min(2.5, value + .15))} aria-label="放大">+</button>
            <button type="button" className="icon-button" onClick={() => setRotation((value) => (value + 90) % 360)} aria-label="顺时针旋转"><RotateIcon /></button>
          </div>
        )}
      </div>
      <div ref={stageRef} className={`preview-stage ${pdf ? 'preview-stage-pdf' : ''}`}>
        {empty && (
          <WorkspaceState variant="input-empty" title="预览区等待内容" description="上传 PDF、图片或视频后，可在这里检查原始内容。" />
        )}
        {loading && <WorkspaceState variant="loading" description="正在加载 PDF…" role="status" live="polite" busy />}
        {error && !loading && <WorkspaceState variant="error" title="无法显示预览" description={error} role="alert" live="assertive" />}
        {sourceUrl && kind === 'pdf' && pdf && !error && (
          <div
            ref={documentRef}
            className="pdf-document"
            style={{ width: `${scale * 100}%` }}
            aria-label={`PDF 连续预览，共 ${pdf.numPages} 页`}
          >
            {Array.from({ length: pdf.numPages }, (_, index) => {
              const pageNumber = index + 1
              return (
                <PdfPageCanvas
                  key={`${sourceUrl}-${pageNumber}`}
                  pdf={pdf}
                  pageNumber={pageNumber}
                  pageWidth={pageWidth}
                  rotation={rotation}
                  current={pageNumber === page}
                  scrollRootRef={stageRef}
                />
              )
            })}
          </div>
        )}
        {sourceUrl && kind === 'image' && <img src={sourceUrl} alt="待解析文档预览" style={{ transform: `scale(${scale}) rotate(${rotation}deg)` }} />}
        {sourceUrl && kind === 'video' && <video src={sourceUrl} controls preload="metadata" aria-label="待解析视频预览" />}
        {sourceUrl && kind === 'tiff' && (
          <WorkspaceState variant="info" title="TIFF 已选择" description="浏览器不原生显示 TIFF，服务端仍会正常解析。" />
        )}
        {sourceUrl && kind === 'office' && (
          <WorkspaceState variant="info" title="Office 文档已选择" description="DOCX / PPTX 不在浏览器中强制预览；解析完成后展示结构化结果与媒体资产。" />
        )}
        {sourceUrl && kind === 'unknown' && (
          <WorkspaceState variant="error" title="不支持预览该格式" description="服务支持 PDF、DOCX、PPTX、常见图片与独立视频。" role="alert" live="assertive" />
        )}
      </div>
    </article>
  )
}
