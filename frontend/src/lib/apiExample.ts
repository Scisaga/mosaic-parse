import type { ParseOptions, SourceSelection } from '../types/api'

function shellQuote(value: string): string {
  return `'${value.replace(/'/g, `'"'"'`)}'`
}

function fields(options: ParseOptions): Array<[string, string]> {
  return [
    ['profile', options.profile],
    ...(options.unitRange.trim() ? [['unit_range', options.unitRange.trim()] as [string, string]] : []),
    ['language', options.language],
    ['description_language', options.descriptionLanguage],
    ['include_renderings', String(options.includeRenderings)],
    ...(options.timeoutSeconds > 0 ? [['timeout_seconds', String(options.timeoutSeconds)] as [string, string]] : []),
  ]
}

export function curlExample(source: SourceSelection, options: ParseOptions, origin = 'http://localhost:12303'): string {
  const parts = [
    `curl --fail-with-body -X POST ${shellQuote(`${origin}/v1/content/jobs`)}`,
    '  -H "X-API-Key: ${MOSAICPARSE_API_KEY:-}"',
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
    return `import os\n\nimport httpx\n\npayload = {\n        "source_url": ${JSON.stringify(source.url || 'https://example.com/report.pdf')},\n${optionLines}\n}\nresponse = httpx.post(\n    ${JSON.stringify(`${origin}/v1/content/jobs`)},\n    headers={"X-API-Key": os.getenv("MOSAICPARSE_API_KEY", "")},\n    data=payload,\n    timeout=${options.timeoutSeconds || 300},\n)\nresponse.raise_for_status()\nprint(response.json())`
  }
  return `import os\n\nimport httpx\n\nwith open(${JSON.stringify(source.file?.name || '/path/to/report.pdf')}, "rb") as content:\n    response = httpx.post(\n        ${JSON.stringify(`${origin}/v1/content/jobs`)},\n        headers={"X-API-Key": os.getenv("MOSAICPARSE_API_KEY", "")},\n        files={"file": (${JSON.stringify(source.file?.name || 'report.pdf')}, content)},\n        data={\n${optionLines}\n        },\n        timeout=${options.timeoutSeconds || 300},\n    )\nresponse.raise_for_status()\nprint(response.json())`
}
