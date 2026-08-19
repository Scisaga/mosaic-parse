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

const ACCENTS: readonly Rgb[] = [
  [48, 153, 82],
  [132, 72, 172],
  [205, 157, 38],
  [207, 83, 46],
]

interface AccentBlob {
  color: Rgb
  radiusX: number
  radiusY: number
  x: number
  y: number
}

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

function mixColor(base: Rgb, accent: Rgb, amount: number): Rgb {
  return base.map((channel, index) => (
    Math.round(lerp(channel, accent[index], amount))
  )) as unknown as Rgb
}

function createAccentBlobs(columns: number, rows: number, seed: number): AccentBlob[] {
  const ordered = ACCENTS
    .map((color, index) => ({ color, index, order: hash(index, 73, seed ^ 0xc2b2ae35) }))
    .sort((left, right) => left.order - right.order)

  return ordered.map(({ color, index }, slot) => ({
    color,
    radiusX: 2.8 + hash(index, 89, seed) * 3.8,
    radiusY: 1.4 + hash(index, 97, seed) * 1.5,
    x: ((slot + .15 + hash(index, 101, seed) * .7) / ordered.length) * Math.max(0, columns - 1),
    y: hash(index, 107, seed) * Math.max(0, rows - 1),
  }))
}

function accentAt(column: number, row: number, blobs: AccentBlob[]) {
  let color: Rgb | null = null
  let strength = 0
  for (const blob of blobs) {
    const distanceX = (column - blob.x) / blob.radiusX
    const distanceY = (row - blob.y) / blob.radiusY
    const distance = Math.sqrt(distanceX ** 2 + distanceY ** 2)
    if (distance >= 1) continue
    const candidateStrength = smooth(1 - distance) * .84
    if (candidateStrength > strength) {
      color = blob.color
      strength = candidateStrength
    }
  }
  return { color, strength }
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
  const accentBlobs = createAccentBlobs(columns, rows, seed)

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
      const accent = accentAt(column, row, accentBlobs)
      const baseColor = paletteColor(colorPosition)
      const base = accent.color ? mixColor(baseColor, accent.color, accent.strength) : baseColor
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
