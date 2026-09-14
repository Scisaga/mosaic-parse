import { fireEvent, render, screen, waitFor } from '@testing-library/react'

vi.mock('pdfjs-dist', () => ({
  GlobalWorkerOptions: { workerSrc: '' },
  getDocument: vi.fn(),
}))

vi.mock('pdfjs-dist/build/pdf.worker.min.mjs?url', () => ({ default: '/pdf.worker.mjs' }))

import { getDocument } from 'pdfjs-dist'
import { DocumentPreview } from './DocumentPreview'

describe('DocumentPreview empty states', () => {
  it('renders the branded input Copybot while waiting for a source', () => {
    const { container } = render(
      <DocumentPreview source={{ kind: 'file', file: null, url: '' }} />,
    )
    expect(screen.getByText('预览区等待内容')).toBeInTheDocument()
    expect(container.querySelector('.workspace-state-input-empty img')).toHaveAttribute('src', '/illustrations/workspace-input.png')
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
  })

  it('uses an informational Copybot for TIFF without reporting an error', () => {
    const file = new File(['tiff'], 'scan.tiff', { type: 'image/tiff' })
    const { container } = render(
      <DocumentPreview source={{ kind: 'file', file, url: '' }} />,
    )
    expect(screen.getByText('TIFF 已选择')).toBeInTheDocument()
    expect(container.querySelector('.workspace-state-info img')).toHaveAttribute('src', '/illustrations/workspace-info.png')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})

describe('DocumentPreview PDF flow', () => {
  it('creates a fit-width continuous page stream and uses navigation as scroll targets', async () => {
    const onPageChange = vi.fn()
    const onMetadata = vi.fn()
    const destroy = vi.fn()
    vi.mocked(getDocument).mockReturnValue({
      promise: Promise.resolve({ numPages: 3 }),
      destroy,
    } as never)

    const file = new File(['pdf'], 'report.pdf', { type: 'application/pdf' })
    const { container, unmount } = render(
      <DocumentPreview
        source={{ kind: 'file', file, url: '' }}
        onPageChange={onPageChange}
        onMetadata={onMetadata}
      />,
    )

    await waitFor(() => expect(container.querySelectorAll('[data-pdf-page]')).toHaveLength(3))
    expect(screen.getByLabelText('PDF 连续预览，共 3 页')).toHaveStyle({ width: '100%' })
    expect(screen.getByTitle('相对于容器适宽')).toHaveTextContent('适宽')
    expect(onMetadata).toHaveBeenCalledWith(3)

    fireEvent.click(screen.getByRole('button', { name: '下一页' }))
    expect(screen.getByRole('spinbutton', { name: '当前页' })).toHaveValue(2)
    expect(onPageChange).toHaveBeenCalledWith(2)
    expect(container.querySelector('[data-pdf-page="2"]')).toHaveAttribute('aria-current', 'page')

    unmount()
    expect(destroy).toHaveBeenCalled()
  })
})
