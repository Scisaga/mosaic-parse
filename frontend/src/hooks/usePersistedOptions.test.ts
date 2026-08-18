import { DEFAULT_OPTIONS, readOptions } from './usePersistedOptions'

describe('persisted workspace options', () => {
  beforeEach(() => localStorage.clear())

  it('loads only the current parse-result contract', () => {
    localStorage.setItem('mosaicparse.parse-options.v3', JSON.stringify({
      ...DEFAULT_OPTIONS,
      profile: 'accurate',
      mode: 'ocr',
      vlmPolicy: 'removed-value',
    }))

    expect(readOptions()).toEqual({ ...DEFAULT_OPTIONS, profile: 'accurate' })
  })
})
