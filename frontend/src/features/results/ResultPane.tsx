import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { API_BASE, getApiKey } from '../../api/client'
import { CopyIcon, DownloadIcon, TrashIcon, WarningIcon } from '../../components/Icons'
import { MarkdownRenderer } from '../../components/MarkdownRenderer'
import { WorkspaceState } from '../../components/WorkspaceState'
import { curlExample, pythonExample } from '../../lib/apiExample'
import { formatBytes, formatDuration, safeFilename } from '../../lib/format'
import type { ContentAsset, ContentJob, ContentParseResult, ContentUnit, ParseOptions, ParseResult, ResultBundle, SourceSelection } from '../../types/api'

type ResultTab = 'overview' | 'markdown' | 'text' | 'units' | 'assets' | 'api'

interface ResultPaneProps {
  source: SourceSelection
  options: ParseOptions
  job: ContentJob | null
  bundle?: ResultBundle
  syncResult?: ParseResult | null
  loading?: boolean
  error?: string | null
  selectedPage?: number | null
  onSelectPage: (page: number) => void
  onRetryPage?: (page: number) => void
  onClear?: () => void
}

function downloadText(content: string, filename: string, type: string) {
  const url = URL.createObjectURL(new Blob([content], { type }))
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}

function unitName(unit: ContentUnit, compact = false): string {
  if (unit.unit_type === 'page') return compact ? `P.${unit.index}` : `第 ${unit.index} 页`
  if (unit.unit_type === 'slide') return compact ? `S.${unit.index}` : `第 ${unit.index} 张幻灯片`
  if (unit.unit_type === 'image') return compact ? 'IMG' : '图片'
  if (unit.unit_type === 'video') return compact ? 'VIDEO' : '视频'
  return compact ? `U.${unit.index}` : '文档正文'
}

function MarkdownContent({ markdown, units, selectedPage }: { markdown: string; units: ContentUnit[]; selectedPage?: number | null }) {
  if (selectedPage) {
    const unit = units.find((candidate) => candidate.index === selectedPage)
    if (unit) return (
      <section id={`result-unit-${unit.index}`} className="result-page selected">
        <div className="result-page-label">{unitName(unit).toUpperCase()}</div>
        {unit.renderings.markdown
          ? <MarkdownRenderer>{unit.renderings.markdown}</MarkdownRenderer>
          : <p className="page-no-output">此单元未生成 Markdown 派生视图。</p>}
      </section>
    )
  }
  return <MarkdownRenderer>{markdown}</MarkdownRenderer>
}

function UnitStatusList({ units, onSelectPage, onRetryPage }: { units: ContentUnit[]; onSelectPage: (page: number) => void; onRetryPage?: (page: number) => void }) {
  if (units.length === 0) {
    return <WorkspaceState variant="pages-empty" title="暂无内容单元" description="解析服务尚未生成单元级结果。" />
  }
  return (
    <div className="page-status-list">
      <div className="page-status-head"><span>单元</span><span>状态</span><span>策略</span><span>耗时</span><span>质量 / 操作</span></div>
      {units.map((unit) => (
        <div className="page-status-row" key={unit.unit_id}>
          <button type="button" className="page-status-main" onClick={() => onSelectPage(unit.index)} aria-label={`查看${unitName(unit)}结果`}>
            <strong>{unitName(unit, true)}</strong>
            <span><small>状态</small><i className={`page-dot page-${unit.status}`} />{unit.status}</span>
            <span title={unit.diagnostics.selected_strategy}><small>策略</small>{unit.diagnostics.selected_strategy}</span>
            <span><small>耗时</small>{formatDuration(unit.duration_ms)}</span>
          </button>
          <div className="page-warning-cell">
            {unit.diagnostics.warning_codes.length > 0
              ? <><WarningIcon /> {unit.diagnostics.warning_codes.length}</>
              : unit.diagnostics.quality_verdict}
            {(unit.status === 'failed' || unit.status === 'warning') && onRetryPage && unit.unit_type !== 'video' && (
              <button type="button" className="inline-retry" onClick={() => onRetryPage(unit.index)} aria-label={`重试${unitName(unit)}`}>重试</button>
            )}
          </div>
        </div>
      ))}
    </div>
  )
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

function AssetMedia({ asset, video = false }: { asset: ContentAsset; video?: boolean }) {
  const url = useAssetBlob(asset)
  if (!url) return <div className="asset-media-loading">正在读取受保护资产…</div>
  if (video || asset.kind === 'video') return <video src={url} controls preload="metadata" />
  return <img src={url} alt={asset.visual_analysis?.summary || asset.filename} loading="lazy" />
}

function AssetGallery({ result }: { result: ContentParseResult }) {
  if (result.assets.length === 0) {
    return <WorkspaceState variant="output-empty" contentKind="text" title="没有媒体资产" description="此内容未产生独立图片、嵌入图片或视频关键帧。" />
  }
  const sourceVideo = result.assets.find((asset) => asset.kind === 'video' && asset.role === 'source')
  const keyframeIds = new Set(result.video_analysis?.keyframes.map((frame) => frame.asset_id) ?? [])
  const previews = new Map(
    result.assets
      .filter((asset) => asset.kind === 'image' && asset.role === 'preview' && asset.parent_asset_id)
      .map((asset) => [asset.parent_asset_id as string, asset]),
  )
  const gallery = result.assets.filter((asset) => (
    asset.kind === 'image'
    && asset.role !== 'preview'
    && !keyframeIds.has(asset.asset_id)
  ))
  return (
    <div className="asset-browser">
      {sourceVideo && (
        <section className="video-player-panel">
          <h3>独立视频</h3>
          <AssetMedia asset={sourceVideo} video />
          <p>{result.video_analysis?.summary}</p>
          <small>摘要仅基于下方采样帧，不代表未采样内容。</small>
        </section>
      )}
      {result.video_analysis && (
        <section className="keyframe-timeline">
          <h3>关键帧时间线</h3>
          <div className="keyframe-track">
            {result.video_analysis.keyframes.map((frame) => {
              const asset = result.assets.find((candidate) => candidate.asset_id === frame.asset_id)
              return asset ? (
                <article className="keyframe-card" key={frame.asset_id}>
                  <AssetMedia asset={asset} />
                  <strong>{(frame.timestamp_ms / 1000).toFixed(3)}s</strong>
                  <p>{frame.visual_analysis.summary}</p>
                </article>
              ) : null
            })}
          </div>
        </section>
      )}
      {gallery.length > 0 && (
        <section className="image-asset-gallery">
          <h3>图片资产</h3>
          <div className="asset-grid">
            {gallery.map((asset) => {
              const mediaAsset = previews.get(asset.asset_id) ?? asset
              return (
                <article className="asset-card" key={asset.asset_id}>
                  <AssetMedia asset={mediaAsset} />
                  <div><strong>{asset.filename}</strong><span>{asset.role} · {formatBytes(asset.size_bytes)}</span></div>
                  <p>{asset.visual_analysis?.detailed_description || asset.visual_analysis?.summary || '未生成图片描述'}</p>
                </article>
              )
            })}
          </div>
        </section>
      )}
    </div>
  )
}

function ResultOverview({ result }: { result: ContentParseResult }) {
  const counts = useMemo(() => ({
    regions: result.units.reduce((total, unit) => total + unit.regions.length, 0),
    blocks: result.units.reduce((total, unit) => total + unit.blocks.length, 0),
    cells: result.tables.reduce((total, table) => total + table.cells.length, 0),
  }), [result])
  const provenance = useMemo(() => {
    const sources = new Set<string>()
    result.units.forEach((unit) => unit.blocks.forEach((block) => {
      if (block.provenance.selected_source) sources.add(block.provenance.selected_source)
      block.provenance.supporting_sources.forEach((source) => sources.add(source))
    }))
    result.tables.forEach((table) => table.cells.forEach((cell) => {
      if (cell.provenance.selected_source) sources.add(cell.provenance.selected_source)
      cell.provenance.supporting_sources.forEach((source) => sources.add(source))
    }))
    return [...sources]
  }, [result])
  const sourceHash = result.source.source_sha256
  const shortHash = sourceHash.length > 18 ? `${sourceHash.slice(0, 12)}…${sourceHash.slice(-6)}` : sourceHash
  return (
    <div className="result-overview">
      <div className="result-summary-head">
        <div><span className={`result-status result-status-${result.status}`}>{result.status}</span><h3>解析摘要</h3><p>{result.source.filename} · {result.source.kind.toUpperCase()} · {formatBytes(result.source.size_bytes)}</p></div>
      </div>
      <div className="result-metrics" aria-label="解析结果规模">
        <div className="result-metric"><strong>{result.units.length} / {result.source.unit_count}</strong><span>内容单元</span></div>
        <div className="result-metric"><strong>{counts.regions}</strong><span>版面区域</span></div>
        <div className="result-metric"><strong>{counts.blocks}</strong><span>文本块</span></div>
        <div className="result-metric"><strong>{result.assets.length}</strong><span>媒体</span></div>
        <div className="result-metric"><strong>{result.tables.length}</strong><span>表格</span></div>
        <div className="result-metric"><strong>{counts.cells}</strong><span>表格单元格</span></div>
      </div>
      <section className="result-section" aria-labelledby="quality-summary-heading">
        <div className="result-section-heading"><h4 id="quality-summary-heading">解析质量</h4><span>{result.warnings.length} 条告警 · {result.diagnostics.unresolved_visual_conflicts} 个未解决视觉冲突</span></div>
        <div className="result-quality">
          <div className="quality-trusted"><strong>{result.diagnostics.trusted_units}</strong><span>可信单元</span></div>
          <div className="quality-degraded"><strong>{result.diagnostics.degraded_units}</strong><span>降级单元</span></div>
          <div className="quality-untrusted"><strong>{result.diagnostics.untrusted_units}</strong><span>不可信单元</span></div>
          <div><strong>{result.diagnostics.visual_units}</strong><span>视觉单元</span></div>
          <div><strong>{result.diagnostics.repaired_units}</strong><span>确定性修复</span></div>
        </div>
      </section>
      <section className="result-section" aria-labelledby="provenance-summary-heading">
        <div className="result-section-heading"><h4 id="provenance-summary-heading">来源追踪</h4><span>{provenance.length ? provenance.join(' · ') : '暂无块级来源记录'}</span></div>
        <dl className="result-details">
          <div><dt>内容 ID</dt><dd title={result.source.content_id}>{result.source.content_id}</dd></div>
          <div><dt>源文件 SHA-256</dt><dd title={sourceHash}>{shortHash}</dd></div>
          <div><dt>主解析后端</dt><dd>{result.runtime.primary_backend}</dd></div>
          <div><dt>OCR / 视觉后端</dt><dd>{result.runtime.ocr_backend || '未使用'} / {result.runtime.visual_backend || '未使用'}</dd></div>
          <div><dt>质量档位</dt><dd>{result.runtime.profile}</dd></div>
          <div><dt>总耗时</dt><dd>{formatDuration(result.runtime.duration_ms)}</dd></div>
        </dl>
      </section>
      <details className="result-technical"><summary>技术信息</summary><dl><div><dt>Object</dt><dd>{result.object}</dd></div><div><dt>Schema</dt><dd>{result.schema_version}</dd></div><div><dt>Parser</dt><dd>{result.runtime.parser_version}</dd></div></dl></details>
    </div>
  )
}

export function ResultPane({ source, options, job, bundle, syncResult, loading = false, error, selectedPage, onSelectPage, onRetryPage, onClear }: ResultPaneProps) {
  const [tab, setTab] = useState<ResultTab>('overview')
  const [codeKind, setCodeKind] = useState<'curl' | 'python'>('curl')
  const [copied, setCopied] = useState(false)
  const contentRef = useRef<HTMLDivElement>(null)
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([])
  const result = bundle?.result ?? syncResult ?? undefined
  const markdown = result?.renderings.markdown ?? ''
  const plainText = result?.renderings.plain_text ?? ''
  const units = result?.units ?? []
  const filename = result?.source.filename ?? job?.filename ?? source.file?.name
  const hasResult = Boolean(result)
  const origin = useMemo(() => {
    const browserOrigin = typeof window === 'undefined' ? 'http://localhost:12303' : window.location.origin
    if (!API_BASE) return browserOrigin
    return /^https?:\/\//.test(API_BASE) ? API_BASE : `${browserOrigin}${API_BASE}`
  }, [])
  const example = codeKind === 'curl' ? curlExample(source, options, origin) : pythonExample(source, options, origin)
  const serializeResult = () => result ? JSON.stringify(result, null, 2) : ''
  const selectUnit = (index: number) => {
    const unit = units.find((candidate) => candidate.index === index)
    setTab(unit?.renderings.markdown ? 'markdown' : 'overview')
    onSelectPage(index)
  }
  useEffect(() => {
    if (!selectedPage || (tab !== 'markdown' && tab !== 'text')) return
    contentRef.current?.querySelector(`#result-unit-${selectedPage}`)?.scrollIntoView({ block: 'start', behavior: 'smooth' })
  }, [selectedPage, tab])
  useEffect(() => { if (bundle || syncResult) setTab('overview') }, [bundle, syncResult])
  const copy = async () => {
    const value = tab === 'api' ? example : tab === 'text' ? plainText : tab === 'markdown' ? markdown : serializeResult()
    if (!value) return
    try { await navigator.clipboard.writeText(value) } catch { /* browser fallback is intentionally omitted */ }
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1600)
  }
  const tabs: Array<{ id: ResultTab; label: string }> = [
    { id: 'overview', label: '概览' },
    { id: 'markdown', label: 'Markdown' },
    { id: 'text', label: '纯文本' },
    { id: 'units', label: `内容单元${units.length ? ` ${units.length}` : ''}` },
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
    event.preventDefault(); setTab(tabs[next].id); tabRefs.current[next]?.focus()
  }
  return (
    <article className="result-pane panel-surface" aria-labelledby="result-heading">
      <div className="panel-header result-header">
        <div className="panel-heading-copy"><span className="eyebrow stage-eyebrow">04 · 解析输出</span><h2 id="result-heading">解析结果</h2><p title={filename || undefined}>{filename || '结果将在任务完成后显示'}</p></div>
        <div className="result-actions">
          <button type="button" className="action-button" onClick={() => void copy()} disabled={!hasResult && tab !== 'api'}><CopyIcon /> {copied ? '已复制' : '复制'}</button>
          <button type="button" className="action-button" onClick={() => downloadText(serializeResult(), safeFilename(filename, 'json'), 'application/json;charset=utf-8')} disabled={!hasResult}><DownloadIcon /> .json</button>
          <button type="button" className="action-button" onClick={() => downloadText(markdown, safeFilename(filename, 'md'), 'text/markdown;charset=utf-8')} disabled={!markdown}><DownloadIcon /> .md</button>
          <button type="button" className="action-button" onClick={() => downloadText(plainText, safeFilename(filename, 'txt'), 'text/plain;charset=utf-8')} disabled={!plainText}><DownloadIcon /> .txt</button>
          {onClear && <button type="button" className="action-button" onClick={onClear} disabled={!hasResult && !job}><TrashIcon /> 清除</button>}
        </div>
      </div>
      <div className="result-tabs" role="tablist" aria-label="解析结果视图">
        {tabs.map((item, index) => <button ref={(node) => { tabRefs.current[index] = node }} id={`result-${item.id}-tab`} type="button" role="tab" aria-selected={tab === item.id} aria-controls="result-tabpanel" tabIndex={tab === item.id ? 0 : -1} className={tab === item.id ? 'active' : ''} onClick={() => setTab(item.id)} onKeyDown={(event) => onTabKeyDown(event, index)} key={item.id}>{item.label}</button>)}
      </div>
      <div id="result-tabpanel" role="tabpanel" aria-labelledby={`result-${tab}-tab`} tabIndex={0} className={`result-content result-${tab}`} ref={contentRef}>
        {loading && <WorkspaceState variant="loading" description="正在读取解析结果…" role="status" live="polite" busy />}
        {error && !loading && <WorkspaceState variant="error" title="结果读取失败" description={error} role="alert" live="assertive" />}
        {!loading && !error && tab === 'overview' && (result ? <ResultOverview result={result} /> : <WorkspaceState variant="output-empty" contentKind="text" title="等待解析结果" description="解析完成后，此处展示摘要、质量和来源追踪。" />)}
        {!loading && !error && tab === 'markdown' && (markdown ? <div className="markdown-body"><MarkdownContent markdown={markdown} units={units} selectedPage={selectedPage} /></div> : <WorkspaceState variant="output-empty" contentKind="markdown" title="未请求 Markdown" description="可在解析设置中启用派生渲染。" />)}
        {!loading && !error && tab === 'text' && (plainText ? <pre className="plain-text">{plainText}</pre> : <WorkspaceState variant="output-empty" contentKind="text" title="未请求纯文本" description="可在解析设置中启用派生渲染。" />)}
        {!loading && !error && tab === 'units' && <UnitStatusList units={units} onSelectPage={selectUnit} onRetryPage={onRetryPage} />}
        {!loading && !error && tab === 'assets' && (result ? <AssetGallery result={result} /> : <WorkspaceState variant="output-empty" contentKind="text" title="等待媒体" description="解析完成后展示图片、视频和关键帧。" />)}
        {!loading && !error && tab === 'api' && <div className="api-example"><div className="code-kind-tabs"><button type="button" className={codeKind === 'curl' ? 'active' : ''} onClick={() => setCodeKind('curl')}>curl</button><button type="button" className={codeKind === 'python' ? 'active' : ''} onClick={() => setCodeKind('python')}>Python</button></div><p>示例返回 ContentParseResult；Markdown 与文本位于 renderings。</p><pre><code>{example}</code></pre></div>}
      </div>
      {result && result.warnings.length > 0 && <div className="document-warnings"><WarningIcon /><span>{result.warnings.length} 条解析告警</span><p>{result.warnings[0].code}</p></div>}
    </article>
  )
}
