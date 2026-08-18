import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { API_BASE, getApiKey } from '../../api/client'
import { CopyIcon, DownloadIcon, TrashIcon, WarningIcon } from '../../components/Icons'
import { MarkdownRenderer } from '../../components/MarkdownRenderer'
import { WorkspaceState } from '../../components/WorkspaceState'
import { curlExample, pythonExample } from '../../lib/apiExample'
import { formatBytes, formatDuration, safeFilename } from '../../lib/format'
import type { AssetIR, ContentEvidenceIR, ContentJob, ContentUnitIR, ParseOptions, ParseResult, ResultBundle, SourceSelection } from '../../types/api'

type ResultTab = 'evidence' | 'markdown' | 'text' | 'units' | 'assets' | 'api'

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

function unitName(unit: ContentUnitIR, compact = false): string {
  if (unit.unit_type === 'page') return compact ? `P.${unit.index}` : `第 ${unit.index} 页`
  if (unit.unit_type === 'slide') return compact ? `S.${unit.index}` : `第 ${unit.index} 张幻灯片`
  if (unit.unit_type === 'image') return compact ? 'IMG' : '图片'
  if (unit.unit_type === 'video') return compact ? 'VIDEO' : '视频'
  return compact ? `U.${unit.index}` : '文档正文'
}

function MarkdownContent({ markdown, units, selectedPage }: { markdown: string; units: ContentUnitIR[]; selectedPage?: number | null }) {
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

function UnitStatusList({ units, onSelectPage, onRetryPage }: { units: ContentUnitIR[]; onSelectPage: (page: number) => void; onRetryPage?: (page: number) => void }) {
  if (units.length === 0) {
    return <WorkspaceState variant="pages-empty" title="暂无内容单元" description="解析服务尚未生成单元级 IR。" />
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

function useAssetBlob(asset: AssetIR): string | null {
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

function AssetMedia({ asset, video = false }: { asset: AssetIR; video?: boolean }) {
  const url = useAssetBlob(asset)
  if (!url) return <div className="asset-media-loading">正在读取受保护资产…</div>
  if (video || asset.kind === 'video') return <video src={url} controls preload="metadata" />
  return <img src={url} alt={asset.visual_analysis?.summary || asset.filename} loading="lazy" />
}

function AssetGallery({ evidence }: { evidence: ContentEvidenceIR }) {
  if (evidence.assets.length === 0) {
    return <WorkspaceState variant="output-empty" contentKind="text" title="没有媒体资产" description="此内容未产生独立图片、嵌入图片或视频关键帧。" />
  }
  const sourceVideo = evidence.assets.find((asset) => asset.kind === 'video' && asset.role === 'source')
  const keyframeIds = new Set(evidence.video_analysis?.keyframes.map((frame) => frame.asset_id) ?? [])
  const previews = new Map(
    evidence.assets
      .filter((asset) => asset.kind === 'image' && asset.role === 'preview' && asset.parent_asset_id)
      .map((asset) => [asset.parent_asset_id as string, asset]),
  )
  const gallery = evidence.assets.filter((asset) => (
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
          <p>{evidence.video_analysis?.summary}</p>
          <small>摘要仅基于下方采样帧，不代表未采样内容。</small>
        </section>
      )}
      {evidence.video_analysis && (
        <section className="keyframe-timeline">
          <h3>关键帧时间线</h3>
          <div className="keyframe-track">
            {evidence.video_analysis.keyframes.map((frame) => {
              const asset = evidence.assets.find((candidate) => candidate.asset_id === frame.asset_id)
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

function EvidenceOverview({ evidence }: { evidence: ContentEvidenceIR }) {
  const counts = useMemo(() => ({
    regions: evidence.units.reduce((total, unit) => total + unit.regions.length, 0),
    blocks: evidence.units.reduce((total, unit) => total + unit.blocks.length, 0),
    cells: evidence.tables.reduce((total, table) => total + table.cells.length, 0),
  }), [evidence])
  const sourceHash = evidence.source.source_sha256
  const shortHash = sourceHash.length > 18 ? `${sourceHash.slice(0, 12)}…${sourceHash.slice(-6)}` : sourceHash
  return (
    <div className="evidence-overview">
      <div className="evidence-summary-head">
        <div><span className={`evidence-status evidence-status-${evidence.status}`}>{evidence.status}</span><h3>结构化证据摘要</h3><p><code>{evidence.schema_version}</code> · 完整 IR 不在页面中展开。</p></div>
        <span className="evidence-object">{evidence.object}</span>
      </div>
      <div className="evidence-metrics" aria-label="证据 IR 规模">
        <div className="evidence-metric"><strong>{evidence.units.length} / {evidence.source.unit_count}</strong><span>内容单元</span></div>
        <div className="evidence-metric"><strong>{counts.regions}</strong><span>版面区域</span></div>
        <div className="evidence-metric"><strong>{counts.blocks}</strong><span>文本块</span></div>
        <div className="evidence-metric"><strong>{evidence.assets.length}</strong><span>媒体资产</span></div>
        <div className="evidence-metric"><strong>{evidence.tables.length}</strong><span>物理表格</span></div>
        <div className="evidence-metric"><strong>{counts.cells}</strong><span>表格单元格</span></div>
      </div>
      <section className="evidence-section" aria-labelledby="quality-summary-heading">
        <div className="evidence-section-heading"><h4 id="quality-summary-heading">质量概览</h4><span>{evidence.warnings.length} 条告警 · {evidence.diagnostics.unresolved_visual_conflicts} 个未解决视觉冲突</span></div>
        <div className="evidence-quality">
          <div className="quality-trusted"><strong>{evidence.diagnostics.trusted_units}</strong><span>trusted</span></div>
          <div className="quality-degraded"><strong>{evidence.diagnostics.degraded_units}</strong><span>degraded</span></div>
          <div className="quality-untrusted"><strong>{evidence.diagnostics.untrusted_units}</strong><span>untrusted</span></div>
          <div><strong>{evidence.diagnostics.visual_units}</strong><span>视觉单元</span></div>
          <div><strong>{evidence.diagnostics.repaired_units}</strong><span>确定性修复</span></div>
        </div>
      </section>
      <section className="evidence-section" aria-labelledby="runtime-summary-heading">
        <div className="evidence-section-heading"><h4 id="runtime-summary-heading">内容与运行信息</h4></div>
        <dl className="evidence-details">
          <div><dt>内容 ID</dt><dd title={evidence.source.content_id}>{evidence.source.content_id}</dd></div>
          <div><dt>源文件 SHA-256</dt><dd title={sourceHash}>{shortHash}</dd></div>
          <div><dt>内容类型</dt><dd>{evidence.source.kind}</dd></div>
          <div><dt>质量档位</dt><dd>{evidence.runtime.profile}</dd></div>
          <div><dt>主解析后端</dt><dd>{evidence.runtime.primary_backend}</dd></div>
          <div><dt>OCR / 视觉后端</dt><dd>{evidence.runtime.ocr_backend || '未使用'} / {evidence.runtime.visual_backend || '未使用'}</dd></div>
          <div><dt>输入大小</dt><dd>{formatBytes(evidence.runtime.input_bytes)}</dd></div>
          <div><dt>总耗时</dt><dd>{formatDuration(evidence.runtime.duration_ms)}</dd></div>
        </dl>
      </section>
    </div>
  )
}

export function ResultPane({ source, options, job, bundle, syncResult, loading = false, error, selectedPage, onSelectPage, onRetryPage, onClear }: ResultPaneProps) {
  const [tab, setTab] = useState<ResultTab>('evidence')
  const [codeKind, setCodeKind] = useState<'curl' | 'python'>('curl')
  const [copied, setCopied] = useState(false)
  const contentRef = useRef<HTMLDivElement>(null)
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([])
  const evidence = bundle?.evidence ?? syncResult ?? undefined
  const markdown = evidence?.renderings.markdown ?? ''
  const plainText = evidence?.renderings.plain_text ?? ''
  const units = evidence?.units ?? []
  const filename = evidence?.source.filename ?? job?.filename ?? source.file?.name
  const hasResult = Boolean(evidence)
  const origin = useMemo(() => {
    const browserOrigin = typeof window === 'undefined' ? 'http://localhost:12303' : window.location.origin
    if (!API_BASE) return browserOrigin
    return /^https?:\/\//.test(API_BASE) ? API_BASE : `${browserOrigin}${API_BASE}`
  }, [])
  const example = codeKind === 'curl' ? curlExample(source, options, origin) : pythonExample(source, options, origin)
  const serializeEvidence = () => evidence ? JSON.stringify(evidence, null, 2) : ''
  const selectUnit = (index: number) => {
    const unit = units.find((candidate) => candidate.index === index)
    setTab(unit?.renderings.markdown ? 'markdown' : 'evidence')
    onSelectPage(index)
  }
  useEffect(() => {
    if (!selectedPage || (tab !== 'markdown' && tab !== 'text')) return
    contentRef.current?.querySelector(`#result-unit-${selectedPage}`)?.scrollIntoView({ block: 'start', behavior: 'smooth' })
  }, [selectedPage, tab])
  useEffect(() => { if (bundle || syncResult) setTab('evidence') }, [bundle, syncResult])
  const copy = async () => {
    const value = tab === 'api' ? example : tab === 'text' ? plainText : tab === 'markdown' ? markdown : serializeEvidence()
    if (!value) return
    try { await navigator.clipboard.writeText(value) } catch { /* browser fallback is intentionally omitted */ }
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1600)
  }
  const tabs: Array<{ id: ResultTab; label: string }> = [
    { id: 'evidence', label: '证据 IR' },
    { id: 'markdown', label: 'Markdown 派生视图' },
    { id: 'text', label: '纯文本派生视图' },
    { id: 'units', label: `内容单元${units.length ? ` ${units.length}` : ''}` },
    { id: 'assets', label: `媒体资产${evidence?.assets.length ? ` ${evidence.assets.length}` : ''}` },
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
        <div className="panel-heading-copy"><span className="eyebrow">04 · 内容证据</span><h2 id="result-heading">ContentEvidenceIR</h2><p title={filename || undefined}>{filename || '结果将在任务完成后显示'}</p></div>
        <div className="result-actions">
          <button type="button" className="action-button" onClick={() => void copy()} disabled={!hasResult && tab !== 'api'}><CopyIcon /> {copied ? '已复制' : '复制'}</button>
          <button type="button" className="action-button" onClick={() => downloadText(serializeEvidence(), safeFilename(filename, 'json'), 'application/json;charset=utf-8')} disabled={!hasResult}><DownloadIcon /> .json</button>
          <button type="button" className="action-button" onClick={() => downloadText(markdown, safeFilename(filename, 'md'), 'text/markdown;charset=utf-8')} disabled={!markdown}><DownloadIcon /> .md</button>
          <button type="button" className="action-button" onClick={() => downloadText(plainText, safeFilename(filename, 'txt'), 'text/plain;charset=utf-8')} disabled={!plainText}><DownloadIcon /> .txt</button>
          {onClear && <button type="button" className="action-button" onClick={onClear} disabled={!hasResult && !job}><TrashIcon /> 清除</button>}
        </div>
      </div>
      <div className="result-tabs" role="tablist" aria-label="解析结果视图">
        {tabs.map((item, index) => <button ref={(node) => { tabRefs.current[index] = node }} id={`result-${item.id}-tab`} type="button" role="tab" aria-selected={tab === item.id} aria-controls="result-tabpanel" tabIndex={tab === item.id ? 0 : -1} className={tab === item.id ? 'active' : ''} onClick={() => setTab(item.id)} onKeyDown={(event) => onTabKeyDown(event, index)} key={item.id}>{item.label}</button>)}
      </div>
      <div id="result-tabpanel" role="tabpanel" aria-labelledby={`result-${tab}-tab`} tabIndex={0} className={`result-content result-${tab}`} ref={contentRef}>
        {loading && <WorkspaceState variant="loading" description="正在读取结构化证据…" role="status" live="polite" busy />}
        {error && !loading && <WorkspaceState variant="error" title="结果读取失败" description={error} role="alert" live="assertive" />}
        {!loading && !error && tab === 'evidence' && (evidence ? <EvidenceOverview evidence={evidence} /> : <WorkspaceState variant="output-empty" contentKind="text" title="等待证据 IR" description="解析完成后，此处展示稳定、可追溯的内容证据结构。" />)}
        {!loading && !error && tab === 'markdown' && (markdown ? <div className="markdown-body"><MarkdownContent markdown={markdown} units={units} selectedPage={selectedPage} /></div> : <WorkspaceState variant="output-empty" contentKind="markdown" title="未请求 Markdown 派生视图" description="主结果是证据 IR；可在解析设置中启用派生渲染。" />)}
        {!loading && !error && tab === 'text' && (plainText ? <pre className="plain-text">{plainText}</pre> : <WorkspaceState variant="output-empty" contentKind="text" title="未请求纯文本派生视图" description="主结果是证据 IR；可在解析设置中启用派生渲染。" />)}
        {!loading && !error && tab === 'units' && <UnitStatusList units={units} onSelectPage={selectUnit} onRetryPage={onRetryPage} />}
        {!loading && !error && tab === 'assets' && (evidence ? <AssetGallery evidence={evidence} /> : <WorkspaceState variant="output-empty" contentKind="text" title="等待媒体资产" description="解析完成后展示图片、视频和关键帧。" />)}
        {!loading && !error && tab === 'api' && <div className="api-example"><div className="code-kind-tabs"><button type="button" className={codeKind === 'curl' ? 'active' : ''} onClick={() => setCodeKind('curl')}>curl</button><button type="button" className={codeKind === 'python' ? 'active' : ''} onClick={() => setCodeKind('python')}>Python</button></div><p>示例返回 ContentEvidenceIR；Markdown 与文本位于 renderings。</p><pre><code>{example}</code></pre></div>}
      </div>
      {evidence && evidence.warnings.length > 0 && <div className="document-warnings"><WarningIcon /><span>{evidence.warnings.length} 条证据告警</span><p>{evidence.warnings[0].code}</p></div>}
    </article>
  )
}
