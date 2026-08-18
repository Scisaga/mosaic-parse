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

function clamp(value: number) {
  return Math.max(0, Math.min(1, value))
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
  const angle = Math.PI * (.2 + hash(19, 41, seed) * .6)
  const directionX = Math.cos(angle)
  const directionY = Math.sin(angle)
  const projectionRadius = (Math.abs(directionX) + Math.abs(directionY)) / 2

  for (let row = 0; row < rows; row += 1) {
    for (let column = 0; column < columns; column += 1) {
      const horizontal = columns === 1 ? .5 : column / (columns - 1)
      const vertical = rows === 1 ? .5 : row / (rows - 1)
      const directional = projectionRadius === 0
        ? .5
        : ((horizontal - .5) * directionX + (vertical - .5) * directionY) / (projectionRadius * 2) + .5
      const broadNoise = valueNoise(horizontal * 3.2, vertical * 2.8, seed, 1)
      const localNoise = valueNoise(horizontal * 7.5, vertical * 5.5, seed ^ 0x6d2b79f5, 1)
      const lightNoise = valueNoise(horizontal * 4.4, vertical * 3.8, seed ^ 0x9e3779b9, 1) - .5
      const colorPosition = clamp(directional * .46 + broadNoise * .42 + localNoise * .12)
      const base = paletteColor(colorPosition)
      const lightness = lightNoise * 20 + (localNoise - .5) * 5
      const coolness = (broadNoise - .5) * 9
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
