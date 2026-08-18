import { RefreshIcon, StopIcon, TrashIcon } from '../../components/Icons'
import { formatDuration, percentOf } from '../../lib/format'
import type { ContentJob } from '../../types/api'

interface JobBarProps {
  job: ContentJob | null
  sseState?: 'idle' | 'connecting' | 'live' | 'fallback'
  actionBusy?: boolean
  onCancel: () => void
  onRetry: () => void
  onClear: () => void
}

const STATUS_LABEL: Record<ContentJob['status'], string> = {
  queued: '排队中',
  running: '解析中',
  completed: '已完成',
  partial: '部分完成',
  failed: '失败',
  cancelled: '已取消',
}

function progressPhaseLabel(phase?: string | null): string {
  if (phase === 'postprocess.text_repair') return '修复文字'
  if (phase === 'postprocess.visual_fusion') return '融合视觉证据'
  if (phase === 'page_pipeline') return '页面流水线'
  if (phase?.startsWith('page.')) return '生成结果'
  return '页面'
}

export function JobBar({ job, sseState = 'idle', actionBusy = false, onCancel, onRetry, onClear }: JobBarProps) {
  if (!job) {
    return <footer className="job-bar job-idle" aria-live="polite"><span className="job-status-dot" /><strong>工作台就绪</strong><span>文档仅在提交后发送到服务端</span></footer>
  }
  const percent = percentOf(job.progress.current, job.progress.total, job.progress.percent)
  const active = job.status === 'queued' || job.status === 'running'
  const retryable = job.status === 'failed' || job.status === 'partial' || job.status === 'cancelled'
  const progressLabel = progressPhaseLabel(job.progress.phase)
  return (
    <footer className="job-bar" aria-live="polite">
      <div className="job-progress-block">
        <div className="job-progress-meta">
          <strong><span className={`job-status-dot job-${job.status}`} />{STATUS_LABEL[job.status]}</strong>
          <span>{job.progress.current}/{job.progress.total ?? '—'} {progressLabel}</span>
          {active && <span className={`transport-state transport-${sseState}`}>{sseState === 'live' ? '实时更新' : sseState === 'fallback' ? '轮询更新' : '连接中'}</span>}
        </div>
        <div className="progress-track" role="progressbar" aria-valuenow={Math.round(percent)} aria-valuemin={0} aria-valuemax={100}><span style={{ width: `${percent}%` }} /></div>
      </div>
      <div className="job-facts">
        <span><small>已处理</small>{job.progress.current}/{job.progress.total ?? '—'}</span>
        <span><small>档位</small><b>{job.options?.profile ?? '—'}</b></span>
        <span><small>耗时</small>{formatDuration(job.started_at && job.completed_at ? Date.parse(job.completed_at) - Date.parse(job.started_at) : null)}</span>
        <span><small>尝试</small>{job.attempt ?? 1}</span>
      </div>
      <div className="job-actions">
        {active && <button type="button" className="secondary-button danger-button" onClick={onCancel} disabled={actionBusy}><StopIcon />取消</button>}
        {retryable && <button type="button" className="secondary-button" onClick={onRetry} disabled={actionBusy}><RefreshIcon />重试</button>}
        {!active && <button type="button" className="secondary-button" onClick={onClear} disabled={actionBusy}><TrashIcon />清除</button>}
      </div>
    </footer>
  )
}
