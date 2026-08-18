import { useEffect, useMemo, useRef, useState } from 'react'
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
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null)
  const [page, setPage] = useState(1)
  const [scale, setScale] = useState(1.1)
  const [rotation, setRotation] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setPdf(null)
    setPage(1)
    setRotation(0)
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
    if (!activePage || !pdf) return
    const next = Math.max(1, Math.min(pdf.numPages, activePage))
    setPage(next)
  }, [activePage, pdf])

  useEffect(() => {
    if (!pdf || !canvasRef.current) return
    let disposed = false
    let renderTask: ReturnType<Awaited<ReturnType<PDFDocumentProxy['getPage']>>['render']> | undefined
    void pdf.getPage(page).then((pdfPage) => {
      if (disposed || !canvasRef.current) return
      const viewport = pdfPage.getViewport({ scale, rotation })
      const ratio = Math.min(window.devicePixelRatio || 1, 2)
      const canvas = canvasRef.current
      const context = canvas.getContext('2d')
      if (!context) return
      canvas.width = Math.floor(viewport.width * ratio)
      canvas.height = Math.floor(viewport.height * ratio)
      canvas.style.width = `${Math.floor(viewport.width)}px`
      canvas.style.height = `${Math.floor(viewport.height)}px`
      renderTask = pdfPage.render({ canvasContext: context, viewport, transform: ratio === 1 ? undefined : [ratio, 0, 0, ratio, 0, 0] })
      return renderTask.promise
    }).catch((reason: unknown) => {
      if (!disposed && !(reason instanceof Error && reason.name === 'RenderingCancelledException')) {
        setError(reason instanceof Error ? reason.message : '页面渲染失败')
      }
    })
    return () => {
      disposed = true
      renderTask?.cancel()
    }
  }, [page, pdf, rotation, scale])

  const goTo = (next: number) => {
    if (!pdf) return
    const safe = Math.max(1, Math.min(pdf.numPages, Math.round(next) || 1))
    setPage(safe)
    onPageChange?.(safe)
  }

  const empty = !sourceUrl
  const sourceName = source.kind === 'file' ? source.file?.name : source.url.trim()
  return (
    <article className="document-preview panel-surface" aria-labelledby="preview-heading">
      <div className="panel-header">
        <div className="panel-heading-copy">
          <span className="eyebrow">03 · 原始内容</span>
          <h2 id="preview-heading">内容预览</h2>
          <p title={sourceName || undefined}>{sourceName || '等待选择内容'}</p>
        </div>
        {pdf && (
          <div className="preview-controls" aria-label="PDF 预览控制">
            <button type="button" className="icon-button" onClick={() => goTo(page - 1)} disabled={page <= 1} aria-label="上一页"><ChevronLeftIcon /></button>
            <label className="page-control"><span className="sr-only">当前页</span><input type="number" min={1} max={pdf.numPages} value={page} onChange={(event) => goTo(Number(event.target.value))} /> <i>/ {pdf.numPages}</i></label>
            <button type="button" className="icon-button" onClick={() => goTo(page + 1)} disabled={page >= pdf.numPages} aria-label="下一页"><ChevronRightIcon /></button>
            <button type="button" className="zoom-button" onClick={() => setScale((value) => Math.max(.5, value - .15))} aria-label="缩小">−</button>
            <span className="zoom-value">{Math.round(scale * 100)}%</span>
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
      <div className="preview-stage">
        {empty && (
          <WorkspaceState variant="input-empty" title="预览区等待内容" description="上传 PDF、图片或视频后，可在这里检查原始内容。" />
        )}
        {loading && <WorkspaceState variant="loading" description="正在加载 PDF…" role="status" live="polite" busy />}
        {error && !loading && <WorkspaceState variant="error" title="无法显示预览" description={error} role="alert" live="assertive" />}
        {sourceUrl && kind === 'pdf' && !error && <canvas ref={canvasRef} className={loading ? 'hidden' : ''} aria-label={`PDF 第 ${page} 页`} />}
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
