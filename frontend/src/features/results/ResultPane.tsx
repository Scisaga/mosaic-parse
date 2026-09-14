import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { API_BASE, getApiKey } from '../../api/client'
import { CopyIcon, DownloadIcon, TrashIcon } from '../../components/Icons'
import { MarkdownRenderer } from '../../components/MarkdownRenderer'
import { WorkspaceState } from '../../components/WorkspaceState'
import { curlExample, pythonExample } from '../../lib/apiExample'
import { formatBytes, safeFilename } from '../../lib/format'
import type { ContentAsset, ContentJob, ParseOptions, ParseResult, ResultBundle, SourceSelection } from '../../types/api'

type ResultTab = 'preview' | 'source' | 'assets' | 'api'

interface ResultPaneProps {
  source: SourceSelection
  options: ParseOptions
  job: ContentJob | null
  bundle?: ResultBundle
  syncResult?: ParseResult | null
  loading?: boolean
  error?: string | null
  onClear?: () => void
}

function downloadText(content: string, filename: string) {
  const url = URL.createObjectURL(new Blob([content], { type: 'text/markdown;charset=utf-8' }))
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}

function useAssetBlob(asset: ContentAsset): string | null {
  const [url, setUrl] = useState<string | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    let objectUrl: string | null = null
    const apiKey = getApiKey()
    void fetch(`${API_BASE}${asset.download_url}`, {
      credentials: 'same-origin',
      headers: apiKey ? { 'X-API-Key': apiKey } : {},
      signal: controller.signal,
    }).then((response) => {
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      return response.blob()
    }).then((blob) => {
      if (!controller.signal.aborted) {
        objectUrl = URL.createObjectURL(blob)
        setUrl(objectUrl)
      }
    }).catch(() => undefined)
    return () => {
      controller.abort()
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [asset.download_url])
  return url
}

function AssetMedia({ asset }: { asset: ContentAsset }) {
  const url = useAssetBlob(asset)
  if (!url) return <div className="asset-media-loading">正在读取受保护资产…</div>
  if (asset.kind === 'video') return <video src={url} controls preload="metadata" />
  return <img src={url} alt={asset.visual_analysis?.summary || asset.filename} loading="lazy" />
}

function AssetGallery({ assets }: { assets: ContentAsset[] }) {
  if (assets.length === 0) {
    return <WorkspaceState variant="output-empty" contentKind="text" title="没有媒体资产" description="此内容未产生独立图片、嵌入图片或视频关键帧。" />
  }
  const previews = new Map(
    assets
      .filter((asset) => asset.kind === 'image' && asset.role === 'preview' && asset.parent_asset_id)
      .map((asset) => [asset.parent_asset_id as string, asset]),
  )
  const visible = assets.filter((asset) => asset.role !== 'preview')
  return (
    <div className="asset-browser">
      <section className="image-asset-gallery">
        <h3>嵌入媒体与派生资产</h3>
        <p>Markdown 通过资产 ID 引用这些文件；下载地址继续由受保护接口提供。</p>
        <div className="asset-grid">
          {visible.map((asset) => {
            const mediaAsset = previews.get(asset.asset_id) ?? asset
            return (
              <article className="asset-card" key={asset.asset_id}>
                <AssetMedia asset={mediaAsset} />
                <div><strong>{asset.filename}</strong><span>{asset.role} · {formatBytes(asset.size_bytes)}</span></div>
                <p>{asset.visual_analysis?.detailed_description || asset.visual_analysis?.summary || '已保留资产，未生成描述'}</p>
                <small>{asset.asset_id}</small>
              </article>
            )
          })}
        </div>
      </section>
    </div>
  )
}

export function ResultPane({ source, options, job, bundle, syncResult, loading = false, error, onClear }: ResultPaneProps) {
  const [tab, setTab] = useState<ResultTab>('preview')
  const [codeKind, setCodeKind] = useState<'curl' | 'python'>('curl')
  const [copied, setCopied] = useState(false)
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([])
  const result = bundle?.result ?? syncResult ?? undefined
  const markdown = bundle?.markdown ?? result?.markdown ?? ''
  const filename = result?.filename ?? job?.filename ?? source.file?.name
  const hasResult = Boolean(markdown)
  const origin = useMemo(() => {
    const browserOrigin = typeof window === 'undefined' ? 'http://localhost:12303' : window.location.origin
    if (!API_BASE) return browserOrigin
    return /^https?:\/\//.test(API_BASE) ? API_BASE : `${browserOrigin}${API_BASE}`
  }, [])
  const example = codeKind === 'curl' ? curlExample(source, options, origin) : pythonExample(source, options, origin)

  useEffect(() => { if (bundle || syncResult) setTab('preview') }, [bundle, syncResult])

  const copy = async () => {
    const value = tab === 'api' ? example : markdown
    if (!value) return
    try { await navigator.clipboard.writeText(value) } catch { /* clipboard may be unavailable */ }
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1600)
  }
  const tabs: Array<{ id: ResultTab; label: string }> = [
    { id: 'preview', label: '内容预览' },
    { id: 'source', label: 'Markdown 源文' },
    { id: 'assets', label: `媒体${result?.assets.length ? ` ${result.assets.length}` : ''}` },
    { id: 'api', label: 'API 示例' },
  ]
  const onTabKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    let next = index
    if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') next = (index - 1 + tabs.length) % tabs.length
    else if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = (index + 1) % tabs.length
    else if (event.key === 'Home') next = 0
    else if (event.key === 'End') next = tabs.length - 1
    else return
    event.preventDefault()
    setTab(tabs[next].id)
    tabRefs.current[next]?.focus()
  }

  return (
    <article className="result-pane panel-surface" aria-labelledby="result-heading">
      <div className="panel-header result-header">
        <div className="panel-heading-copy"><span className="eyebrow stage-eyebrow">04 · 解析输出</span><h2 id="result-heading">Markdown</h2><p title={filename || undefined}>{filename || '结果将在任务完成后显示'}</p></div>
        <div className="result-actions">
          <button type="button" className="action-button" onClick={() => void copy()} disabled={!hasResult && tab !== 'api'}><CopyIcon /> {copied ? '已复制' : '复制'}</button>
          <button type="button" className="action-button" onClick={() => downloadText(markdown, safeFilename(filename, 'result.md'))} disabled={!hasResult}><DownloadIcon /> .md</button>
          {onClear && <button type="button" className="action-button" onClick={onClear} disabled={!hasResult && !job}><TrashIcon /> 清除</button>}
        </div>
      </div>
      <div className="result-tabs" role="tablist" aria-label="Markdown 视图">
        {tabs.map((item, index) => <button ref={(node) => { tabRefs.current[index] = node }} id={`result-${item.id}-tab`} type="button" role="tab" aria-selected={tab === item.id} aria-controls="result-tabpanel" tabIndex={tab === item.id ? 0 : -1} className={tab === item.id ? 'active' : ''} onClick={() => setTab(item.id)} onKeyDown={(event) => onTabKeyDown(event, index)} key={item.id}>{item.label}</button>)}
      </div>
      <div id="result-tabpanel" role="tabpanel" aria-labelledby={`result-${tab}-tab`} tabIndex={0} className={`result-content result-${tab}`}>
        {loading && <WorkspaceState variant="loading" description="正在读取 Markdown…" role="status" live="polite" busy />}
        {error && !loading && <WorkspaceState variant="error" title="结果读取失败" description={error} role="alert" live="assertive" />}
        {!loading && !error && tab === 'preview' && (markdown ? <div className="markdown-body"><MarkdownRenderer>{markdown}</MarkdownRenderer></div> : <WorkspaceState variant="output-empty" contentKind="markdown" title="等待 Markdown" description="解析完成后，此处展示正文、列表、表格和媒体引用。" />)}
        {!loading && !error && tab === 'source' && (markdown ? <pre className="plain-text">{markdown}</pre> : <WorkspaceState variant="output-empty" contentKind="text" title="等待 Markdown 源文" description="结果接口只返回这一份 GFM Markdown。" />)}
        {!loading && !error && tab === 'assets' && <AssetGallery assets={result?.assets ?? []} />}
        {!loading && !error && tab === 'api' && <div className="api-example"><div className="code-kind-tabs"><button type="button" className={codeKind === 'curl' ? 'active' : ''} onClick={() => setCodeKind('curl')}>curl</button><button type="button" className={codeKind === 'python' ? 'active' : ''} onClick={() => setCodeKind('python')}>Python</button></div><p>创建任务返回控制 JSON；任务完成后，`GET /result` 直接返回 `text/markdown`。</p><pre><code>{example}</code></pre></div>}
      </div>
    </article>
  )
}
