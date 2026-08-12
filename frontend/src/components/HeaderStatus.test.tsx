import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HeaderStatus } from './HeaderStatus'

describe('HeaderStatus', () => {
  it('reports service truth and returns focus after closing settings with Escape', async () => {
    const user = userEvent.setup()
    render(
      <HeaderStatus
        snapshot={{
          api: 'ready',
          backends: [
            { id: 'docling', label: 'Docling', status: 'ready' },
            { id: 'glm', label: 'GLM', status: 'unavailable', message: '连接失败' },
          ],
          queue: { active: 1, capacity: 8 },
        }}
        loading={false}
        onRefresh={vi.fn()}
        apiKey=""
        onApiKeyChange={vi.fn()}
      />,
    )

    expect(screen.getByText('API')).toBeInTheDocument()
    expect(screen.getByText('GLM')).toBeInTheDocument()
    expect(screen.getByTitle('连接失败')).toHaveClass('status-unavailable')
    expect(screen.getByText('任务队列')).toBeInTheDocument()

    const settingsButton = screen.getByRole('button', { name: /连接设置/ })
    await user.click(settingsButton)
    expect(screen.getByRole('dialog', { name: 'API 连接设置' })).toBeInTheDocument()
    expect(screen.getByLabelText('API Key')).toHaveFocus()

    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(settingsButton).toHaveFocus()
  })
})
