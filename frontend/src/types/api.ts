export type ScanPolicy = 'auto' | 'skip'
export type DescriptionLanguage = 'zh-CN' | 'en' | 'auto'
export type JobStatus = 'queued' | 'running' | 'completed' | 'partial' | 'failed' | 'cancelled'

export interface ParseOptions {
  scanPolicy: ScanPolicy
  unitRange: string
  language: string
  describeImages: boolean
  descriptionLanguage: DescriptionLanguage
  timeoutSeconds: number
}

export interface SourceSelection {
  kind: 'file' | 'url'
  file: File | null
  url: string
}

export interface VisualAnalysis {
  classification: 'document' | 'visual' | 'mixed' | 'unknown'
  summary: string
  detailed_description: string
  visible_text: string
  scene: string | null
  objects: string[]
  actions: string[]
  language: DescriptionLanguage
  model: string
  uncertainties: string[]
}

export interface ContentAsset {
  asset_id: string
  kind: 'image' | 'video'
  role: 'source' | 'embedded_image' | 'page_crop' | 'preview' | 'keyframe'
  mime_type: string
  sha256: string
  size_bytes: number
  filename: string
  width: number | null
  height: number | null
  duration_ms: number | null
  parent_asset_id: string | null
  visual_analysis: VisualAnalysis | null
  status: 'ready' | 'partial' | 'failed'
  warning_codes: string[]
  download_url: string
}

/** Joined in the browser from the Markdown result and asset endpoints. */
export interface MarkdownResult {
  content_id: string
  filename?: string
  markdown: string
  assets: ContentAsset[]
}

export type ParseResult = MarkdownResult

export interface JobProgress {
  current: number
  total: number | null
  unit?: string
  percent?: number | null
  phase?: string | null
}

export interface ContentJob {
  id: string
  object?: 'content.parse.job'
  status: JobStatus
  progress: JobProgress
  filename?: string
  mime_type?: string
  unit_count?: number | null
  options?: {
    scan_policy: ScanPolicy
    unit_range: string | null
    language: string[]
    describe_images: boolean
    description_language: DescriptionLanguage
    timeout_seconds: number | null
  }
  error?: ApiErrorBody['error'] | null
  attempt?: number
  parent_job_id?: string | null
  status_url?: string
  events_url?: string
  result_url?: string
  assets_url?: string
  bundle_url?: string
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
  queue?: { active: number | null; capacity: number | null }
  checkedAt?: number
}

export interface JobEvent {
  type: string
  job_id?: string
  current?: number
  total?: number
  percent?: number
  unit_index?: number
  status?: JobStatus
  message?: string
  [key: string]: unknown
}

export interface ResultBundle {
  result: MarkdownResult
  markdown: string
}
