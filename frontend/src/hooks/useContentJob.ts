import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { getContentJob, getContentResult, jobEventsUrl, parseJobEventData } from '../api/client'
import { runSseLoop } from '../api/sse'
import type { ContentJob, JobEvent } from '../types/api'

const TERMINAL = new Set<ContentJob['status']>(['completed', 'partial', 'failed', 'cancelled'])

function statusFromEvent(event: JobEvent): ContentJob['status'] | undefined {
  if (event.status) return event.status
  if (event.type === 'job.started') return 'running'
  if (event.type === 'job.completed') return 'completed'
  if (event.type === 'job.failed') return 'failed'
  if (event.type === 'job.cancelled') return 'cancelled'
  return undefined
}

export function useContentJob(seed: ContentJob | null) {
  const queryClient = useQueryClient()
  const jobId = seed?.id ?? ''
  const [sseState, setSseState] = useState<'idle' | 'connecting' | 'live' | 'fallback'>('idle')

  const jobQuery = useQuery({
    queryKey: ['content-job', jobId],
    queryFn: () => getContentJob(jobId),
    enabled: Boolean(jobId),
    initialData: seed ?? undefined,
    refetchInterval: (query) => {
      const status = query.state.data?.status
      return status && TERMINAL.has(status) ? false : 2_500
    },
    retry: 2,
  })
  const job = jobQuery.data ?? seed
  const terminal = Boolean(job?.status && TERMINAL.has(job.status))

  useEffect(() => {
    if (!jobId || terminal) {
      setSseState('idle')
      return
    }
    const controller = new AbortController()

    const acceptEvent = (data: string, eventType: string) => {
      const event = parseJobEventData(data, eventType)
      if (!event) return
      queryClient.setQueryData<ContentJob>(['content-job', jobId], (current) => {
        if (!current) return current
        const status = statusFromEvent(event)
        const nextCurrent = typeof event.current === 'number' ? event.current : current.progress.current
        const nextTotal = typeof event.total === 'number' ? event.total : current.progress.total
        return {
          ...current,
          status: status ?? current.status,
          progress: {
            ...current.progress,
            current: nextCurrent,
            total: nextTotal,
            percent: typeof event.percent === 'number' ? event.percent : current.progress.percent,
            phase: typeof event.phase === 'string' ? event.phase : current.progress.phase,
          },
        }
      })
      // Page progress is already carried by the event stream. Refetching the
      // durable snapshot for every frame creates a request burst and can
      // overwrite several closely-spaced page updates with the final state.
      // Keep polling as the recovery path and refresh once at terminal state.
      if (event.type === 'job.completed' || event.type === 'job.failed' || event.type === 'job.cancelled') {
        void queryClient.invalidateQueries({ queryKey: ['content-job', jobId] })
      }
    }

    void runSseLoop({
      url: jobEventsUrl({ id: jobId, events_url: seed?.events_url }),
      signal: controller.signal,
      onFrame: (frame) => acceptEvent(frame.data, frame.event),
      onState: (state) => {
        if (!controller.signal.aborted) setSseState(state)
      },
    }).catch(() => {
      if (!controller.signal.aborted) setSseState('fallback')
    })
    return () => controller.abort()
  }, [jobId, queryClient, seed?.events_url, terminal])

  const resultQuery = useQuery({
    queryKey: ['content-result', jobId],
    queryFn: () => getContentResult(jobId),
    enabled: Boolean(jobId && job && (job.status === 'completed' || job.status === 'partial')),
    staleTime: Infinity,
    retry: 2,
  })

  return { job, jobQuery, resultQuery, sseState, terminal }
}
