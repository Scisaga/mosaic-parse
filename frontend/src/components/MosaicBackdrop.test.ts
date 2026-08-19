import { generateMosaicCells } from '../lib/mosaicBackdrop'

function channels(color: string) {
  return color.match(/\d+/g)?.map(Number) ?? []
}

function distance(left: string, right: string) {
  const a = channels(left)
  const b = channels(right)
  return Math.sqrt(a.reduce((total, channel, index) => total + (channel - b[index]) ** 2, 0))
}

function brightness(color: string) {
  const [red, green, blue] = channels(color)
  return red * .2126 + green * .7152 + blue * .0722
}

function hasAccent(cells: ReturnType<typeof generateMosaicCells>, accent: 'green' | 'purple' | 'yellow' | 'orange') {
  return cells.some(({ color }) => {
    const [red, green, blue] = channels(color)
    if (accent === 'green') return green > red * 1.28 && green > blue * 1.08
    if (accent === 'purple') return red > green * 1.16 && blue > green * 1.2
    if (accent === 'yellow') return red > blue * 1.8 && green > blue * 1.45 && red - green < 75
    return red > green * 1.5 && green > blue * 1.08
  })
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
    const brightnessValues = first.map((cell) => brightness(cell.color))
    const average = (values: number[]) => values.reduce((sum, value) => sum + value, 0) / values.length
    expect(new Set(middleColumnColors).size).toBeGreaterThan(rows / 2)
    expect(Math.max(...brightnessValues) - Math.min(...brightnessValues)).toBeGreaterThan(80)
    expect(horizontalNeighbours.filter((value) => value > 6).length).toBeGreaterThan(first.length * .6)
    expect(average(horizontalNeighbours)).toBeLessThan(average(farDistances))
    expect(average(verticalNeighbours)).toBeLessThan(average(farDistances))
  })

  it('adds sparse green, purple, yellow, and orange accent clusters', () => {
    const cells = generateMosaicCells(1440, 120, 12, 42)
    expect(hasAccent(cells, 'green')).toBe(true)
    expect(hasAccent(cells, 'purple')).toBe(true)
    expect(hasAccent(cells, 'yellow')).toBe(true)
    expect(hasAccent(cells, 'orange')).toBe(true)
  })
})
