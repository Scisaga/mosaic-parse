import { useEffect, useState } from 'react'
import type { ParseOptions } from '../types/api'

const STORAGE_KEY = 'mosaicparse.parse-options.v6'
const LEGACY_STORAGE_KEY = 'mosaicparse.parse-options.v5'

export const DEFAULT_OPTIONS: ParseOptions = {
  scanPolicy: 'auto',
  unitRange: '',
  language: 'zh,en',
  describeImages: false,
  descriptionLanguage: 'zh-CN',
  timeoutSeconds: 300,
}

export function readOptions(): ParseOptions {
  try {
    const raw = localStorage.getItem(STORAGE_KEY) ?? localStorage.getItem(LEGACY_STORAGE_KEY)
    if (!raw) return DEFAULT_OPTIONS
    const saved = JSON.parse(raw) as Partial<ParseOptions>
    return {
      scanPolicy: saved.scanPolicy === 'auto' || saved.scanPolicy === 'skip'
        ? saved.scanPolicy
        : DEFAULT_OPTIONS.scanPolicy,
      unitRange: typeof saved.unitRange === 'string' ? saved.unitRange : DEFAULT_OPTIONS.unitRange,
      language: typeof saved.language === 'string' ? saved.language : DEFAULT_OPTIONS.language,
      describeImages: typeof saved.describeImages === 'boolean'
        ? saved.describeImages
        : DEFAULT_OPTIONS.describeImages,
      descriptionLanguage: saved.descriptionLanguage === 'en' || saved.descriptionLanguage === 'auto'
        ? saved.descriptionLanguage
        : DEFAULT_OPTIONS.descriptionLanguage,
      timeoutSeconds: typeof saved.timeoutSeconds === 'number'
        ? saved.timeoutSeconds
        : DEFAULT_OPTIONS.timeoutSeconds,
    }
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
