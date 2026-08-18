import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { DEFAULT_OPTIONS } from '../../hooks/usePersistedOptions'
import { ParseOptionsForm } from './ParseOptionsForm'

describe('ParseOptionsForm advanced settings', () => {
  it('keeps execution strategy out of the workspace and uses one clear action', () => {
    render(
      <ParseOptionsForm
        options={DEFAULT_OPTIONS}
        onChange={vi.fn()}
        onSubmit={vi.fn()}
        sourceReady
        busy={false}
      />,
    )

    expect(screen.queryByLabelText('执行方式')).not.toBeInTheDocument()
    expect(screen.queryByText('执行方式')).not.toBeInTheDocument()
    expect(screen.queryByText('同步解析')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '开始解析' })).toBeInTheDocument()
  })

  it('opens as an accessible overlay and restores focus after Escape or outside click', async () => {
    const user = userEvent.setup()
    render(
      <ParseOptionsForm
        options={DEFAULT_OPTIONS}
        onChange={vi.fn()}
        onSubmit={vi.fn()}
        sourceReady
        busy={false}
      />,
    )

    const trigger = screen.getByRole('button', { name: '高级设置' })
    expect(trigger).toHaveAttribute('aria-haspopup', 'dialog')
    expect(trigger).toHaveAttribute('aria-expanded', 'false')

    await user.click(trigger)
    const dialog = screen.getByRole('dialog', { name: '高级设置' })
    expect(dialog).toBeInTheDocument()
    expect(trigger).toHaveAttribute('aria-expanded', 'true')
    await waitFor(() => expect(screen.getByPlaceholderText('zh,en')).toHaveFocus())

    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(trigger).toHaveFocus()

    await user.click(trigger)
    const layer = document.querySelector('.advanced-layer')
    expect(layer).not.toBeNull()
    fireEvent.mouseDown(layer!)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(trigger).toHaveFocus()
  })

  it('keeps keyboard focus inside the advanced dialog', async () => {
    const user = userEvent.setup()
    render(
      <ParseOptionsForm
        options={DEFAULT_OPTIONS}
        onChange={vi.fn()}
        onSubmit={vi.fn()}
        sourceReady
        busy={false}
      />,
    )

    await user.click(screen.getByRole('button', { name: '高级设置' }))
    await waitFor(() => expect(screen.getByPlaceholderText('zh,en')).toHaveFocus())
    const done = screen.getByRole('button', { name: '完成' })
    done.focus()
    await user.keyboard('{Tab}')
    expect(screen.getByRole('button', { name: '关闭高级设置' })).toHaveFocus()
  })
})
