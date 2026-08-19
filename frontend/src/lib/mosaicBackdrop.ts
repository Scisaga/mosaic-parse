type Rgb = readonly [number, number, number]

export interface MosaicCell {
  color: string
  height: number
  width: number
  x: number
  y: number
}

const PALETTE: readonly Rgb[] = [
  [7, 26, 46],
  [35, 73, 105],
  [18, 112, 132],
  [44, 174, 187],
  [35, 105, 143],
  [43, 66, 108],
  [8, 28, 49],
]

const ACCENT_PALETTE: readonly Rgb[] = [
  [45, 139, 83],
  [21, 132, 137],
  [150, 70, 185],
  [96, 68, 163],
  [44, 82, 132],
  [192, 156, 57],
  [181, 154, 61],
  [80, 146, 74],
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

function paletteColor(palette: readonly Rgb[], position: number): Rgb {
  const scaled = Math.max(0, Math.min(1, position)) * (palette.length - 1)
  const index = Math.min(palette.length - 2, Math.floor(scaled))
  const amount = scaled - index
  return palette[index].map((channel, channelIndex) => (
    Math.round(lerp(channel, palette[index + 1][channelIndex], amount))
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

function mixColor(base: Rgb, accent: Rgb, amount: number): Rgb {
  return base.map((channel, index) => (
    Math.round(lerp(channel, accent[index], amount))
  )) as unknown as Rgb
}

function accentAt(horizontal: number, vertical: number, seed: number) {
  const broadHue = valueNoise(horizontal * 2.7, vertical * 2.35, seed ^ 0xc2b2ae35, 1)
  const localHue = valueNoise(horizontal * 7.2, vertical * 5.4, seed ^ 0x27d4eb2d, 1)
  const broadCoverage = valueNoise(horizontal * 3.4, vertical * 2.8, seed ^ 0x165667b1, 1)
  const localCoverage = valueNoise(horizontal * 8.4, vertical * 5.8, seed ^ 0xd3a2646c, 1)
  const position = clamp(.5 + (broadHue * .58 + localHue * .42 - .5) * 2.35)
  const coverage = broadCoverage * .72 + localCoverage * .28
  return {
    color: paletteColor(ACCENT_PALETTE, position),
    strength: .18 + smooth(clamp((coverage - .16) / .76)) * .62,
  }
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
      const localNoise = valueNoise(horizontal * 11, vertical * 7.5, seed ^ 0x6d2b79f5, 1)
      const lightNoise = valueNoise(horizontal * 4.4, vertical * 3.8, seed ^ 0x9e3779b9, 1) - .5
      const tileNoise = hash(column, row, seed ^ 0x85ebca6b) - .5
      const colorField = directional * .2 + broadNoise * .45 + localNoise * .35
      const colorPosition = clamp(.5 + (colorField - .5) * 1.75 + tileNoise * .14)
      const accent = accentAt(horizontal, vertical, seed)
      const baseColor = paletteColor(PALETTE, colorPosition)
      const base = mixColor(baseColor, accent.color, accent.strength)
      const lightness = lightNoise * 24 + (localNoise - .5) * 10 + tileNoise * 24
      const coolness = (broadNoise - .5) * 12 + tileNoise * 8
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
