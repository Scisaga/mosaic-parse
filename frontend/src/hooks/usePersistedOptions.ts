import { useEffect, useState } from 'react'
import type { ParseOptions } from '../types/api'

const STORAGE_KEY = 'docling-glm.parse-options.v1'

export const DEFAULT_OPTIONS: ParseOptions = {
  mode: 'auto',
  profile: 'balanced',
  outputFormat: 'markdown',
  pageRange: '',
  language: 'zh,en',
  enableVlmFallback: false,
  preservePageBreaks: true,
  includePages: true,
  includeDiagnostics: true,
  timeoutSeconds: 300,
  submissionKind: 'async',
}

export function readOptions(): ParseOptions {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return DEFAULT_OPTIONS
    const saved = JSON.parse(raw) as Partial<ParseOptions>
    // The Web workspace always uses durable asynchronous jobs. Normalize old
    // browser preferences so a previously saved sync mode cannot silently
    // switch the submit endpoint back to the blocking API.
    return { ...DEFAULT_OPTIONS, ...saved, submissionKind: 'async' }
  } catch {
    return DEFAULT_OPTIONS
  }
}

export function usePersistedOptions() {
  const [options, setOptions] = useState<ParseOptions>(readOptions)
  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(options))
  }, [options])
  return [options, setOptions] as const
}
