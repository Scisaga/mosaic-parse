import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { API_BASE } from '../../api/client'
import { CopyIcon, DownloadIcon, TrashIcon, WarningIcon } from '../../components/Icons'
import { MarkdownRenderer } from '../../components/MarkdownRenderer'
import { WorkspaceState } from '../../components/WorkspaceState'
import { curlExample, pythonExample } from '../../lib/apiExample'
import { formatDuration, markdownToPlainText, safeFilename } from '../../lib/format'
import type { DocumentJob, PageParseResult, ParseOptions, ParseResult, ResultBundle, SourceSelection } from '../../types/api'

type ResultTab = 'markdown' | 'text' | 'pages' | 'api'

interface ResultPaneProps {
  source: SourceSelection
  options: ParseOptions
  job: DocumentJob | null
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

function splitPageSections(content: string, pattern: RegExp): Map<number, string> {
  const matches = [...content.matchAll(pattern)]
  const pages = new Map<number, string>()
  for (let index = 0; index < matches.length; index += 1) {
    const match = matches[index]
    const page = Number(match[1])
    const start = (match.index ?? 0) + match[0].length
    const end = matches[index + 1]?.index ?? content.length
    if (page > 0) pages.set(page, content.slice(start, end).trim())
  }
  return pages
}

function pagesFromExportedContent(markdown: string, text: string): PageParseResult[] {
  const markdownPages = splitPageSections(markdown, /<!--\s*page:\s*(\d+)\s*-->/gi)
  const textPages = splitPageSections(text, /^---\s*Page\s+(\d+)\s*---\s*$/gim)
  const pageNumbers = [...new Set([...markdownPages.keys(), ...textPages.keys()])].sort((left, right) => left - right)
  return pageNumbers.map((page) => ({
    page_number: page,
    status: 'unknown',
    backend: null,
    duration_ms: null,
    content: markdownPages.get(page) ?? null,
    plain_text: textPages.get(page) ?? null,
    warnings: [],
  }))
}

function MarkdownContent({ markdown, pages, selectedPage }: { markdown: string; pages: PageParseResult[]; selectedPage?: number | null }) {
  if (pages.some((page) => page.content)) {
    return <>{pages.map((page) => (
      <section id={`result-page-${page.page_number}`} className={`result-page ${selectedPage === page.page_number ? 'selected' : ''}`} key={page.page_number}>
        <div className="result-page-label">PAGE {page.page_number}</div>
        {page.content
          ? <MarkdownRenderer>{page.content}</MarkdownRenderer>
          : <p className="page-no-output">此页未返回 Markdown 内容。</p>}
      </section>
    ))}</>
  }
  return <MarkdownRenderer>{markdown}</MarkdownRenderer>
}

function PageStatusList({ pages, onSelectPage, onRetryPage }: { pages: PageParseResult[]; onSelectPage: (page: number) => void; onRetryPage?: (page: number) => void }) {
  if (pages.length === 0) {
    return <WorkspaceState variant="pages-empty" title="暂无逐页状态" description="解析服务尚未返回页面级诊断；不会用推测值填充。" />
  }
  return (
    <div className="page-status-list">
      <div className="page-status-head"><span>页码</span><span>状态</span><span>后端</span><span>耗时</span><span>告警 / 操作</span></div>
      {pages.map((page) => (
        <div className="page-status-row" key={page.page_number}>
          <button type="button" className="page-status-main" onClick={() => onSelectPage(page.page_number)} aria-label={`查看第 ${page.page_number} 页结果`}>
            <strong>P.{page.page_number}</strong>
            <span><small>状态</small><i className={`page-dot page-${page.status}`} />{page.status}</span>
            <span title={page.backend ?? undefined}><small>后端</small>{page.backend ?? '—'}</span>
            <span><small>耗时</small>{formatDuration(page.duration_ms)}</span>
          </button>
          <div className="page-warning-cell">
            {(page.warnings?.length ?? 0) > 0 ? <><WarningIcon /> {page.warnings?.length}</> : '—'}
            {(page.status === 'failed' || page.status === 'warning') && onRetryPage && (
              <button type="button" className="inline-retry" onClick={() => onRetryPage(page.page_number)} aria-label={`重试第 ${page.page_number} 页`}>重试</button>
            )}
          </div>
        </div>
      ))}
    </div>
  )
}

export function ResultPane({ source, options, job, bundle, syncResult, loading = false, error, selectedPage, onSelectPage, onRetryPage, onClear }: ResultPaneProps) {
  const [tab, setTab] = useState<ResultTab>('markdown')
  const [codeKind, setCodeKind] = useState<'curl' | 'python'>('curl')
  const [copied, setCopied] = useState(false)
  const contentRef = useRef<HTMLDivElement>(null)
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([])
  const metadata = bundle?.metadata ?? syncResult ?? undefined
  const rawMarkdown = bundle?.markdown
    ?? syncResult?.markdown
    ?? (syncResult?.output_format === 'markdown' ? syncResult.content : '')
    ?? ''
  const markdown = rawMarkdown || (syncResult?.output_format === 'text' ? syncResult.content ?? '' : '')
  const text = bundle?.text
    ?? syncResult?.plain_text
    ?? (syncResult?.output_format === 'text' ? syncResult.content : '')
    ?? markdownToPlainText(markdown)
  const metadataPages = metadata?.pages
  const jobPages = job?.pages
  const pages = useMemo(
    () => {
      const reportedPages = metadataPages ?? jobPages ?? []
      return reportedPages.length > 0 ? reportedPages : pagesFromExportedContent(markdown, text)
    },
    [jobPages, markdown, metadataPages, text],
  )
  const filename = metadata?.filename ?? job?.filename ?? source.file?.name
  const hasResult = Boolean(markdown || text)
  const origin = useMemo(() => {
    const browserOrigin = typeof window === 'undefined' ? 'http://localhost:12303' : window.location.origin
    if (!API_BASE) return browserOrigin
    return /^https?:\/\//.test(API_BASE) ? API_BASE : `${browserOrigin}${API_BASE}`
  }, [])
  const example = codeKind === 'curl' ? curlExample(source, options, origin) : pythonExample(source, options, origin)

  const selectPage = (pageNumber: number) => {
    const page = pages.find((candidate) => candidate.page_number === pageNumber)
    if (page?.content?.trim()) setTab('markdown')
    else if (page?.plain_text?.trim()) setTab('text')
    else setTab('markdown')
    onSelectPage(pageNumber)
  }

  useEffect(() => {
    if (!selectedPage || (tab !== 'markdown' && tab !== 'text')) return
    contentRef.current?.querySelector(`#result-page-${selectedPage}`)?.scrollIntoView({ block: 'start', behavior: 'smooth' })
  }, [selectedPage, tab])

  useEffect(() => {
    if (syncResult?.output_format === 'text') setTab('text')
    else if (bundle || syncResult) setTab('markdown')
  }, [bundle, syncResult])

  const copy = async () => {
    const value = tab === 'api' ? example : tab === 'text' ? text : markdown
    if (!value) return
    try {
      await navigator.clipboard.writeText(value)
    } catch {
      const textarea = document.createElement('textarea')
      textarea.value = value
      textarea.style.position = 'fixed'
      textarea.style.opacity = '0'
      document.body.appendChild(textarea)
      textarea.select()
      document.execCommand('copy')
      textarea.remove()
    }
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1600)
  }

  const tabs: Array<{ id: ResultTab; label: string }> = [
    { id: 'markdown', label: 'Markdown 预览' },
    { id: 'text', label: '纯文本' },
    { id: 'pages', label: `页面状态${pages.length ? ` ${pages.length}` : ''}` },
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
        <div className="panel-heading-copy">
          <span className="eyebrow">04 · 解析输出</span>
          <h2 id="result-heading">解析结果</h2>
          <p title={filename || undefined}>{filename || '结果将在任务完成后显示'}</p>
        </div>
        <div className="result-actions">
          <button type="button" className="action-button" onClick={() => void copy()} disabled={!hasResult && tab !== 'api'}><CopyIcon /> {copied ? '已复制' : '复制'}</button>
          <button type="button" className="action-button" onClick={() => downloadText(markdown || text, safeFilename(filename, 'md'), 'text/markdown;charset=utf-8')} disabled={!hasResult}><DownloadIcon /> .md</button>
          <button type="button" className="action-button" onClick={() => downloadText(text || markdownToPlainText(markdown), safeFilename(filename, 'txt'), 'text/plain;charset=utf-8')} disabled={!hasResult}><DownloadIcon /> .txt</button>
          {onClear && <button type="button" className="action-button" onClick={onClear} disabled={!hasResult && !job}><TrashIcon /> 清除</button>}
        </div>
      </div>
      <div className="result-tabs" role="tablist" aria-label="解析结果视图">
        {tabs.map((item, index) => <button ref={(node) => { tabRefs.current[index] = node }} id={`result-${item.id}-tab`} type="button" role="tab" aria-selected={tab === item.id} aria-controls="result-tabpanel" tabIndex={tab === item.id ? 0 : -1} className={tab === item.id ? 'active' : ''} onClick={() => setTab(item.id)} onKeyDown={(event) => onTabKeyDown(event, index)} key={item.id}>{item.label}</button>)}
      </div>
      <div id="result-tabpanel" role="tabpanel" aria-labelledby={`result-${tab}-tab`} tabIndex={0} className={`result-content result-${tab}`} ref={contentRef}>
        {loading && <WorkspaceState variant="loading" description="正在读取解析结果…" role="status" live="polite" busy />}
        {error && !loading && <WorkspaceState variant="error" title="结果读取失败" description={error} role="alert" live="assertive" />}
        {!loading && !error && tab === 'markdown' && (hasResult
          ? <div className="markdown-body"><MarkdownContent markdown={markdown} pages={pages} selectedPage={selectedPage} /></div>
          : <WorkspaceState variant="output-empty" contentKind="markdown" title="等待 Markdown 结果" description="提交解析任务后，可在这里预览结构化内容。" />)}
        {!loading && !error && tab === 'text' && (hasResult
          ? (pages.some((page) => page.plain_text || page.content)
            ? <div className="plain-pages">{pages.filter((page) => page.plain_text || page.content).map((page) => <section id={`result-page-${page.page_number}`} className={`plain-page ${selectedPage === page.page_number ? 'selected' : ''}`} key={page.page_number}><span>PAGE {page.page_number}</span><pre>{page.plain_text ?? page.content}</pre></section>)}</div>
            : <pre className="plain-text">{text}</pre>)
          : <WorkspaceState variant="output-empty" contentKind="text" title="等待纯文本结果" description="结果可直接复制或下载为 UTF-8 文本。" />)}
        {!loading && !error && tab === 'pages' && <PageStatusList pages={pages} onSelectPage={selectPage} onRetryPage={onRetryPage} />}
        {!loading && !error && tab === 'api' && (
          <div className="api-example">
            <div className="code-kind-tabs">
              <button type="button" className={codeKind === 'curl' ? 'active' : ''} onClick={() => setCodeKind('curl')}>curl</button>
              <button type="button" className={codeKind === 'python' ? 'active' : ''} onClick={() => setCodeKind('python')}>Python</button>
            </div>
            <p>示例会随当前来源、解析模式和高级参数更新。</p>
            <pre><code>{example}</code></pre>
          </div>
        )}
      </div>
      {metadata?.warnings && metadata.warnings.length > 0 && (
        <div className="document-warnings"><WarningIcon /><span>{metadata.warnings.length} 条文档告警</span><p>{metadata.warnings[0]?.message}</p></div>
      )}
    </article>
  )
}
