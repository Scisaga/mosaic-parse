import { useCallback, useEffect, useRef, useState } from 'react'
import type { ServiceSnapshot } from '../types/api'
import { CloseIcon, ExternalLinkIcon, RefreshIcon, SettingsIcon } from './Icons'
import { MosaicBackdrop } from './MosaicBackdrop'

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
  const [statusOpen, setStatusOpen] = useState(false)
  const [draftKey, setDraftKey] = useState(apiKey)
  const statusButtonRef = useRef<HTMLButtonElement>(null)
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
  useEffect(() => {
    if (!statusOpen) return
    const closeStatus = (event: globalThis.KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      setStatusOpen(false)
      statusButtonRef.current?.focus()
    }
    document.addEventListener('keydown', closeStatus)
    return () => document.removeEventListener('keydown', closeStatus)
  }, [statusOpen])
  const apiStatus = snapshot?.api ?? (loading ? 'checking' : 'unavailable')
  const services = [
    { id: 'api', label: 'API', status: apiStatus, message: 'HTTP API 与任务服务' },
    ...(snapshot?.backends ?? []),
  ]
  const readyCount = services.filter((service) => service.status === 'ready').length
  const aggregateStatus = loading ? 'checking' : readyCount === services.length ? 'ready' : 'unavailable'
  return (
    <header className="app-header">
      <MosaicBackdrop />
      <div className="brand-lockup">
        <img className="brand-logo" src="/logo.png?v=4" alt="" aria-hidden="true" />
        <div className="brand-copy">
          <h1>MosaicParse</h1>
          <p>多模态内容解析</p>
        </div>
      </div>
      <div className="header-operations">
        <div className="service-statuses" aria-label="服务状态" aria-live="polite">
          <button ref={statusButtonRef} className={`service-summary status-${aggregateStatus}`} type="button" aria-expanded={statusOpen} aria-controls="service-status-popover" onClick={() => setStatusOpen((value) => !value)}>
            <i aria-hidden="true" /> 服务 {readyCount}/{services.length} 就绪
          </button>
          {statusOpen && (
            <section id="service-status-popover" className="service-status-popover" aria-label="服务状态详情">
              <div className="service-status-heading"><strong>服务状态</strong><span>实时探测</span></div>
              <ul>
                {services.map((service) => (
                  <li className={`status-${service.status}`} title={service.message || undefined} key={service.id}>
                    <span><i aria-hidden="true" />{service.label}</span><b>{labelByStatus[service.status]}</b>
                  </li>
                ))}
              </ul>
              <div className="service-queue"><span>任务队列</span><b>{snapshot?.queue?.active ?? '—'} / {snapshot?.queue?.capacity ?? '—'}</b></div>
            </section>
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
