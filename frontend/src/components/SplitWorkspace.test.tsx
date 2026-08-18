import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { SplitWorkspace } from './SplitWorkspace'

describe('SplitWorkspace', () => {
  it('starts at the 44/56 result-priority split and keeps keyboard resizing', async () => {
    const user = userEvent.setup()
    const { container } = render(
      <SplitWorkspace preview={<div>预览</div>} result={<div>结果</div>} />,
    )
    const workspace = container.querySelector<HTMLElement>('.split-workspace')
    const splitter = screen.getByRole('separator', { name: '调整预览和结果宽度' })

    expect(workspace?.style.gridTemplateColumns).toBe('calc(44% - 6px) 12px calc(56% - 6px)')
    expect(splitter).toHaveAttribute('aria-valuenow', '44')
    splitter.focus()
    await user.keyboard('{ArrowRight}')
    expect(splitter).toHaveAttribute('aria-valuenow', '46')
    await user.keyboard('{Home}')
    expect(splitter).toHaveAttribute('aria-valuenow', '44')
  })
})
