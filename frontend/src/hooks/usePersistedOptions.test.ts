import { DEFAULT_OPTIONS, readOptions } from './usePersistedOptions'

describe('persisted workspace options', () => {
  beforeEach(() => localStorage.clear())

  it('loads only the current Markdown option contract', () => {
    localStorage.setItem('mosaicparse.parse-options.v6', JSON.stringify({
      ...DEFAULT_OPTIONS,
      scanPolicy: 'skip',
      mode: 'ocr',
      vlmPolicy: 'removed-value',
    }))

    expect(readOptions()).toEqual({ ...DEFAULT_OPTIONS, scanPolicy: 'skip' })
  })

  it('migrates all removed profile choices to automatic scan routing', () => {
    localStorage.setItem('mosaicparse.parse-options.v5', JSON.stringify({
      ...DEFAULT_OPTIONS,
      profile: 'fast',
      language: 'zh',
    }))

    expect(readOptions()).toEqual({ ...DEFAULT_OPTIONS, scanPolicy: 'auto', language: 'zh' })
  })
})
