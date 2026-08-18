import { curlExample, pythonExample } from './apiExample'
import { DEFAULT_OPTIONS } from '../hooks/usePersistedOptions'

describe('API examples', () => {
  it('uses the asynchronous endpoint and current URL parameters', () => {
    const example = curlExample(
      { kind: 'url', file: null, url: 'https://example.com/a.pdf' },
      { ...DEFAULT_OPTIONS, profile: 'fast', unitRange: '1-2' },
      'http://localhost:12303',
    )
    expect(example).toContain('/v1/content/jobs')
    expect(example).toContain('source_url=https://example.com/a.pdf')
    expect(example).toContain('unit_range=1-2')
    expect(example).not.toContain('mode=')
  })

  it('produces a usable asynchronous Python file upload example', () => {
    const example = pythonExample(
      { kind: 'file', file: new File(['x'], 'report.pdf'), url: '' },
      DEFAULT_OPTIONS,
      'http://localhost:12303',
    )
    expect(example).toContain('/v1/content/jobs')
    expect(example).toContain('files={"file"')
    expect(example).toContain('report.pdf')
  })
})
