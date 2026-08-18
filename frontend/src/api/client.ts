import type {
  ApiErrorBody,
  BackendCapability,
  ContentJob,
  JobEvent,
  JobProgress,
  ParseOptions,
  ParseResult,
  ResultBundle,
  ServiceSnapshot,
  SourceSelection,
} from '../types/api'

const configuredBase = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.trim() ?? ''
export const API_BASE = configuredBase.replace(/\/$/, '')
const API_KEY_STORAGE = 'mosaicparse.api-key'

export function getApiKey(): string {
  if (typeof sessionStorage === 'undefined') return ''
  return sessionStorage.getItem(API_KEY_STORAGE) ?? ''
}

export function setApiKey(value: string): void {
  if (typeof sessionStorage === 'undefined') return
  const normalized = value.trim()
  if (normalized) sessionStorage.setItem(API_KEY_STORAGE, normalized)
  else sessionStorage.removeItem(API_KEY_STORAGE)
}

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly requestId?: string
  readonly details?: Record<string, unknown>

  constructor(status: number, body: ApiErrorBody['error']) {
    super(body.message)
    this.name = 'ApiError'
    this.status = status
    this.code = body.code
    this.requestId = body.request_id
    this.details = body.details
  }
}

function endpoint(path: string): string {
  if (/^https?:\/\//.test(path)) return path
  return `${API_BASE}${path.startsWith('/') ? path : `/${path}`}`
}

async function errorFromResponse(response: Response): Promise<ApiError> {
  let body: unknown
  try {
    body = await response.json()
  } catch {
    body = null
  }
  const candidate = body && typeof body === 'object' ? body as Record<string, unknown> : {}
  const wrapped = candidate.error && typeof candidate.error === 'object'
    ? candidate.error as Partial<ApiErrorBody['error']>
    : null
  const detail = candidate.detail
  const validationMessage = Array.isArray(detail)
    ? detail.map((item) => {
      const issue = asRecord(item)
      const location = Array.isArray(issue.loc) ? issue.loc.slice(1).join('.') : ''
      return `${location ? `${location}: ` : ''}${String(issue.msg ?? '参数无效')}`
    }).join('；')
    : null
  const message = wrapped?.message
    ?? (typeof detail === 'string' ? detail : null)
    ?? validationMessage
    ?? response.statusText
    ?? `HTTP ${response.status}`
  return new ApiError(response.status, {
    code: wrapped?.code ?? `http_${response.status}`,
    message,
    request_id: wrapped?.request_id,
    details: wrapped?.details,
  })
}

async function request(input: string, init?: RequestInit): Promise<Response> {
  let response: Response
  try {
    const apiKey = getApiKey()
    response = await fetch(endpoint(input), {
      credentials: 'same-origin',
      ...init,
      headers: {
        Accept: 'application/json',
        ...(apiKey ? { 'X-API-Key': apiKey } : {}),
        ...init?.headers,
      },
    })
  } catch (error) {
    throw new ApiError(0, {
      code: 'network_error',
      message: error instanceof Error ? error.message : '无法连接解析服务',
    })
  }
  if (!response.ok) throw await errorFromResponse(response)
  return response
}

export function buildParseForm(source: SourceSelection, options: ParseOptions): FormData {
  const data = new FormData()
  if (source.kind === 'file' && source.file) data.append('file', source.file, source.file.name)
  if (source.kind === 'url' && source.url.trim()) data.append('source_url', source.url.trim())
  data.append('profile', options.profile)
  if (options.unitRange.trim()) data.append('unit_range', options.unitRange.trim())
  data.append('language', options.language.trim() || 'zh,en')
  data.append('description_language', options.descriptionLanguage)
  data.append('include_renderings', String(options.includeRenderings))
  if (options.timeoutSeconds > 0) data.append('timeout_seconds', String(options.timeoutSeconds))
  return data
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' ? value as Record<string, unknown> : {}
}

function normalizeProgress(raw: unknown): JobProgress {
  const value = asRecord(raw)
  const current = Number(value.current ?? value.processed ?? value.completed ?? 0)
  const totalValue = value.total ?? value.unit_count
  const total = totalValue === null || totalValue === undefined ? null : Number(totalValue)
  const percentValue = value.percent
  return {
    current: Number.isFinite(current) ? current : 0,
    total: total !== null && Number.isFinite(total) ? total : null,
    unit: typeof value.unit === 'string' ? value.unit : 'page',
    percent: typeof percentValue === 'number' ? percentValue : null,
    phase: typeof value.phase === 'string' ? value.phase : null,
  }
}

function normalizeJob(raw: unknown): ContentJob {
  const value = asRecord(raw)
  const nested = value.job && typeof value.job === 'object' ? asRecord(value.job) : value
  const id = String(nested.id ?? nested.job_id ?? '')
  const rawProgress = nested.progress ?? {
    current: nested.processed_units,
    total: nested.unit_count,
    percent: nested.progress_percent,
  }
  return {
    ...(nested as unknown as ContentJob),
    id,
    status: (nested.status as ContentJob['status']) ?? 'queued',
    progress: normalizeProgress(rawProgress),
  }
}

export async function createContentJob(source: SourceSelection, options: ParseOptions): Promise<ContentJob> {
  const response = await request('/v1/content/jobs', {
    method: 'POST',
    body: buildParseForm(source, options),
  })
  return normalizeJob(await response.json())
}

export async function parseContent(source: SourceSelection, options: ParseOptions): Promise<ParseResult> {
  const response = await request('/v1/content/parse', {
    method: 'POST',
    body: buildParseForm(source, options),
  })
  return await response.json() as ParseResult
}

export async function getContentJob(jobId: string): Promise<ContentJob> {
  const response = await request(`/v1/content/jobs/${encodeURIComponent(jobId)}`)
  return normalizeJob(await response.json())
}

export async function getContentResult(jobId: string): Promise<ResultBundle> {
  const response = await request(`/v1/content/jobs/${encodeURIComponent(jobId)}/result`)
  const evidence = await response.json() as ParseResult
  return {
    evidence,
    markdown: evidence.renderings.markdown,
    text: evidence.renderings.plain_text,
  }
}

export async function retryContentJob(jobId: string, unitRange?: string): Promise<ContentJob> {
  const body = new FormData()
  if (unitRange?.trim()) body.append('unit_range', unitRange.trim())
  const response = await request(`/v1/content/jobs/${encodeURIComponent(jobId)}/retry`, {
    method: 'POST',
    body: unitRange?.trim() ? body : undefined,
  })
  return normalizeJob(await response.json())
}

export async function cancelContentJob(jobId: string): Promise<'cancelled' | 'deleted'> {
  const response = await request(`/v1/content/jobs/${encodeURIComponent(jobId)}`, { method: 'DELETE' })
  const body = asRecord(await response.json())
  return body.status === 'deleted' ? 'deleted' : 'cancelled'
}

function backendStatus(raw: unknown): BackendCapability['status'] {
  const value = asRecord(raw)
  const status = String(value.status ?? value.state ?? '').toLowerCase()
  if (status === 'ready' || status === 'healthy' || status === 'ok' || status === 'available') return 'ready'
  if (status === 'disabled' || value.enabled === false) return 'disabled'
  if (status === 'unavailable' || status === 'degraded' || status === 'error' || status === 'failed' || value.available === false) return 'unavailable'
  if (value.available === true || value.ready === true) return 'ready'
  return 'unknown'
}

function backendFromValue(id: string, label: string, raw: unknown): BackendCapability {
  const value = asRecord(raw)
  return {
    id,
    label,
    status: typeof raw === 'string' ? backendStatus({ status: raw }) : backendStatus(raw),
    message: typeof value.message === 'string'
      ? value.message
      : typeof value.detail === 'string' ? value.detail
      : typeof value.error === 'string' ? value.error : null,
  }
}

function normalizeBackends(raw: unknown): { backends: BackendCapability[]; queue?: ServiceSnapshot['queue'] } {
  const root = asRecord(raw)
  const listed = Array.isArray(root.backends) ? root.backends : Array.isArray(raw) ? raw : null
  const aliases: Record<string, { id: string; label: string }> = {
    docling: { id: 'docling', label: 'Docling' },
    'docling-standard': { id: 'docling', label: 'Docling' },
    glm: { id: 'glm', label: 'GLM' },
    glm_ocr: { id: 'glm', label: 'GLM' },
    'glm-ocr-remote': { id: 'glm', label: 'GLM' },
    vlm: { id: 'vlm', label: 'VLM' },
    ollama: { id: 'vlm', label: 'VLM' },
    ollama_vlm: { id: 'vlm', label: 'VLM' },
  }
  const byId = new Map<string, BackendCapability>()
  if (listed) {
    for (const item of listed) {
      const record = asRecord(item)
      const rawId = String(record.id ?? record.name ?? record.backend ?? '').toLowerCase()
      const alias = aliases[rawId]
        ?? (rawId.includes('docling') ? aliases.docling
          : rawId.includes('glm') ? aliases.glm
            : rawId.includes('vlm') || rawId.includes('ollama') ? aliases.vlm
              : { id: rawId || 'unknown', label: String(record.label ?? (rawId || 'Backend')) })
      byId.set(alias.id, backendFromValue(alias.id, alias.label, item))
    }
  } else {
    for (const [key, alias] of Object.entries(aliases)) {
      if (root[key] !== undefined && !byId.has(alias.id)) byId.set(alias.id, backendFromValue(alias.id, alias.label, root[key]))
    }
  }
  for (const expected of [
    { id: 'docling', label: 'Docling' },
    { id: 'glm', label: 'GLM' },
    { id: 'vlm', label: 'VLM' },
  ]) {
    if (!byId.has(expected.id)) byId.set(expected.id, { ...expected, status: 'unknown' })
  }
  const queueRaw = asRecord(root.queue)
  const activeRaw = queueRaw.active ?? queueRaw.running ?? root.active_jobs ?? root.queue_active
  const capacityRaw = queueRaw.capacity ?? queueRaw.max ?? root.queue_capacity ?? root.max_jobs
  const toNumber = (value: unknown): number | null => {
    const number = Number(value)
    return value !== undefined && value !== null && Number.isFinite(number) ? number : null
  }
  const hasQueue = activeRaw !== undefined || capacityRaw !== undefined
  return {
    backends: [...byId.values()],
    queue: hasQueue ? { active: toNumber(activeRaw), capacity: toNumber(capacityRaw) } : undefined,
  }
}

export async function fetchServiceSnapshot(): Promise<ServiceSnapshot> {
  const [healthResult, readyResult, backendResult] = await Promise.allSettled([
    request('/health').then((response) => response.json()),
    request('/ready').then((response) => response.json()),
    request('/v1/backends').then((response) => response.json()),
  ])
  const health = healthResult.status === 'fulfilled' ? asRecord(healthResult.value) : {}
  const ready = readyResult.status === 'fulfilled' ? asRecord(readyResult.value) : {}
  const isReady = healthResult.status === 'fulfilled'
    && readyResult.status === 'fulfilled'
    && ready.ready === true
  const api = isReady ? 'ready' as const : 'unavailable' as const
  const normalized = backendResult.status === 'fulfilled'
    ? normalizeBackends(backendResult.value)
    : normalizeBackends(health)
  return {
    api,
    ready: isReady,
    backends: normalized.backends,
    queue: normalized.queue,
    checkedAt: Date.now(),
  }
}

export function jobEventsUrl(job: Pick<ContentJob, 'id' | 'events_url'>): string {
  return endpoint(job.events_url || `/v1/content/jobs/${encodeURIComponent(job.id)}/events`)
}

export function parseJobEventData(data: string, eventType = 'message'): JobEvent | null {
  try {
    const payload = JSON.parse(data) as JobEvent
    return { ...payload, type: payload.type || eventType || 'message' }
  } catch {
    return null
  }
}

export function parseJobEvent(event: MessageEvent<string>, fallbackType = 'message'): JobEvent | null {
  return parseJobEventData(event.data, event.type || fallbackType)
}
