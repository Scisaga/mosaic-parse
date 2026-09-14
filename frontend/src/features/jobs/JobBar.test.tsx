import { render, screen } from '@testing-library/react'
import type { ContentJob } from '../../types/api'
import { JobBar } from './JobBar'

const callbacks = {
  onCancel: vi.fn(),
  onRetry: vi.fn(),
  onClear: vi.fn(),
}

function jobWithPhase(phase: string): ContentJob {
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

    rerender(<JobBar job={jobWithPhase('postprocess.visual_fusion')} {...callbacks} />)
    expect(screen.getByText('2/5 融合视觉结果')).toBeInTheDocument()
    expect(screen.queryByText('2/5 页面')).not.toBeInTheDocument()
  })

  it('hides the normal live transport badge but keeps fallback status visible', () => {
    const { container, rerender } = render(<JobBar job={jobWithPhase('page_pipeline')} sseState="live" {...callbacks} />)
    expect(screen.queryByText('实时更新')).not.toBeInTheDocument()
    expect(container.querySelector('.transport-state')).not.toBeInTheDocument()

    rerender(<JobBar job={jobWithPhase('page_pipeline')} sseState="fallback" {...callbacks} />)
    expect(screen.getByText('轮询更新')).toBeInTheDocument()
  })
})
