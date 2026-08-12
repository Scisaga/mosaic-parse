import styles from './index.css?raw'

describe('desktop control deck layout contract', () => {
  it('keeps both cards at the same compact desktop height and restores natural mobile flow', () => {
    expect(styles).toContain('--control-card-height: 140px')
    expect(styles).toContain('.source-picker, .options-panel { height: var(--control-card-height); }')
    expect(styles).toContain('.control-deck { --control-card-height: 140px; flex: none; align-items: stretch; }')
    expect(styles).toContain('.control-deck { grid-template-columns: 1fr; align-items: start; }')
  })
})
