import { useState } from 'react'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ResultPane } from './ResultPane'
import { DEFAULT_OPTIONS } from '../../hooks/usePersistedOptions'
import type { ParseResult } from '../../types/api'

const result: ParseResult = {
  id: 'doc_1',
  status: 'completed',
  filename: 'report.pdf',
  output_format: 'markdown',
  content: '# 安全标题\n\n<script>window.pwned = true</script>\n\n| A | B |\n|---|---|\n| 1 | 2 |',
  pages: [
    { page_number: 1, status: 'completed', backend: 'docling-standard', duration_ms: 82, content: '# 第一页' },
    { page_number: 2, status: 'warning', backend: 'glm-ocr-remote', duration_ms: 128, content: '第二页', warnings: [{ message: '低字符数' }] },
  ],
}

describe('ResultPane', () => {
  it('uses branded output and page empty states without inventing page diagnostics', async () => {
    const user = userEvent.setup()
    const { container } = render(
      <ResultPane
        source={{ kind: 'file', file: null, url: '' }}
        options={DEFAULT_OPTIONS}
        job={null}
        onSelectPage={vi.fn()}
      />,
    )
    expect(screen.getByText('等待 Markdown 结果')).toBeInTheDocument()
    expect(container.querySelector('.workspace-state-output-empty img')).toHaveAttribute('src', '/illustrations/workspace-markdown.png')
    expect(screen.queryByRole('img')).not.toBeInTheDocument()

    await user.click(screen.getByRole('tab', { name: '纯文本' }))
    expect(container.querySelector('.workspace-state-output-empty img')).toHaveAttribute('src', '/illustrations/workspace-text.png')

    await user.click(screen.getByRole('tab', { name: '页面状态' }))
    expect(screen.getByText('暂无逐页状态')).toBeInTheDocument()
    expect(container.querySelector('.workspace-state-pages-empty img')).toHaveAttribute('src', '/illustrations/workspace-pages.png')
    expect(screen.getByText(/不会用推测值填充/)).toBeInTheDocument()
  })

  it('renders sanitized Markdown and a clickable page-status view', async () => {
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
    expect(await screen.findByRole('heading', { name: '第一页' })).toBeInTheDocument()
    expect(container.querySelector('script')).not.toBeInTheDocument()
    await user.click(screen.getByRole('tab', { name: /页面状态/ }))
    expect(screen.getByText('glm-ocr-remote')).toBeInTheDocument()
    const retryButton = screen.getByRole('button', { name: '重试第 2 页' })
    retryButton.focus()
    await user.keyboard(' ')
    expect(onRetryPage).toHaveBeenCalledWith(2)
    await user.click(screen.getByText('P.2'))
    expect(onSelectPage).toHaveBeenCalledWith(2)
    expect(screen.getByRole('tab', { name: 'Markdown 预览' })).toHaveAttribute('aria-selected', 'true')
  })

  it('links exported page markers while leaving unreported diagnostics unknown', async () => {
    const user = userEvent.setup()
    render(
      <ResultPane
        source={{ kind: 'url', file: null, url: 'https://example.com/report.pdf' }}
        options={DEFAULT_OPTIONS}
        job={{ id: 'job_1', status: 'completed', filename: 'report.pdf', progress: { current: 2, total: 2 } }}
        bundle={{
          markdown: '<!-- page: 1 -->\n\n第一页\n\n<!-- page: 2 -->\n\n第二页',
          text: '--- Page 1 ---\n\n第一页\n\n--- Page 2 ---\n\n第二页',
        }}
        onSelectPage={vi.fn()}
      />,
    )
    await user.click(screen.getByRole('tab', { name: /页面状态/ }))
    expect(screen.getByText('P.2')).toBeInTheDocument()
    expect(screen.getAllByText('unknown')).toHaveLength(2)
    expect(screen.getAllByText('—').length).toBeGreaterThan(0)
  })

  it('opens and highlights the selected page output while notifying the document preview', async () => {
    const user = userEvent.setup()
    const previewPageChange = vi.fn()
    const linkedResult: ParseResult = {
      ...result,
      content: '# 第一页\n\n第二页纯文本',
      plain_text: '第一页\n\n第二页纯文本',
      pages: [
        { page_number: 1, status: 'completed', content: '# 第一页', plain_text: '第一页', duration_ms: 10 },
        { page_number: 2, status: 'warning', content: null, plain_text: '第二页纯文本', duration_ms: 20, warnings: [{ message: '仅纯文本' }] },
      ],
    }

    function LinkedPane() {
      const [selectedPage, setSelectedPage] = useState<number | null>(null)
      return (
        <ResultPane
          source={{ kind: 'file', file: new File(['x'], 'report.pdf'), url: '' }}
          options={DEFAULT_OPTIONS}
          job={null}
          syncResult={linkedResult}
          selectedPage={selectedPage}
          onSelectPage={(page) => { previewPageChange(page); setSelectedPage(page) }}
        />
      )
    }

    const { container } = render(<LinkedPane />)
    await user.click(screen.getByRole('tab', { name: /页面状态/ }))
    await user.click(screen.getByText('P.1'))
    expect(screen.getByRole('tab', { name: 'Markdown 预览' })).toHaveAttribute('aria-selected', 'true')
    expect(container.querySelector('#result-page-1')).toHaveClass('selected')
    expect(previewPageChange).toHaveBeenLastCalledWith(1)

    await user.click(screen.getByRole('tab', { name: /页面状态/ }))
    await user.click(screen.getByText('P.2'))
    expect(screen.getByRole('tab', { name: '纯文本' })).toHaveAttribute('aria-selected', 'true')
    expect(container.querySelector('#result-page-2')).toHaveClass('selected')
    expect(screen.getByText('第二页纯文本')).toBeInTheDocument()
    expect(previewPageChange).toHaveBeenLastCalledWith(2)
    expect(Element.prototype.scrollIntoView).toHaveBeenCalled()
  })
})
