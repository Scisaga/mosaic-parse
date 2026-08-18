import { useRef, useState, type DragEvent, type KeyboardEvent } from 'react'
import { FileIcon, LinkIcon, UploadIcon, CloseIcon } from '../../components/Icons'
import { formatBytes } from '../../lib/format'
import type { SourceSelection } from '../../types/api'

const ACCEPTED = '.pdf,.docx,.pptx,.png,.jpg,.jpeg,.webp,.tif,.tiff,.bmp,.mp4,.mov,.mkv,.webm,.avi,application/pdf,image/*,video/*'

interface SourcePickerProps {
  source: SourceSelection
  onChange: (source: SourceSelection) => void
  disabled?: boolean
  detectedPages?: number | null
  validationError?: string | null
  validationVisible?: boolean
}

export function SourcePicker({ source, onChange, disabled = false, detectedPages, validationError, validationVisible = false }: SourcePickerProps) {
  const inputRef = useRef<HTMLInputElement>(null)
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([])
  const [dragging, setDragging] = useState(false)
  const [urlTouched, setUrlTouched] = useState(false)

  const selectKind = (kind: SourceSelection['kind']) => {
    onChange({ ...source, kind })
  }

  const onTabKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    let next = index
    if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') next = Math.max(0, index - 1)
    else if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = Math.min(1, index + 1)
    else if (event.key === 'Home') next = 0
    else if (event.key === 'End') next = 1
    else return
    event.preventDefault()
    const kind = next === 0 ? 'file' : 'url'
    selectKind(kind)
    tabRefs.current[next]?.focus()
  }

  const acceptFile = (file?: File) => {
    if (!file) return
    onChange({ kind: 'file', file, url: '' })
  }

  const drop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault()
    setDragging(false)
    if (!disabled) acceptFile(event.dataTransfer.files[0])
  }

  return (
    <section className="source-picker" aria-labelledby="source-heading">
      <div className="section-heading-row">
        <div className="section-heading-copy">
          <span className="eyebrow stage-eyebrow">01 · 内容来源</span>
          <h2 id="source-heading">选择内容</h2>
          <p>上传文档、图片或视频，或粘贴可公开访问的内容地址。</p>
        </div>
        <div className="source-kind-tabs" role="tablist" aria-label="文档来源">
          <button id="source-file-tab" ref={(node) => { tabRefs.current[0] = node }} type="button" role="tab" aria-selected={source.kind === 'file'} aria-controls="source-file-panel" tabIndex={source.kind === 'file' ? 0 : -1} className={source.kind === 'file' ? 'active' : ''} onClick={() => selectKind('file')} onKeyDown={(event) => onTabKeyDown(event, 0)} disabled={disabled}>
            <UploadIcon /> 上传
          </button>
          <button id="source-url-tab" ref={(node) => { tabRefs.current[1] = node }} type="button" role="tab" aria-selected={source.kind === 'url'} aria-controls="source-url-panel" tabIndex={source.kind === 'url' ? 0 : -1} className={source.kind === 'url' ? 'active' : ''} onClick={() => selectKind('url')} onKeyDown={(event) => onTabKeyDown(event, 1)} disabled={disabled}>
            <LinkIcon /> URL
          </button>
        </div>
      </div>

      <div id={source.kind === 'file' ? 'source-file-panel' : 'source-url-panel'} role="tabpanel" aria-labelledby={source.kind === 'file' ? 'source-file-tab' : 'source-url-tab'}>
        {source.kind === 'file' ? (
          source.file ? (
            <div className="selected-file">
              <div className="file-symbol"><FileIcon /></div>
              <div className="file-description">
                <strong title={source.file.name}>{source.file.name}</strong>
                <span>{source.file.type || '未知类型'} · {formatBytes(source.file.size)}{detectedPages ? ` · ${detectedPages} 页` : ''}</span>
              </div>
              <button type="button" className="icon-button" onClick={() => onChange({ ...source, file: null })} disabled={disabled} aria-label="移除文档" title="移除文档"><CloseIcon /></button>
            </div>
          ) : (
            <div
              className={`drop-zone ${dragging ? 'dragging' : ''}`}
              onDragEnter={(event) => { event.preventDefault(); setDragging(true) }}
              onDragOver={(event) => event.preventDefault()}
              onDragLeave={(event) => { if (!event.currentTarget.contains(event.relatedTarget as Node)) setDragging(false) }}
              onDrop={drop}
            >
              <input ref={inputRef} type="file" accept={ACCEPTED} onChange={(event) => acceptFile(event.target.files?.[0])} disabled={disabled} />
              <div className="drop-icon"><UploadIcon /></div>
              <div>
                <strong>拖放内容到这里，或点击选择</strong>
                <p title="支持 PDF、DOCX、PPTX、常见图片与视频">PDF · Office · 图片 · 视频</p>
              </div>
              <button type="button" className="secondary-button upload-button" onClick={() => inputRef.current?.click()} disabled={disabled}>浏览文件</button>
            </div>
          )
        ) : (
          <div>
            <div className={`url-field ${(urlTouched || validationVisible) && validationError ? 'invalid' : ''}`}>
              <LinkIcon />
              <label htmlFor="source-url" className="sr-only">文档 URL</label>
              <input
                id="source-url"
                type="url"
                placeholder="https://example.com/report.pdf"
                value={source.url}
                onChange={(event) => onChange({ ...source, url: event.target.value })}
                onBlur={() => setUrlTouched(true)}
                disabled={disabled}
                autoComplete="url"
                aria-invalid={Boolean((urlTouched || validationVisible) && validationError)}
                aria-describedby={(urlTouched || validationVisible) && validationError ? 'source-url-error' : undefined}
              />
            </div>
            {(urlTouched || validationVisible) && validationError && <p id="source-url-error" className="field-error" role="alert">{validationError}</p>}
          </div>
        )}
        {source.kind === 'file' && validationVisible && validationError && <p className="field-error source-file-error" role="alert">{validationError}</p>}
      </div>
    </section>
  )
}
