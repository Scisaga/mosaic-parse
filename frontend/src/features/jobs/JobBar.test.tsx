import { render, screen } from '@testing-library/react'
import type { DocumentJob } from '../../types/api'
import { JobBar } from './JobBar'

const callbacks = {
  onCancel: vi.fn(),
  onRetry: vi.fn(),
  onClear: vi.fn(),
}

function jobWithPhase(phase: string): DocumentJob {
  return {
    id: 'job_phase',
    status: 'running',
    progress: { current: 2, total: 5, phase },
  }
}

describe('JobBar progress phases', () => {
  it('labels text repair and diagram recognition instead of falling back to pages', () => {
    const { rerender } = render(<JobBar job={jobWithPhase('postprocess.text_repair')} {...callbacks} />)
    expect(screen.getByText('2/5 修复文字')).toBeInTheDocument()
    expect(screen.queryByText('2/5 页面')).not.toBeInTheDocument()

    rerender(<JobBar job={jobWithPhase('postprocess.diagram')} {...callbacks} />)
    expect(screen.getByText('2/5 识别图表')).toBeInTheDocument()
    expect(screen.queryByText('2/5 页面')).not.toBeInTheDocument()
  })
})
