type Rgb = readonly [number, number, number]

export interface MosaicCell {
  color: string
  height: number
  width: number
  x: number
  y: number
}

const PALETTE: readonly Rgb[] = [
  [10, 31, 51],
  [24, 67, 88],
  [30, 100, 112],
  [25, 133, 148],
  [38, 117, 137],
  [25, 63, 86],
  [9, 29, 48],
]

function hash(x: number, y: number, seed: number) {
  let value = Math.imul(x ^ seed, 0x45d9f3b) ^ Math.imul(y + seed, 0x27d4eb2d)
  value = Math.imul(value ^ (value >>> 16), 0x45d9f3b)
  return ((value ^ (value >>> 16)) >>> 0) / 0xffffffff
}

function smooth(value: number) {
  return value * value * (3 - 2 * value)
}

function lerp(left: number, right: number, amount: number) {
  return left + (right - left) * amount
}

function valueNoise(x: number, y: number, seed: number, scale: number) {
  const gridX = Math.floor(x / scale)
  const gridY = Math.floor(y / scale)
  const amountX = smooth((x % scale) / scale)
  const amountY = smooth((y % scale) / scale)
  const top = lerp(hash(gridX, gridY, seed), hash(gridX + 1, gridY, seed), amountX)
  const bottom = lerp(hash(gridX, gridY + 1, seed), hash(gridX + 1, gridY + 1, seed), amountX)
  return lerp(top, bottom, amountY)
}

function paletteColor(position: number): Rgb {
  const scaled = Math.max(0, Math.min(1, position)) * (PALETTE.length - 1)
  const index = Math.min(PALETTE.length - 2, Math.floor(scaled))
  const amount = scaled - index
  return PALETTE[index].map((channel, channelIndex) => (
    Math.round(lerp(channel, PALETTE[index + 1][channelIndex], amount))
  )) as unknown as Rgb
}

function cssColor(base: Rgb, lightness: number, coolness: number) {
  const channels = base.map((channel, index) => {
    const coolShift = index === 2 ? coolness : index === 0 ? -coolness * .45 : 0
    return Math.max(0, Math.min(255, Math.round(channel + lightness + coolShift)))
  })
  return `rgb(${channels.join(' ')})`
}

export function generateMosaicCells(
  width: number,
  height: number,
  tileSize: number,
  seed: number,
): MosaicCell[] {
  const columns = Math.max(1, Math.ceil(width / tileSize))
  const rows = Math.max(1, Math.ceil(height / tileSize))
  const gap = Math.max(1, tileSize * .11)
  const cells: MosaicCell[] = []

  for (let row = 0; row < rows; row += 1) {
    for (let column = 0; column < columns; column += 1) {
      const horizontal = columns === 1 ? 0 : column / (columns - 1)
      const broadNoise = valueNoise(column, row, seed, 8) - .5
      const localNoise = valueNoise(column, row, seed ^ 0x6d2b79f5, 3) - .5
      const verticalShade = rows === 1 ? 0 : (row / (rows - 1) - .5) * 5
      const base = paletteColor(horizontal)
      const lightness = broadNoise * 24 + localNoise * 7 + verticalShade
      const coolness = broadNoise * 7
      cells.push({
        color: cssColor(base, lightness, coolness),
        height: Math.max(1, tileSize - gap),
        width: Math.max(1, tileSize - gap),
        x: column * tileSize + gap / 2,
        y: row * tileSize + gap / 2,
      })
    }
  }
  return cells
}
