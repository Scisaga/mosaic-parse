export type ParseProfile = 'fast' | 'balanced' | 'accurate'
export type DescriptionLanguage = 'zh-CN' | 'en' | 'auto'
export type JobStatus = 'queued' | 'running' | 'completed' | 'partial' | 'failed' | 'cancelled'
export type UnitStatus = 'completed' | 'warning' | 'failed'
export type QualityVerdict = 'trusted' | 'degraded' | 'untrusted'
export type UnitType = 'page' | 'slide' | 'document_body' | 'image' | 'video'
export type RegionType = 'text' | 'heading' | 'table' | 'image' | 'signature' | 'seal' | 'handwriting' | 'formula' | 'unknown'
export type EvidenceSourceKind = 'native' | 'docling' | 'glm' | 'qwen' | 'ffmpeg' | 'ooxml'
export type ElementQuality = 'complete' | 'confirmed' | 'selected' | 'conflicted' | 'missing' | 'truncated'

export interface ParseOptions {
  profile: ParseProfile
  unitRange: string
  language: string
  descriptionLanguage: DescriptionLanguage
  includeRenderings: boolean
  timeoutSeconds: number
}

export interface SourceSelection {
  kind: 'file' | 'url'
  file: File | null
  url: string
}

export interface NormalizedBBox {
  left: number
  top: number
  right: number
  bottom: number
}

export interface ElementEvidence {
  selected_source: EvidenceSourceKind | null
  supporting_sources: EvidenceSourceKind[]
  sources: Array<{ source: EvidenceSourceKind; backend: string | null }>
  reason_codes: string[]
}

export interface TextBlockIR {
  block_id: string
  unit_id: string
  region_id: string
  block_type: RegionType
  bbox: NormalizedBBox | null
  reading_order: number
  text: string
  quality: ElementQuality
  evidence: ElementEvidence
}

export interface RegionIR {
  region_id: string
  unit_id: string
  region_type: RegionType
  bbox: NormalizedBBox | null
  reading_order: number
  block_ids: string[]
  table_ids: string[]
  asset_ids: string[]
  quality: ElementQuality
}

export interface TableCellIR {
  cell_id: string
  row: number
  column: number
  row_span: number
  column_span: number
  bbox: NormalizedBBox | null
  text: string
  is_column_header: boolean
  is_row_header: boolean
  quality: ElementQuality
  evidence: ElementEvidence
}

export interface TableIR {
  table_id: string
  unit_id: string
  region_id: string
  source_units: number[]
  bbox: NormalizedBBox | null
  caption: string | null
  unit_text: string | null
  row_count: number
  column_count: number
  header_rows: number[]
  cells: TableCellIR[]
  logical_table_id: string | null
  quality: ElementQuality
  reason_codes: string[]
}

export interface ContentUnitIR {
  unit_id: string
  unit_type: UnitType
  index: number
  width: number | null
  height: number | null
  rotation_degrees: 0 | 90 | 180 | 270 | null
  status: UnitStatus
  regions: RegionIR[]
  blocks: TextBlockIR[]
  table_ids: string[]
  asset_ids: string[]
  renderings: { markdown: string; plain_text: string }
  diagnostics: {
    source_kind: 'native' | 'scanned' | 'mixed' | 'sparse' | 'visual' | 'office' | 'video'
    quality_verdict: QualityVerdict
    selected_strategy: 'docling' | 'native_repair' | 'qwen_visual_fusion' | 'office_native' | 'visual_description' | 'video_keyframes'
    native_text_characters: number | null
    visual_ink_ratio: number | null
    image_coverage_ratio: number | null
    detected_rotation_degrees: 0 | 90 | 180 | 270 | null
    warning_codes: string[]
    qwen_calls: number | null
    qwen_duration_ms: number | null
    unresolved_conflicts: number | null
    truncated_calls: number | null
  }
  duration_ms: number
}

export interface VisualAnalysisIR {
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

export interface AssetIR {
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
  locations: Array<{
    unit_id: string | null
    page_number: number | null
    slide_number: number | null
    bbox: NormalizedBBox | null
    timestamp_ms: number | null
    relationship_id: string | null
    placement_id: string | null
  }>
  visual_analysis: VisualAnalysisIR | null
  status: 'ready' | 'partial' | 'failed'
  warning_codes: string[]
  download_url: string
}

export interface IRWarning {
  code: string
  severity: 'info' | 'warning' | 'error'
  unit_index: number | null
  region_id: string | null
  asset_id: string | null
  count: number | null
}

export interface ContentEvidenceIR {
  object: 'content.evidence'
  schema_version: 'content-evidence/1.0'
  status: 'completed' | 'partial'
  source: {
    content_id: string
    source_sha256: string
    filename: string
    mime_type: string
    kind: 'pdf' | 'docx' | 'pptx' | 'image' | 'video'
    size_bytes: number
    unit_count: number
    page_count: number | null
    slide_count: number | null
    duration_ms: number | null
    width: number | null
    height: number | null
  }
  units: ContentUnitIR[]
  assets: AssetIR[]
  tables: TableIR[]
  logical_tables: Array<{
    logical_table_id: string
    fragment_table_ids: string[]
    source_units: number[]
    header_policy: 'first_fragment'
  }>
  visual_analysis: VisualAnalysisIR | null
  video_analysis: {
    duration_ms: number
    width: number
    height: number
    codec: string | null
    frame_rate: number | null
    visual_only: true
    summary: string
    scenes: Array<{ scene_id: string; start_ms: number; end_ms: number; keyframe_asset_ids: string[] }>
    keyframes: Array<{ asset_id: string; timestamp_ms: number; scene_id: string | null; visual_analysis: VisualAnalysisIR }>
  } | null
  renderings: { markdown: string; plain_text: string }
  diagnostics: {
    trusted_units: number
    degraded_units: number
    untrusted_units: number
    repaired_units: number
    visual_units: number
    unresolved_visual_conflicts: number
  }
  warnings: IRWarning[]
  runtime: {
    profile: ParseProfile
    primary_backend: string
    ocr_backend: string | null
    visual_backend: string | null
    parser_version: string
    input_bytes: number
    duration_ms: number
    qwen_calls: number
    ffmpeg_duration_ms: number | null
  }
  links: { job: string; events: string; result: string; assets: string; bundle: string }
  created_at: string
}

export type ParseResult = ContentEvidenceIR

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
    profile: ParseProfile
    unit_range: string | null
    language: string[]
    description_language: DescriptionLanguage
    include_renderings: boolean
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
  evidence: ContentEvidenceIR
  markdown: string
  text: string
}
