import { render, screen } from '@testing-library/react'
import { WorkspaceState, type WorkspaceStateVariant } from './WorkspaceState'

const variantScenes: Array<[WorkspaceStateVariant, 'markdown' | 'text' | undefined, string]> = [
  ['input-empty', undefined, 'input'],
  ['output-empty', undefined, 'text'],
  ['pages-empty', undefined, 'pages'],
  ['loading', undefined, 'loading'],
  ['info', undefined, 'info'],
  ['error', undefined, 'error'],
]

describe('WorkspaceState', () => {
  it.each(variantScenes)('maps %s to its decorative PNG scene', (variant, contentKind, scene) => {
    const { container } = render(
      <WorkspaceState variant={variant} contentKind={contentKind} title="状态标题" description="状态说明" />,
    )
    const art = container.querySelector('img.workspace-state-art')
    expect(art).toHaveAttribute('src', `/illustrations/workspace-${scene}.png`)
    expect(art).toHaveAttribute('data-scene', scene)
    expect(art).toHaveAttribute('alt', '')
    expect(art).toHaveAttribute('aria-hidden', 'true')
    expect(art).toHaveAttribute('draggable', 'false')
    expect(art).toHaveAttribute('width', '640')
    expect(art).toHaveAttribute('height', '512')
    expect(container.querySelector('svg')).not.toBeInTheDocument()
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
  })

  it('uses distinct assets for all four core empty-state compositions', () => {
    const { container, rerender } = render(<WorkspaceState variant="input-empty" description="Input" />)
    expect(container.querySelector('img')).toHaveAttribute('src', '/illustrations/workspace-input.png')

    rerender(<WorkspaceState variant="output-empty" contentKind="markdown" description="Markdown" />)
    expect(container.querySelector('img')).toHaveAttribute('src', '/illustrations/workspace-markdown.png')

    rerender(<WorkspaceState variant="output-empty" contentKind="text" description="Text" />)
    expect(container.querySelector('img')).toHaveAttribute('src', '/illustrations/workspace-text.png')

    rerender(<WorkspaceState variant="pages-empty" description="Pages" />)
    expect(container.querySelector('img')).toHaveAttribute('src', '/illustrations/workspace-pages.png')
  })

  it('keeps live loading and error semantics on the wrapper only', () => {
    const { rerender } = render(<WorkspaceState variant="loading" description="正在加载" role="status" live="polite" busy />)
    const status = screen.getByRole('status')
    expect(status).toHaveAttribute('aria-live', 'polite')
    expect(status).toHaveAttribute('aria-busy', 'true')
    expect(status.querySelector('img')).toHaveAttribute('src', '/illustrations/workspace-loading.png')
    expect(status.querySelector('img')).toHaveAttribute('aria-hidden', 'true')

    rerender(<WorkspaceState variant="error" title="失败" description="错误详情" role="alert" live="assertive" />)
    const alert = screen.getByRole('alert')
    expect(alert).toHaveAttribute('aria-live', 'assertive')
    expect(alert.querySelector('img')).toHaveAttribute('src', '/illustrations/workspace-error.png')
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
  })
})
