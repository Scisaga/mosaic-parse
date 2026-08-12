import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import {
  cancelDocumentJob,
  createDocumentJob,
  getApiKey,
  retryDocumentJob,
  setApiKey,
} from '../api/client'
import { HeaderStatus } from '../components/HeaderStatus'
import { SplitWorkspace } from '../components/SplitWorkspace'
import { JobBar } from '../features/jobs/JobBar'
import { ParseOptionsForm } from '../features/parse-options/ParseOptionsForm'
import { DocumentPreview } from '../features/preview/DocumentPreview'
import { ResultPane } from '../features/results/ResultPane'
import { SourcePicker } from '../features/upload/SourcePicker'
import { useDocumentJob } from '../hooks/useDocumentJob'
import { usePersistedOptions } from '../hooks/usePersistedOptions'
import { useServiceHealth } from '../hooks/useServiceHealth'
import { errorMessage } from '../lib/format'
import type { DocumentJob, SourceSelection } from '../types/api'

interface Notice {
  kind: 'success' | 'error' | 'info'
  message: string
}

function validateSource(source: SourceSelection): string | null {
  if (source.kind === 'file') return source.file ? null : '请先选择一个文档'
  if (!source.url.trim()) return '请输入文档 URL'
  try {
    const url = new URL(source.url)
    return url.protocol === 'http:' || url.protocol === 'https:' ? null : 'URL 仅支持 HTTP 或 HTTPS'
  } catch {
    return '请输入有效的文档 URL'
  }
}

export function App() {
  const queryClient = useQueryClient()
  const [source, setSource] = useState<SourceSelection>({ kind: 'file', file: null, url: '' })
  const [options, setOptions] = usePersistedOptions()
  const [seedJob, setSeedJob] = useState<DocumentJob | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [actionBusy, setActionBusy] = useState(false)
  const [selectedPage, setSelectedPage] = useState<number | null>(null)
  const [detectedPages, setDetectedPages] = useState<number | null>(null)
  const [notice, setNotice] = useState<Notice | null>(null)
  const [sourceValidationVisible, setSourceValidationVisible] = useState(false)
  const [apiKeyValue, setApiKeyValue] = useState(getApiKey)
  const health = useServiceHealth()
  const { job, jobQuery, resultQuery, sseState } = useDocumentJob(seedJob)
  const active = job?.status === 'queued' || job?.status === 'running'
  const busy = submitting || active
  const sourceError = validateSource(source)
  const sourceReady = sourceError === null

  const notify = (kind: Notice['kind'], message: string) => {
    setNotice({ kind, message })
    window.setTimeout(() => setNotice((current) => current?.message === message ? null : current), 4500)
  }

  const submit = async () => {
    setSourceValidationVisible(true)
    if (sourceError) {
      notify('error', sourceError)
      return
    }
    setSubmitting(true)
    setSelectedPage(null)
    setNotice(null)
    try {
      const created = await createDocumentJob(source, { ...options, submissionKind: 'async' })
      setSeedJob(created)
      notify('success', `任务 ${created.id} 已创建`)
    } catch (error) {
      notify('error', errorMessage(error))
    } finally {
      setSubmitting(false)
    }
  }

  const cancel = async () => {
    if (!job) return
    setActionBusy(true)
    try {
      const status = await cancelDocumentJob(job.id)
      if (status === 'deleted') {
        queryClient.removeQueries({ queryKey: ['document-job', job.id] })
        setSeedJob(null)
        notify('info', '任务已结束并删除')
      } else {
        setSeedJob({ ...job, status: 'cancelled' })
        await queryClient.invalidateQueries({ queryKey: ['document-job', job.id] })
        notify('info', '取消请求已发送')
      }
    } catch (error) {
      notify('error', errorMessage(error))
    } finally {
      setActionBusy(false)
    }
  }

  const retry = async (page?: number) => {
    if (!job) return
    setActionBusy(true)
    try {
      const retried = await retryDocumentJob(job.id, page ? String(page) : undefined)
      setSeedJob(retried)
      setSelectedPage(page ?? null)
      notify('success', page ? `第 ${page} 页重试任务已创建` : '重试任务已创建')
    } catch (error) {
      notify('error', errorMessage(error))
    } finally {
      setActionBusy(false)
    }
  }

  const clear = async () => {
    if (job) {
      setActionBusy(true)
      try {
        await cancelDocumentJob(job.id)
      } catch (error) {
        notify('error', `服务端任务未删除：${errorMessage(error)}`)
      } finally {
        queryClient.removeQueries({ queryKey: ['document-job', job.id] })
        queryClient.removeQueries({ queryKey: ['document-result', job.id] })
        setActionBusy(false)
      }
    }
    setSeedJob(null)
    setSelectedPage(null)
  }

  const resultError = resultQuery.error ? errorMessage(resultQuery.error) : jobQuery.error ? errorMessage(jobQuery.error) : null

  const changeSource = (next: SourceSelection) => {
    if (job) {
      queryClient.removeQueries({ queryKey: ['document-job', job.id] })
      queryClient.removeQueries({ queryKey: ['document-result', job.id] })
    }
    setSeedJob(null)
    setSelectedPage(null)
    setSource(next)
    setSourceValidationVisible(false)
    setDetectedPages(null)
  }

  return (
    <div className="app-shell">
      <HeaderStatus
        snapshot={health.data}
        loading={health.isFetching}
        onRefresh={() => void health.refetch()}
        apiKey={apiKeyValue}
        onApiKeyChange={(value) => {
          setApiKey(value)
          setApiKeyValue(value.trim())
          void health.refetch()
        }}
      />
      <main>
        <section className="control-deck">
          <SourcePicker
            source={source}
            onChange={changeSource}
            disabled={busy}
            detectedPages={detectedPages}
            validationError={sourceError}
            validationVisible={sourceValidationVisible}
          />
          <ParseOptionsForm options={options} onChange={setOptions} onSubmit={() => void submit()} sourceReady={sourceReady} busy={busy} />
        </section>
        <SplitWorkspace
          preview={<DocumentPreview source={source} activePage={selectedPage} onPageChange={setSelectedPage} onMetadata={setDetectedPages} />}
          result={(
            <ResultPane
              source={source}
              options={options}
              job={job ?? null}
              bundle={resultQuery.data}
              loading={resultQuery.isLoading || (Boolean(job) && (job?.status === 'completed' || job?.status === 'partial') && !resultQuery.data && !resultError)}
              error={resultError}
              selectedPage={selectedPage}
              onSelectPage={setSelectedPage}
              onRetryPage={job ? (page) => void retry(page) : undefined}
              onClear={() => void clear()}
            />
          )}
          resultAttention={Boolean(job)}
        />
      </main>
      <JobBar job={job ?? null} sseState={sseState} actionBusy={actionBusy} onCancel={() => void cancel()} onRetry={() => void retry()} onClear={() => void clear()} />
      {notice && (
        <div className={`toast toast-${notice.kind}`} role={notice.kind === 'error' ? 'alert' : 'status'} aria-live={notice.kind === 'error' ? 'assertive' : 'polite'}>
          <span>{notice.message}</span>
          <button type="button" onClick={() => setNotice(null)} aria-label="关闭通知">×</button>
        </div>
      )}
    </div>
  )
}
