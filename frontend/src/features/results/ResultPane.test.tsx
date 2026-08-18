import { useState } from 'react'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ResultPane } from './ResultPane'
import { DEFAULT_OPTIONS } from '../../hooks/usePersistedOptions'
import type { ContentUnit, ParseResult } from '../../types/api'

function page(pageNumber: number, status: ContentUnit['status'], markdown: string, plainText: string): ContentUnit {
  return {
    unit_id: `p${pageNumber}`,
    unit_type: 'page',
    index: pageNumber,
    width: 595,
    height: 842,
    rotation_degrees: 0,
    status,
    regions: [],
    blocks: [],
    table_ids: [],
    asset_ids: [],
    renderings: { markdown, plain_text: plainText },
    diagnostics: {
      source_kind: pageNumber === 1 ? 'native' : 'mixed',
      quality_verdict: status === 'completed' ? 'trusted' : 'degraded',
      selected_strategy: pageNumber === 1 ? 'docling' : 'qwen_visual_fusion',
      native_text_characters: 100,
      visual_ink_ratio: 0.1,
      image_coverage_ratio: 0,
      detected_rotation_degrees: 0,
      warning_codes: status === 'warning' ? ['unresolved_visual_conflict'] : [],
      qwen_calls: pageNumber === 1 ? 0 : 1,
      qwen_duration_ms: pageNumber === 1 ? 0 : 1200,
      unresolved_conflicts: status === 'warning' ? 1 : 0,
      truncated_calls: 0,
    },
    duration_ms: pageNumber === 1 ? 82 : 128,
  }
}

const result: ParseResult = {
  object: 'content.parse_result',
  schema_version: 'content-parse-result/1.0',
  status: 'completed',
  source: {
    content_id: 'content_1',
    source_sha256: '0'.repeat(64),
    filename: 'report.pdf',
    mime_type: 'application/pdf',
    kind: 'pdf',
    size_bytes: 3,
    unit_count: 2,
    page_count: 2,
    slide_count: null,
    duration_ms: null,
    width: null,
    height: null,
  },
  units: [
    page(1, 'completed', '# 第一页', '第一页'),
    page(2, 'warning', '第二页', '第二页'),
  ],
  assets: [],
  tables: [],
  logical_tables: [],
  visual_analysis: null,
  renderings: {
    markdown: '# 安全标题\n\n<script>window.pwned = true</script>\n\n| A | B |\n|---|---|\n| 1 | 2 |',
    plain_text: '安全标题\nA B\n1 2',
  },
  diagnostics: {
    trusted_units: 1,
    degraded_units: 1,
    untrusted_units: 0,
    repaired_units: 0,
    visual_units: 1,
    unresolved_visual_conflicts: 1,
  },
  warnings: [{ code: 'unresolved_visual_conflict', severity: 'warning', unit_index: 2, region_id: null, asset_id: null, count: 1 }],
  runtime: {
    profile: 'accurate',
    primary_backend: 'docling-standard',
    ocr_backend: 'glm-ocr',
    visual_backend: 'qwen3.6',
    parser_version: '0.4.0',
    input_bytes: 3,
    duration_ms: 210,
    qwen_calls: 1,
    ffmpeg_duration_ms: null,
  },
  video_analysis: null,
  links: { job: '/job', events: '/events', result: '/result', assets: '/assets', bundle: '/bundle' },
  created_at: '2026-08-17T00:00:00Z',
}

describe('ResultPane', () => {
  it('presents the parse overview as the primary empty state', async () => {
    const user = userEvent.setup()
    const { container } = render(
      <ResultPane
        source={{ kind: 'file', file: null, url: '' }}
        options={DEFAULT_OPTIONS}
        job={null}
        onSelectPage={vi.fn()}
      />,
    )
    expect(screen.getByText('等待解析结果')).toBeInTheDocument()
    expect(screen.getAllByRole('tab').map((tab) => tab.textContent)).toEqual([
      '概览', 'Markdown', '纯文本', '内容单元', '媒体', 'API 示例',
    ])
    await user.click(screen.getByRole('tab', { name: 'Markdown' }))
    expect(container.querySelector('.workspace-state-output-empty')).toBeInTheDocument()
    await user.click(screen.getByRole('tab', { name: /内容单元/ }))
    expect(screen.getByText('暂无内容单元')).toBeInTheDocument()
  })

  it('renders sanitized Markdown and unit diagnostics', async () => {
    const user = userEvent.setup()
    const onSelectPage = vi.fn()
    const onRetryPage = vi.fn()
    const { container } = render(
      <ResultPane
        source={{ kind: 'file', file: new File(['x'], 'report.pdf'), url: '' }}
        options={DEFAULT_OPTIONS}
        job={null}
        syncResult={result}
        onSelectPage={onSelectPage}
        onRetryPage={onRetryPage}
      />,
    )
    expect(screen.getByText(/content-parse-result\/1.0/)).toBeInTheDocument()
    expect(screen.getByText('2 / 2')).toBeInTheDocument()
    expect(screen.getByText('解析摘要')).toBeInTheDocument()
    expect(screen.getByText('来源追踪')).toBeInTheDocument()
    expect(container.querySelector('.result-overview pre')).not.toBeInTheDocument()
    expect(screen.queryByText('安全标题')).not.toBeInTheDocument()
    await user.click(screen.getByRole('tab', { name: 'Markdown' }))
    expect(await screen.findByRole('heading', { name: '安全标题' })).toBeInTheDocument()
    expect(container.querySelector('script')).not.toBeInTheDocument()
    await user.click(screen.getByRole('tab', { name: /内容单元/ }))
    expect(screen.getByText('qwen_visual_fusion')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '重试第 2 页' }))
    expect(onRetryPage).toHaveBeenCalledWith(2)
    await user.click(screen.getByText('P.2'))
    expect(onSelectPage).toHaveBeenCalledWith(2)
    expect(screen.getByRole('tab', { name: 'Markdown' })).toHaveAttribute('aria-selected', 'true')
  })

  it('opens and highlights a selected physical page rendering', async () => {
    const user = userEvent.setup()
    const previewPageChange = vi.fn()

    function LinkedPane() {
      const [selectedPage, setSelectedPage] = useState<number | null>(null)
      return (
        <ResultPane
          source={{ kind: 'file', file: new File(['x'], 'report.pdf'), url: '' }}
          options={DEFAULT_OPTIONS}
          job={null}
          syncResult={result}
          selectedPage={selectedPage}
          onSelectPage={(selected) => { previewPageChange(selected); setSelectedPage(selected) }}
        />
      )
    }

    const { container } = render(<LinkedPane />)
    await user.click(screen.getByRole('tab', { name: /内容单元/ }))
    await user.click(screen.getByText('P.1'))
    expect(container.querySelector('#result-unit-1')).toHaveClass('selected')
    expect(previewPageChange).toHaveBeenLastCalledWith(1)
  })
})
