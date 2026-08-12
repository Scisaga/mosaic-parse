import { DEFAULT_OPTIONS, readOptions } from './usePersistedOptions'

describe('persisted workspace options', () => {
  beforeEach(() => localStorage.clear())

  it('migrates a legacy sync preference to the async-only workspace policy', () => {
    localStorage.setItem('docling-glm.parse-options.v1', JSON.stringify({
      ...DEFAULT_OPTIONS,
      mode: 'ocr',
      submissionKind: 'sync',
    }))

    expect(readOptions()).toMatchObject({ mode: 'ocr', submissionKind: 'async' })
  })
})
