import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { SourcePicker } from './SourcePicker'

describe('SourcePicker', () => {
  it('exposes URL validation inline and supports arrow-key tab selection', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    const { rerender } = render(
      <SourcePicker
        source={{ kind: 'file', file: null, url: '' }}
        onChange={onChange}
        validationError="请输入文档 URL"
      />,
    )

    const uploadTab = screen.getByRole('tab', { name: '上传' })
    uploadTab.focus()
    await user.keyboard('{ArrowRight}')
    expect(onChange).toHaveBeenCalledWith({ kind: 'url', file: null, url: '' })

    rerender(
      <SourcePicker
        source={{ kind: 'url', file: null, url: '' }}
        onChange={onChange}
        validationError="请输入文档 URL"
        validationVisible
      />,
    )
    const input = screen.getByLabelText('文档 URL')
    expect(input).toHaveAttribute('aria-invalid', 'true')
    expect(input).toHaveAccessibleDescription('请输入文档 URL')
    expect(screen.getByRole('alert')).toHaveTextContent('请输入文档 URL')
  })
})
