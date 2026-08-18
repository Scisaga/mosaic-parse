import { useCallback, useEffect, useRef, useState } from 'react'
import type { ServiceSnapshot } from '../types/api'
import { CloseIcon, ExternalLinkIcon, RefreshIcon, SettingsIcon } from './Icons'

interface HeaderStatusProps {
  snapshot?: ServiceSnapshot
  loading: boolean
  onRefresh: () => void
  apiKey: string
  onApiKeyChange: (value: string) => void
}

const labelByStatus = {
  ready: '就绪',
  unavailable: '不可用',
  disabled: '未启用',
  unknown: '未知',
  checking: '检查中',
}

export function HeaderStatus({ snapshot, loading, onRefresh, apiKey, onApiKeyChange }: HeaderStatusProps) {
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [draftKey, setDraftKey] = useState(apiKey)
  const settingsButtonRef = useRef<HTMLButtonElement>(null)
  const dialogRef = useRef<HTMLElement>(null)
  useEffect(() => setDraftKey(apiKey), [apiKey])
  const closeSettings = useCallback(() => {
    setSettingsOpen(false)
    settingsButtonRef.current?.focus()
  }, [])
  useEffect(() => {
    if (!settingsOpen) return
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        closeSettings()
        return
      }
      if (event.key !== 'Tab') return
      const focusable = [...(dialogRef.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), [href], [tabindex]:not([tabindex="-1"])') ?? [])]
      if (focusable.length === 0) return
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
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
  }, [closeSettings, settingsOpen])
  const apiStatus = snapshot?.api ?? (loading ? 'checking' : 'unavailable')
  return (
    <header className="app-header">
      <div className="brand-lockup">
        <img className="brand-logo" src="/logo.png?v=4" alt="" aria-hidden="true" />
        <div className="brand-copy">
          <h1>MosaicParse</h1>
          <p>多模态内容证据解析</p>
        </div>
      </div>
      <div className="header-operations">
        <div className="service-statuses" aria-label="服务状态" aria-live="polite">
          <span className={`status-pill status-${apiStatus}`} title="来自 /health 与 /ready 的实时状态">
            <i aria-hidden="true" /> <b>API</b> {labelByStatus[apiStatus]}
          </span>
          {(snapshot?.backends ?? []).map((backend) => (
            <span
              className={`status-pill status-${backend.status}`}
              title={backend.message || `来自 /v1/backends 的 ${backend.label} 状态`}
              key={backend.id}
            >
              <i aria-hidden="true" /> <b>{backend.label}</b> {labelByStatus[backend.status]}
            </span>
          ))}
          {snapshot?.queue && (
            <span className="queue-pill" title="当前任务 / 队列容量">
              <span>任务队列</span> {snapshot.queue.active ?? '—'} / {snapshot.queue.capacity ?? '—'}
            </span>
          )}
        </div>
        <nav className="header-actions" aria-label="全局操作">
          <button className="header-action header-refresh" type="button" onClick={onRefresh} disabled={loading} aria-label="刷新后端状态" title="刷新后端状态">
            <RefreshIcon className={loading ? 'spin' : ''} />
          </button>
          <button ref={settingsButtonRef} className="header-action header-settings" type="button" onClick={() => setSettingsOpen(true)}>
            <SettingsIcon /> <span>连接设置</span>
          </button>
          <a className="header-action" href="/docs" target="_blank" rel="noreferrer">
            <span>API 文档</span> <ExternalLinkIcon />
          </a>
        </nav>
      </div>
      {settingsOpen && (
        <div className="settings-overlay" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) closeSettings() }}>
          <section ref={dialogRef} className="settings-dialog" role="dialog" aria-modal="true" aria-labelledby="settings-title" aria-describedby="settings-description">
            <button className="dialog-close" type="button" onClick={closeSettings} aria-label="关闭连接设置"><CloseIcon /></button>
            <span className="eyebrow">连接配置</span>
            <h2 id="settings-title">API 连接设置</h2>
            <p id="settings-description">当服务端设置了可选的公共 API Key 时，请在这里填写。密钥只保存在当前浏览器标签的会话存储中。</p>
            <label htmlFor="api-key">API Key</label>
            <input id="api-key" type="password" value={draftKey} onChange={(event) => setDraftKey(event.target.value)} autoComplete="off" placeholder="留空表示服务未启用 API Key" autoFocus />
            <div className="settings-actions">
              <button type="button" className="secondary-button" onClick={closeSettings}>取消</button>
              <button type="button" className="primary-button" onClick={() => { onApiKeyChange(draftKey); closeSettings() }}>保存并刷新</button>
            </div>
          </section>
        </div>
      )}
    </header>
  )
}
