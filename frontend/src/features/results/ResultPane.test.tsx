import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ResultPane } from './ResultPane'
import { DEFAULT_OPTIONS } from '../../hooks/usePersistedOptions'
import type { ParseResult } from '../../types/api'

const result: ParseResult = {
  content_id: 'content_1',
  filename: 'report.pdf',
  markdown: [
    '# 安全标题',
    '',
    '<script>window.pwned = true</script>',
    '',
    '| 项目 | 本报告期 | 上年同期 |',
    '| --- | ---: | ---: |',
    '| 归母净利润 | 12345 | 10000 |',
  ].join('\n'),
  assets: [],
}

describe('ResultPane', () => {
  it('presents Markdown as the primary empty state', () => {
    render(
      <ResultPane
        source={{ kind: 'file', file: null, url: '' }}
        options={DEFAULT_OPTIONS}
        job={null}
      />,
    )
    expect(screen.getByText('等待 Markdown')).toBeInTheDocument()
    expect(screen.getAllByRole('tab').map((tab) => tab.textContent)).toEqual([
      '内容预览', 'Markdown 源文', '媒体', 'API 示例',
    ])
    expect(screen.queryByRole('button', { name: '.json' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '.txt' })).not.toBeInTheDocument()
  })

  it('renders sanitized GFM Markdown and exposes the exact source', async () => {
    const user = userEvent.setup()
    const { container } = render(
      <ResultPane
        source={{ kind: 'file', file: new File(['x'], 'report.pdf'), url: '' }}
        options={DEFAULT_OPTIONS}
        job={null}
        syncResult={result}
      />,
    )
    expect(await screen.findByRole('heading', { name: '安全标题' })).toBeInTheDocument()
    expect(container.querySelector('script')).not.toBeInTheDocument()
    expect(screen.getByText(/归母净利润/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '.md' })).toBeEnabled()

    await user.click(screen.getByRole('tab', { name: 'Markdown 源文' }))
    expect(screen.getByText(/\| 归母净利润 \| 12345 \| 10000 \|/)).toBeInTheDocument()
  })

  it('documents that only the result endpoint returns Markdown', async () => {
    const user = userEvent.setup()
    render(
      <ResultPane
        source={{ kind: 'url', file: null, url: 'https://example.com/report.pdf' }}
        options={DEFAULT_OPTIONS}
        job={null}
      />,
    )
    await user.click(screen.getByRole('tab', { name: 'API 示例' }))
    expect(screen.getByText(/GET \/result/)).toBeInTheDocument()
    expect(screen.getAllByText(/text\/markdown/)).toHaveLength(2)
  })
})
