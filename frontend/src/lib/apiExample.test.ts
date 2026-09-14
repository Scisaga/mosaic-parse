import { curlExample, pythonExample } from './apiExample'
import { DEFAULT_OPTIONS } from '../hooks/usePersistedOptions'

describe('API examples', () => {
  it('uses the asynchronous endpoint and current URL parameters', () => {
    const example = curlExample(
      { kind: 'url', file: null, url: 'https://example.com/a.pdf' },
      { ...DEFAULT_OPTIONS, scanPolicy: 'auto', unitRange: '1-2' },
      'http://localhost:12303',
    )
    expect(example).toContain('/v1/content/jobs')
    expect(example).toContain('/v1/content/jobs/$JOB_ID/result')
    expect(example).toContain('Accept: text/markdown')
    expect(example).toContain('result.md')
    expect(example).toContain('source_url=https://example.com/a.pdf')
    expect(example).toContain('unit_range=1-2')
    expect(example).toContain('scan_policy=auto')
    expect(example).not.toContain('profile=')
    expect(example).not.toContain('mode=')
  })

  it('produces a usable asynchronous Python file upload example', () => {
    const example = pythonExample(
      { kind: 'file', file: new File(['x'], 'report.pdf'), url: '' },
      DEFAULT_OPTIONS,
      'http://localhost:12303',
    )
    expect(example).toContain('/v1/content/jobs')
    expect(example).toContain("/v1/content/jobs/{job['id']}/result")
    expect(example).toContain('"Accept": "text/markdown"')
    expect(example).toContain('files={"file"')
    expect(example).toContain('report.pdf')
  })
})
