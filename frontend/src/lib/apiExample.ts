import type { ParseOptions, SourceSelection } from '../types/api'

function shellQuote(value: string): string {
  return `'${value.replace(/'/g, `'"'"'`)}'`
}

function endpoint(kind: ParseOptions['submissionKind']): string {
  return kind === 'async' ? '/v1/documents/jobs' : '/v1/documents/parse'
}

function fields(options: ParseOptions): Array<[string, string]> {
  return [
    ['mode', options.mode],
    ['profile', options.profile],
    ['output_format', options.outputFormat],
    ...(options.pageRange.trim() ? [['page_range', options.pageRange.trim()] as [string, string]] : []),
    ['language', options.language],
    ['enable_vlm_fallback', String(options.enableVlmFallback)],
    ['preserve_page_breaks', String(options.preservePageBreaks)],
    ['include_pages', String(options.includePages)],
    ['include_diagnostics', String(options.includeDiagnostics)],
    ...(options.timeoutSeconds > 0 ? [['timeout_seconds', String(options.timeoutSeconds)] as [string, string]] : []),
  ]
}

export function curlExample(source: SourceSelection, options: ParseOptions, origin = 'http://localhost:12303'): string {
  const parts = [
    `curl --fail-with-body -X POST ${shellQuote(`${origin}${endpoint(options.submissionKind)}`)}`,
    '  -H "X-API-Key: ${DOCLING_GLM_API_KEY:-}"',
  ]
  if (source.kind === 'url') {
    parts.push(`  -F ${shellQuote(`source_url=${source.url || 'https://example.com/report.pdf'}`)}`)
  } else {
    parts.push(`  -F ${shellQuote(`file=@${source.file?.name || '/path/to/report.pdf'}`)}`)
  }
  for (const [key, value] of fields(options)) parts.push(`  -F ${shellQuote(`${key}=${value}`)}`)
  return parts.join(' \\\n')
}

export function pythonExample(source: SourceSelection, options: ParseOptions, origin = 'http://localhost:12303'): string {
  const optionLines = fields(options)
    .map(([key, value]) => `        ${JSON.stringify(key)}: ${JSON.stringify(value)},`)
    .join('\n')
  if (source.kind === 'url') {
    return `import os\n\nimport httpx\n\npayload = {\n        "source_url": ${JSON.stringify(source.url || 'https://example.com/report.pdf')},\n${optionLines}\n}\nresponse = httpx.post(\n    ${JSON.stringify(`${origin}${endpoint(options.submissionKind)}`)},\n    headers={"X-API-Key": os.getenv("DOCLING_GLM_API_KEY", "")},\n    data=payload,\n    timeout=${options.timeoutSeconds || 300},\n)\nresponse.raise_for_status()\nprint(response.json())`
  }
  return `import os\n\nimport httpx\n\nwith open(${JSON.stringify(source.file?.name || '/path/to/report.pdf')}, "rb") as document:\n    response = httpx.post(\n        ${JSON.stringify(`${origin}${endpoint(options.submissionKind)}`)},\n        headers={"X-API-Key": os.getenv("DOCLING_GLM_API_KEY", "")},\n        files={"file": (${JSON.stringify(source.file?.name || 'report.pdf')}, document)},\n        data={\n${optionLines}\n        },\n        timeout=${options.timeoutSeconds || 300},\n    )\nresponse.raise_for_status()\nprint(response.json())`
}
