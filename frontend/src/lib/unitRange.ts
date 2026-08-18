const PAGE_TOKEN = /^(\d+)(?:-(\d+))?$/

export function validatePageRange(value: string): string | null {
  const trimmed = value.trim()
  if (!trimmed) return null
  for (const rawToken of trimmed.split(',')) {
    const token = rawToken.trim()
    const match = PAGE_TOKEN.exec(token)
    if (!match) return '请输入如 1-5,8,10-12 的页码范围'
    const start = Number(match[1])
    const end = match[2] ? Number(match[2]) : start
    if (start < 1 || end < start) return '页码必须从 1 开始，且区间终点不能小于起点'
  }
  return null
}

export function rangeIncludes(value: string, page: number): boolean {
  if (!value.trim()) return true
  return value.split(',').some((rawToken) => {
    const match = PAGE_TOKEN.exec(rawToken.trim())
    if (!match) return false
    const start = Number(match[1])
    const end = match[2] ? Number(match[2]) : start
    return page >= start && page <= end
  })
}
