import { render, screen } from '@testing-library/react'

vi.mock('pdfjs-dist', () => ({
  GlobalWorkerOptions: { workerSrc: '' },
  getDocument: vi.fn(),
}))

vi.mock('pdfjs-dist/build/pdf.worker.min.mjs?url', () => ({ default: '/pdf.worker.mjs' }))

import { DocumentPreview } from './DocumentPreview'

describe('DocumentPreview empty states', () => {
  it('renders the branded input Copybot while waiting for a source', () => {
    const { container } = render(
      <DocumentPreview source={{ kind: 'file', file: null, url: '' }} />,
    )
    expect(screen.getByText('预览区等待文档')).toBeInTheDocument()
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
