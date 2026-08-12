import { rangeIncludes, validatePageRange } from './pageRange'

describe('validatePageRange', () => {
  it('accepts empty and disjoint page ranges', () => {
    expect(validatePageRange('')).toBeNull()
    expect(validatePageRange('1-5, 8, 10-12')).toBeNull()
  })

  it('rejects zero, reversed, and malformed ranges', () => {
    expect(validatePageRange('0')).toMatch(/从 1 开始/)
    expect(validatePageRange('5-2')).toMatch(/终点/)
    expect(validatePageRange('1..5')).toMatch(/请输入/)
  })

  it('checks page membership without expanding ranges', () => {
    expect(rangeIncludes('1-3,9', 2)).toBe(true)
    expect(rangeIncludes('1-3,9', 4)).toBe(false)
  })
})
