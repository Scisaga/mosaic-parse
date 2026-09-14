import styles from './index.css?raw'

describe('desktop control deck layout contract', () => {
  it('keeps cards aligned without clipping content and restores natural mobile flow', () => {
    expect(styles).toContain('--control-card-min-height: 112px')
    expect(styles).toContain('.source-picker, .options-panel { min-height: var(--control-card-min-height); }')
    expect(styles).not.toContain('.source-picker, .options-panel { height: var(--control-card-height); }')
    expect(styles).toContain('.control-deck { --control-card-min-height: 112px; flex: none; align-items: stretch; }')
    expect(styles).toContain('grid-template-columns: minmax(360px, 40fr) minmax(600px, 60fr)')
    expect(styles).toContain('.options-content { display: grid; grid-template-columns: minmax(240px, 320px) 148px;')
    expect(styles).toContain('.options-grid { width: 100%; max-width: 320px; display: grid; grid-template-columns: minmax(0, 1fr);')
    expect(styles).toContain('.control-deck { grid-template-columns: 1fr; align-items: start; }')
  })
})

describe('PDF preview layout contract', () => {
  it('fits a continuous page stream to the preview width', () => {
    expect(styles).toContain('.preview-stage-pdf { display: block; padding: 24px; scroll-behavior: smooth; scrollbar-gutter: stable; }')
    expect(styles).toContain('.pdf-document { min-width: 0; display: grid; gap: 18px; margin-inline: auto; }')
    expect(styles).toContain('.pdf-page-shell { width: 100%; position: relative; overflow: hidden;')
    expect(styles).toContain('.pdf-page-shell canvas { width: 100%; height: auto; display: block;')
  })
})

describe('result rendering contract', () => {
  it('uses CJK-safe UI and data fonts with a lightweight result overview', () => {
    expect(styles).toContain('--font-ui: "Noto Sans CJK SC"')
    expect(styles).toContain('--font-data: "Noto Sans Mono CJK SC"')
    expect(styles).toContain('font-family: var(--font-ui)')
    expect(styles).toContain('.result-overview')
    expect(styles).toContain('max-width: 1920px')
    expect(styles).toContain('minmax(360px, 40fr) minmax(600px, 60fr)')
  })
})

describe('Mosaic header contract', () => {
  it('uses CSS mosaic tiles, keeps stage labels unadorned, and does not clip status popovers', () => {
    expect(styles).toContain('.stage-eyebrow::before { display: none; }')
    expect(styles).toContain('.header-mosaic')
    expect(styles).toContain('background: rgba(5, 18, 31, .34)')
    expect(styles).toContain('position: relative;\n  z-index: 20;\n  isolation: isolate;\n  overflow: visible;')
  })
})
