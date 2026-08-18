import { generateMosaicCells } from '../lib/mosaicBackdrop'

function channels(color: string) {
  return color.match(/\d+/g)?.map(Number) ?? []
}

function distance(left: string, right: string) {
  const a = channels(left)
  const b = channels(right)
  return Math.sqrt(a.reduce((total, channel, index) => total + (channel - b[index]) ** 2, 0))
}

describe('MosaicBackdrop color field', () => {
  it('is seeded, varied, and more coherent locally than across the full gradient', () => {
    const first = generateMosaicCells(480, 72, 12, 42)
    const repeated = generateMosaicCells(480, 72, 12, 42)
    const alternate = generateMosaicCells(480, 72, 12, 84)
    expect(first).toEqual(repeated)
    expect(first.map((cell) => cell.color)).not.toEqual(alternate.map((cell) => cell.color))

    const columns = 40
    const neighbourDistances = first.flatMap((cell, index) => (
      index % columns === columns - 1 ? [] : [distance(cell.color, first[index + 1].color)]
    ))
    const farDistances = first.slice(0, columns).map((cell, index) => (
      distance(cell.color, first[index + Math.floor(columns / 2)].color)
    ))
    const average = (values: number[]) => values.reduce((sum, value) => sum + value, 0) / values.length
    expect(average(neighbourDistances)).toBeLessThan(average(farDistances))
  })
})
