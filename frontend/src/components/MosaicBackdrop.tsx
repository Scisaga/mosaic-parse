import { useEffect, useRef } from 'react'
import { generateMosaicCells } from '../lib/mosaicBackdrop'

function randomSeed() {
  if (typeof crypto !== 'undefined' && crypto.getRandomValues) {
    return crypto.getRandomValues(new Uint32Array(1))[0]
  }
  return Math.floor(Math.random() * 0xffffffff)
}

function draw(canvas: HTMLCanvasElement, seed: number) {
  const bounds = canvas.getBoundingClientRect()
  if (bounds.width < 1 || bounds.height < 1) return
  const ratio = Math.min(window.devicePixelRatio || 1, 2)
  canvas.width = Math.ceil(bounds.width * ratio)
  canvas.height = Math.ceil(bounds.height * ratio)
  const context = canvas.getContext('2d')
  if (!context) return
  context.setTransform(ratio, 0, 0, ratio, 0, 0)
  context.clearRect(0, 0, bounds.width, bounds.height)
  context.fillStyle = '#071a2c'
  context.fillRect(0, 0, bounds.width, bounds.height)
  const tileSize = bounds.width <= 600 ? 11 : 12
  const cells = generateMosaicCells(bounds.width, bounds.height, tileSize, seed)
  for (const cell of cells) {
    context.fillStyle = cell.color
    context.beginPath()
    context.roundRect(cell.x, cell.y, cell.width, cell.height, 1.6)
    context.fill()
    context.fillStyle = 'rgba(255, 255, 255, .055)'
    context.fillRect(cell.x + 1, cell.y + 1, Math.max(0, cell.width - 2), .7)
  }
}

export function MosaicBackdrop() {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const seedRef = useRef<number | null>(null)
  if (seedRef.current === null) seedRef.current = randomSeed()

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const render = () => draw(canvas, seedRef.current ?? 0)
    render()
    if (typeof ResizeObserver === 'undefined') {
      window.addEventListener('resize', render)
      return () => window.removeEventListener('resize', render)
    }
    const observer = new ResizeObserver(render)
    observer.observe(canvas)
    return () => observer.disconnect()
  }, [])

  return <canvas ref={canvasRef} className="header-mosaic" aria-hidden="true" />
}
