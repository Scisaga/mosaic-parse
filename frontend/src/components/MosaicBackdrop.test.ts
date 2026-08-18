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
  it('is seeded, two-dimensional, and more coherent locally than across the full field', () => {
    const first = generateMosaicCells(480, 120, 12, 42)
    const repeated = generateMosaicCells(480, 120, 12, 42)
    const alternate = generateMosaicCells(480, 120, 12, 84)
    expect(first).toEqual(repeated)
    expect(first.map((cell) => cell.color)).not.toEqual(alternate.map((cell) => cell.color))

    const columns = 40
    const rows = 10
    const horizontalNeighbours = first.flatMap((cell, index) => (
      index % columns === columns - 1 ? [] : [distance(cell.color, first[index + 1].color)]
    ))
    const verticalNeighbours = first.slice(0, -columns).map((cell, index) => (
      distance(cell.color, first[index + columns].color)
    ))
    const farDistances = first.slice(0, columns).map((cell, column) => (
      distance(cell.color, first[(rows - 1) * columns + (columns - 1 - column)].color)
    ))
    const middleColumnColors = Array.from({ length: rows }, (_, row) => first[row * columns + 20].color)
    const average = (values: number[]) => values.reduce((sum, value) => sum + value, 0) / values.length
    expect(new Set(middleColumnColors).size).toBeGreaterThan(rows / 2)
    expect(average(horizontalNeighbours)).toBeLessThan(average(farDistances))
    expect(average(verticalNeighbours)).toBeLessThan(average(farDistances))
  })
})
