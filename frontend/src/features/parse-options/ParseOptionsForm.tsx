import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { createPortal } from 'react-dom'
import { CloseIcon, PlayIcon, SettingsIcon } from '../../components/Icons'
import { validatePageRange } from '../../lib/unitRange'
import type { ParseOptions } from '../../types/api'

interface ParseOptionsFormProps {
  options: ParseOptions
  onChange: (options: ParseOptions) => void
  onSubmit: () => void
  sourceReady: boolean
  busy: boolean
}

const PROFILES = [
  { value: 'fast', label: '快速' },
  { value: 'balanced', label: '均衡' },
  { value: 'accurate', label: '精确' },
] as const

export function ParseOptionsForm({ options, onChange, onSubmit, sourceReady, busy }: ParseOptionsFormProps) {
  const [advanced, setAdvanced] = useState(false)
  const advancedButtonRef = useRef<HTMLButtonElement>(null)
  const advancedDialogRef = useRef<HTMLDivElement>(null)
  const rangeError = validatePageRange(options.unitRange)
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
          <p>服务自动选择文档、视觉或视频链路，并生成统一的结构化解析结果。</p>
        </div>
        <button ref={advancedButtonRef} className="advanced-toggle" type="button" aria-haspopup="dialog" aria-expanded={advanced} aria-controls="advanced-options-dialog" onClick={() => setAdvanced((value) => !value)}>
          <SettingsIcon /> 高级设置
        </button>
      </div>
      <div className="options-content">
        <div className="options-grid">
          <label>
            <span>质量档位</span>
            <select value={options.profile} onChange={(event) => update('profile', event.target.value as ParseOptions['profile'])} disabled={busy}>
              {PROFILES.map((profile) => <option value={profile.value} key={profile.value}>{profile.label}</option>)}
            </select>
            <small>精确档自动对复杂视觉页执行 GLM＋Qwen 融合。</small>
          </label>
          <label className={rangeError ? 'invalid' : ''}>
            <span>页 / 幻灯片范围</span>
            <input value={options.unitRange} onChange={(event) => update('unitRange', event.target.value)} placeholder="全部，如 1-5,8" disabled={busy} aria-invalid={Boolean(rangeError)} aria-describedby={rangeError ? 'page-range-error' : undefined} />
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
                <p id="advanced-options-description">调整语言、超时及是否附带派生渲染。</p>
              </div>
              <button type="button" className="dialog-close advanced-close" onClick={closeAdvanced} aria-label="关闭高级设置"><CloseIcon /></button>
            </div>
            <div className="advanced-options">
              <label>
                <span>OCR 语言</span>
                <input data-advanced-initial value={options.language} onChange={(event) => update('language', event.target.value)} placeholder="zh,en" disabled={busy} />
              </label>
              <label>
                <span>图片描述语言</span>
                <select value={options.descriptionLanguage} onChange={(event) => update('descriptionLanguage', event.target.value as ParseOptions['descriptionLanguage'])} disabled={busy}>
                  <option value="zh-CN">简体中文</option>
                  <option value="en">English</option>
                  <option value="auto">跟随可见内容</option>
                </select>
              </label>
              <label>
                <span>超时（秒）</span>
                <input type="number" min={10} max={3600} value={options.timeoutSeconds} onChange={(event) => update('timeoutSeconds', Number(event.target.value))} disabled={busy} />
              </label>
              <label className="check-field">
                <input type="checkbox" checked={options.includeRenderings} onChange={(event) => update('includeRenderings', event.target.checked)} disabled={busy} />
                <span>附带 Markdown 与纯文本派生渲染</span>
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
