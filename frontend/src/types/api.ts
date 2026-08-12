export type ParseMode = 'auto' | 'standard' | 'ocr' | 'vlm'
export type ParseProfile = 'fast' | 'balanced' | 'accurate'
export type OutputFormat = 'markdown' | 'text'
export type SubmissionKind = 'async' | 'sync'
export type JobStatus = 'queued' | 'running' | 'completed' | 'partial' | 'failed' | 'cancelled'
export type PageStatus = 'queued' | 'running' | 'completed' | 'warning' | 'failed' | 'cancelled' | 'unknown'

export interface ParseOptions {
  mode: ParseMode
  profile: ParseProfile
  outputFormat: OutputFormat
  pageRange: string
  language: string
  enableVlmFallback: boolean
  preservePageBreaks: boolean
  includePages: boolean
  includeDiagnostics: boolean
  timeoutSeconds: number
  submissionKind: SubmissionKind
}

export interface SourceSelection {
  kind: 'file' | 'url'
  file: File | null
  url: string
}

export interface ParseWarning {
  code?: string
  message: string
  page_number?: number | null
  details?: Record<string, unknown> | null
}

export interface PageParseResult {
  page_number: number
  status: PageStatus
  backend?: string | null
  content?: string | null
  plain_text?: string | null
  duration_ms?: number | null
  warnings?: ParseWarning[]
}

export interface JobProgress {
  current: number
  total: number | null
  unit?: string
  percent?: number | null
  phase?: string | null
}

export interface RouteSummary {
  native_text_pages?: number | null
  pages_with_ocr?: number | null
  ocr_regions?: number | null
  vlm_pages?: number | null
  failed_pages?: number | null
}

export interface PipelineSummary {
  mode?: ParseMode | string
  profile?: ParseProfile | string
  primary?: string | null
  ocr?: string | null
  vlm?: string | null
}

export interface ParseResult {
  id: string
  object?: string
  status: JobStatus
  filename?: string
  mime_type?: string
  page_count?: number
  processed_pages?: number
  output_format?: OutputFormat
  content?: string
  markdown?: string
  plain_text?: string
  pages?: PageParseResult[]
  pipeline?: PipelineSummary
  route_summary?: RouteSummary
  warnings?: ParseWarning[]
  usage?: {
    input_bytes?: number | null
    duration_ms?: number | null
  }
  created_at?: string
}

export interface DocumentJob {
  id: string
  object?: string
  status: JobStatus
  progress: JobProgress
  filename?: string
  mime_type?: string
  page_count?: number | null
  processed_pages?: number | null
  mode?: ParseMode | string
  profile?: ParseProfile | string
  output_format?: OutputFormat
  pages?: PageParseResult[]
  warnings?: ParseWarning[]
  pipeline?: PipelineSummary
  route_summary?: RouteSummary
  usage?: ParseResult['usage']
  error?: ApiErrorBody['error'] | null
  status_url?: string
  events_url?: string
  result_url?: string
  created_at?: string
  started_at?: string | null
  completed_at?: string | null
}

export interface ApiErrorBody {
  error: {
    code: string
    message: string
    request_id?: string
    details?: Record<string, unknown>
  }
}

export interface BackendCapability {
  id: string
  label: string
  status: 'ready' | 'unavailable' | 'disabled' | 'unknown'
  message?: string | null
}

export interface ServiceSnapshot {
  api: 'ready' | 'unavailable' | 'checking'
  ready?: boolean
  backends: BackendCapability[]
  queue?: {
    active: number | null
    capacity: number | null
  }
  checkedAt?: number
}

export interface JobEvent {
  type: string
  job_id?: string
  current?: number
  total?: number
  percent?: number
  page_number?: number
  status?: JobStatus
  message?: string
  [key: string]: unknown
}

export interface ResultBundle {
  markdown: string
  text: string
  metadata?: ParseResult
}
