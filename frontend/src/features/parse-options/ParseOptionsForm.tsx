import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { createPortal } from 'react-dom'
import { CloseIcon, PlayIcon, SettingsIcon } from '../../components/Icons'
import { validatePageRange } from '../../lib/pageRange'
import type { ParseOptions } from '../../types/api'

interface ParseOptionsFormProps {
  options: ParseOptions
  onChange: (options: ParseOptions) => void
  onSubmit: () => void
  sourceReady: boolean
  busy: boolean
}

const MODES = [
  { value: 'auto', label: '自动选择' },
  { value: 'standard', label: '标准解析' },
  { value: 'ocr', label: 'OCR' },
  { value: 'vlm', label: 'VLM' },
] as const

const PROFILES = [
  { value: 'fast', label: '快速' },
  { value: 'balanced', label: '均衡' },
  { value: 'accurate', label: '精确' },
] as const

export function ParseOptionsForm({ options, onChange, onSubmit, sourceReady, busy }: ParseOptionsFormProps) {
  const [advanced, setAdvanced] = useState(false)
  const advancedButtonRef = useRef<HTMLButtonElement>(null)
  const advancedDialogRef = useRef<HTMLDivElement>(null)
  const rangeError = validatePageRange(options.pageRange)
  const update = <K extends keyof ParseOptions>(key: K, value: ParseOptions[K]) => onChange({ ...options, [key]: value })

  const closeAdvanced = useCallback(() => {
    setAdvanced(false)
    advancedButtonRef.current?.focus()
  }, [])

  useEffect(() => {
    if (!advanced) return
    const dialog = advancedDialogRef.current
    const focusable = () => [...(dialog?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), [href], [tabindex]:not([tabindex="-1"])') ?? [])]
    window.requestAnimationFrame(() => dialog?.querySelector<HTMLElement>('[data-advanced-initial]')?.focus())
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        closeAdvanced()
        return
      }
      if (event.key !== 'Tab') return
      const items = focusable()
      if (items.length === 0) return
      const first = items[0]
      const last = items[items.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [advanced, closeAdvanced])

  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (!rangeError && sourceReady && !busy) onSubmit()
  }

  return (
    <form className="options-panel" onSubmit={submit} aria-labelledby="options-heading">
      <div className="options-title-row">
        <div className="options-title section-heading-copy">
          <span className="eyebrow">02 · 解析策略</span>
          <h2 id="options-heading">解析设置</h2>
          <p>按文档特征选择解析链路，并控制输出范围与格式。</p>
        </div>
        <button ref={advancedButtonRef} className="advanced-toggle" type="button" aria-haspopup="dialog" aria-expanded={advanced} aria-controls="advanced-options-dialog" onClick={() => setAdvanced((value) => !value)}>
          <SettingsIcon /> 高级设置
        </button>
      </div>
      <div className="options-content">
        <div className="options-grid">
          <label>
            <span>解析模式</span>
            <select value={options.mode} onChange={(event) => update('mode', event.target.value as ParseOptions['mode'])} disabled={busy}>
              {MODES.map((mode) => <option value={mode.value} key={mode.value}>{mode.label}</option>)}
            </select>
          </label>
          <label>
            <span>质量档位</span>
            <select value={options.profile} onChange={(event) => update('profile', event.target.value as ParseOptions['profile'])} disabled={busy}>
              {PROFILES.map((profile) => <option value={profile.value} key={profile.value}>{profile.label}</option>)}
            </select>
          </label>
          <label>
            <span>输出格式</span>
            <select value={options.outputFormat} onChange={(event) => update('outputFormat', event.target.value as ParseOptions['outputFormat'])} disabled={busy}>
              <option value="markdown">Markdown</option>
              <option value="text">纯文本</option>
            </select>
          </label>
          <label className={rangeError ? 'invalid' : ''}>
            <span>页码范围</span>
            <input value={options.pageRange} onChange={(event) => update('pageRange', event.target.value)} placeholder="全部，如 1-5,8" disabled={busy} aria-invalid={Boolean(rangeError)} aria-describedby={rangeError ? 'page-range-error' : undefined} />
            {rangeError && <small id="page-range-error" role="alert">{rangeError}</small>}
          </label>
        </div>
        <div className="submit-zone">
          <button className="primary-button parse-button" type="submit" disabled={!sourceReady || Boolean(rangeError) || busy}>
            {busy ? <span className="button-spinner" /> : <PlayIcon />}
            {busy ? '正在提交…' : '开始解析'}
          </button>
          <p>{sourceReady ? '配置就绪，可以开始解析' : '请先选择待解析文档'}</p>
        </div>
      </div>
      {advanced && createPortal(
        <div className="advanced-layer" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) closeAdvanced() }}>
          <div ref={advancedDialogRef} id="advanced-options-dialog" className="advanced-popover" role="dialog" aria-modal="true" aria-labelledby="advanced-options-heading" aria-describedby="advanced-options-description">
            <div className="advanced-popover-header">
              <div>
                <span className="eyebrow">解析参数</span>
                <h3 id="advanced-options-heading">高级设置</h3>
                <p id="advanced-options-description">调整 OCR、超时和逐页诊断返回策略。</p>
              </div>
              <button type="button" className="dialog-close advanced-close" onClick={closeAdvanced} aria-label="关闭高级设置"><CloseIcon /></button>
            </div>
            <div className="advanced-options">
              <label>
                <span>OCR 语言</span>
                <input data-advanced-initial value={options.language} onChange={(event) => update('language', event.target.value)} placeholder="zh,en" disabled={busy} />
              </label>
              <label>
                <span>超时（秒）</span>
                <input type="number" min={10} max={3600} value={options.timeoutSeconds} onChange={(event) => update('timeoutSeconds', Number(event.target.value))} disabled={busy} />
              </label>
              <label className="check-field">
                <input type="checkbox" checked={options.enableVlmFallback} onChange={(event) => update('enableVlmFallback', event.target.checked)} disabled={busy} />
                <span>允许 VLM fallback</span>
              </label>
              <label className="check-field">
                <input type="checkbox" checked={options.preservePageBreaks} onChange={(event) => update('preservePageBreaks', event.target.checked)} disabled={busy} />
                <span>保留页分隔</span>
              </label>
              <label className="check-field">
                <input type="checkbox" checked={options.includePages} onChange={(event) => update('includePages', event.target.checked)} disabled={busy} />
                <span>返回逐页结果</span>
              </label>
              <label className="check-field">
                <input type="checkbox" checked={options.includeDiagnostics} onChange={(event) => update('includeDiagnostics', event.target.checked)} disabled={busy} />
                <span>包含诊断与告警</span>
              </label>
            </div>
            <div className="advanced-popover-footer">
              <span>设置会自动保存到当前浏览器。</span>
              <button type="button" className="primary-button" onClick={closeAdvanced}>完成</button>
            </div>
          </div>
        </div>,
        document.body,
      )}
    </form>
  )
}
