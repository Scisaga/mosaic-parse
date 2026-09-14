import type { ParseOptions, SourceSelection } from '../types/api'

function shellQuote(value: string): string {
  return `'${value.replace(/'/g, `'"'"'`)}'`
}

function fields(options: ParseOptions): Array<[string, string]> {
  return [
    ['scan_policy', options.scanPolicy],
    ...(options.unitRange.trim() ? [['unit_range', options.unitRange.trim()] as [string, string]] : []),
    ['language', options.language],
    ['describe_images', String(options.describeImages)],
    ['description_language', options.descriptionLanguage],
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
  const create = parts.join(' \\\n')
  return [
    `JOB_ID=$(${create} | jq -r '.id')`,
    '',
    '# Poll GET /v1/content/jobs/$JOB_ID until completed or partial, then:',
    `curl --fail-with-body "${origin}/v1/content/jobs/$JOB_ID/result" \\\n  -H "X-API-Key: \${MOSAICPARSE_API_KEY:-}" \\\n  -H 'Accept: text/markdown' \\\n  -o result.md`,
  ].join('\n')
}

export function pythonExample(source: SourceSelection, options: ParseOptions, origin = 'http://localhost:12303'): string {
  const optionLines = fields(options)
    .map(([key, value]) => `        ${JSON.stringify(key)}: ${JSON.stringify(value)},`)
    .join('\n')
  const tail = `\nresponse.raise_for_status()\njob = response.json()\nprint(f"created {job['id']}; poll {job['status_url']}")\n\n# After the job reaches completed or partial:\nmarkdown = httpx.get(\n    f"${origin}/v1/content/jobs/{job['id']}/result",\n    headers={\n        "X-API-Key": os.getenv("MOSAICPARSE_API_KEY", ""),\n        "Accept": "text/markdown",\n    },\n    timeout=${options.timeoutSeconds || 300},\n)\nmarkdown.raise_for_status()\nprint(markdown.text)`
  if (source.kind === 'url') {
    return `import os\n\nimport httpx\n\npayload = {\n        "source_url": ${JSON.stringify(source.url || 'https://example.com/report.pdf')},\n${optionLines}\n}\nresponse = httpx.post(\n    ${JSON.stringify(`${origin}/v1/content/jobs`)},\n    headers={"X-API-Key": os.getenv("MOSAICPARSE_API_KEY", "")},\n    data=payload,\n    timeout=${options.timeoutSeconds || 300},\n)${tail}`
  }
  return `import os\n\nimport httpx\n\nwith open(${JSON.stringify(source.file?.name || '/path/to/report.pdf')}, "rb") as content:\n    response = httpx.post(\n        ${JSON.stringify(`${origin}/v1/content/jobs`)},\n        headers={"X-API-Key": os.getenv("MOSAICPARSE_API_KEY", "")},\n        files={"file": (${JSON.stringify(source.file?.name || 'report.pdf')}, content)},\n        data={\n${optionLines}\n        },\n        timeout=${options.timeoutSeconds || 300},\n    )${tail}`
}
