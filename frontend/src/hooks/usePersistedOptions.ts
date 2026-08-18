import { useEffect, useState } from 'react'
import type { ParseOptions } from '../types/api'

const STORAGE_KEY = 'mosaicparse.parse-options.v3'

export const DEFAULT_OPTIONS: ParseOptions = {
  profile: 'balanced',
  unitRange: '',
  language: 'zh,en',
  descriptionLanguage: 'zh-CN',
  includeRenderings: true,
  timeoutSeconds: 300,
}

export function readOptions(): ParseOptions {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return DEFAULT_OPTIONS
    const saved = JSON.parse(raw) as Partial<ParseOptions>
    return {
      profile: saved.profile === 'fast' || saved.profile === 'balanced' || saved.profile === 'accurate'
        ? saved.profile
        : DEFAULT_OPTIONS.profile,
      unitRange: typeof saved.unitRange === 'string' ? saved.unitRange : DEFAULT_OPTIONS.unitRange,
      language: typeof saved.language === 'string' ? saved.language : DEFAULT_OPTIONS.language,
      descriptionLanguage: saved.descriptionLanguage === 'en' || saved.descriptionLanguage === 'auto'
        ? saved.descriptionLanguage
        : DEFAULT_OPTIONS.descriptionLanguage,
      includeRenderings: typeof saved.includeRenderings === 'boolean'
        ? saved.includeRenderings
        : DEFAULT_OPTIONS.includeRenderings,
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
