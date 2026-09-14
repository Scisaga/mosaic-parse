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
    expect(screen.queryByText('质量档位')).not.toBeInTheDocument()
    expect(screen.queryByRole('option', { name: '均衡' })).not.toBeInTheDocument()
    expect(screen.queryByRole('option', { name: '精确' })).not.toBeInTheDocument()
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
    await waitFor(() => expect(screen.getByLabelText('扫描页处理')).toHaveFocus())

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
    await waitFor(() => expect(screen.getByLabelText('扫描页处理')).toHaveFocus())
    const done = screen.getByRole('button', { name: '完成' })
    done.focus()
    await user.keyboard('{Tab}')
    expect(screen.getByRole('button', { name: '关闭高级设置' })).toHaveFocus()
  })

  it('keeps embedded image descriptions opt-in', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(
      <ParseOptionsForm
        options={DEFAULT_OPTIONS}
        onChange={onChange}
        onSubmit={vi.fn()}
        sourceReady
        busy={false}
      />,
    )

    await user.click(screen.getByRole('button', { name: '高级设置' }))
    expect(screen.getByLabelText('图片描述语言')).toBeDisabled()
    await user.selectOptions(screen.getByLabelText('嵌入图片描述'), 'on')
    expect(onChange).toHaveBeenCalledWith({ ...DEFAULT_OPTIONS, describeImages: true })
  })

  it('exposes scan processing as an advanced opt-out', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(
      <ParseOptionsForm
        options={DEFAULT_OPTIONS}
        onChange={onChange}
        onSubmit={vi.fn()}
        sourceReady
        busy={false}
      />,
    )

    await user.click(screen.getByRole('button', { name: '高级设置' }))
    expect(screen.getByLabelText('扫描页处理')).toHaveValue('auto')
    await user.selectOptions(screen.getByLabelText('扫描页处理'), 'skip')
    expect(onChange).toHaveBeenCalledWith({ ...DEFAULT_OPTIONS, scanPolicy: 'skip' })
  })
})
