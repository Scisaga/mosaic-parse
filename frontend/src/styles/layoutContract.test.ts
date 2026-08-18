import styles from './index.css?raw'

describe('desktop control deck layout contract', () => {
  it('keeps cards aligned without clipping content and restores natural mobile flow', () => {
    expect(styles).toContain('--control-card-min-height: 140px')
    expect(styles).toContain('.source-picker, .options-panel { min-height: var(--control-card-min-height); }')
    expect(styles).not.toContain('.source-picker, .options-panel { height: var(--control-card-height); }')
    expect(styles).toContain('.control-deck { --control-card-min-height: 140px; flex: none; align-items: stretch; }')
    expect(styles).toContain('.control-deck { grid-template-columns: 1fr; align-items: start; }')
  })
})

describe('result rendering contract', () => {
  it('uses CJK-safe UI and data fonts with a lightweight result overview', () => {
    expect(styles).toContain('--font-ui: "Noto Sans CJK SC"')
    expect(styles).toContain('--font-data: "Noto Sans Mono CJK SC"')
    expect(styles).toContain('font-family: var(--font-ui)')
    expect(styles).toContain('.result-overview')
    expect(styles).toContain('max-width: 1920px')
    expect(styles).toContain('minmax(330px, 36fr) minmax(620px, 64fr)')
  })
})

describe('Mosaic header contract', () => {
  it('uses CSS mosaic tiles and keeps stage labels unadorned', () => {
    expect(styles).toContain('.stage-eyebrow::before { display: none; }')
    expect(styles).toContain('rgba(3, 20, 34, .42) 1px, transparent 1px')
    expect(styles).toContain('radial-gradient(ellipse 72% 190%')
    expect(styles).toContain('background-size: 13px 13px, 13px 13px, 100% 260px')
  })
})
