import DOMPurify from 'dompurify'

export const MAX_MERMAID_SOURCE_LENGTH = 20_000
const MAX_SOURCE_LINES = 750
const MAX_SVG_LENGTH = 1_500_000
const MAX_SVG_ELEMENTS = 5_000

const FORBIDDEN_SVG_TAGS = [
  'a',
  'audio',
  'embed',
  'foreignobject',
  'iframe',
  'image',
  'object',
  'script',
  'template',
  'use',
  'video',
]

const FORBIDDEN_SVG_ATTRIBUTES = [
  'action',
  'formaction',
  'href',
  'src',
  'target',
  'xlink:href',
]

export function validateMermaidSource(source: string): string | null {
  if (!source.trim()) return '图表源码为空。'
  if (source.length > MAX_MERMAID_SOURCE_LENGTH || source.split(/\r?\n/).length > MAX_SOURCE_LINES) {
    return '图表源码超过安全渲染限制。'
  }
  if (/^\s*---\s*(?:\r?\n|$)/.test(source)) return '不允许 Mermaid frontmatter。'
  if (/%%\s*\{/.test(source)) return '不允许 Mermaid 初始化指令。'
  if (/^\s*click\b/im.test(source) || /\b(?:callback|href)\b/i.test(source)) {
    return '不允许 Mermaid 交互链接或回调。'
  }
  if (/^\s*(?:classDef|linkStyle|style)\b/im.test(source)) return '不允许 Mermaid 自定义样式指令。'
  if (/\b(?:https?|ftp|file|data|blob|javascript|vbscript):/i.test(source) || /(?:^|[\s"'(])\/\/[^/\s]/m.test(source)) {
    return '不允许 Mermaid 加载外部 URL。'
  }
  if (/!?\[[^\]]*\]\s*\(/.test(source) || /\b(?:img|image)\s*:/i.test(source) || /<\s*(?:a|img|image|iframe|foreignObject|script|style)\b/i.test(source)) {
    return '不允许 Mermaid 嵌入外部内容。'
  }
  return null
}

function containsUnsafeCss(value: string): boolean {
  if (/@(?:import|font-face|namespace)|expression\s*\(|(?:https?|ftp|file|data|javascript|vbscript):|<\/?style|<!--/i.test(value)) return true
  const urls = value.matchAll(/url\(\s*(['"]?)(.*?)\1\s*\)/gi)
  for (const match of urls) {
    if (!match[2].trim().startsWith('#')) return true
  }
  return false
}

/** Defense-in-depth for Mermaid output. The returned SVG has no active or external resources. */
export function sanitizeMermaidSvg(svg: string): string {
  if (!svg || svg.length > MAX_SVG_LENGTH) throw new Error('unsafe_svg_size')
  const clean = DOMPurify.sanitize(svg, {
    USE_PROFILES: { svg: true, svgFilters: true },
    ALLOW_ARIA_ATTR: true,
    ALLOW_DATA_ATTR: false,
    ALLOW_UNKNOWN_PROTOCOLS: false,
    FORBID_TAGS: FORBIDDEN_SVG_TAGS,
    FORBID_ATTR: FORBIDDEN_SVG_ATTRIBUTES,
  })
  if (typeof clean !== 'string' || !clean) throw new Error('unsafe_svg_empty')

  const document = new DOMParser().parseFromString(clean, 'image/svg+xml')
  if (document.querySelector('parsererror') || document.documentElement.localName !== 'svg') throw new Error('unsafe_svg_parse')
  const root = document.documentElement
  const elements = [root, ...Array.from(root.querySelectorAll('*'))]
  if (elements.length > MAX_SVG_ELEMENTS) throw new Error('unsafe_svg_complexity')

  const forbiddenTags = new Set(FORBIDDEN_SVG_TAGS.map((tag) => tag.toLowerCase()))
  const forbiddenAttributes = new Set(FORBIDDEN_SVG_ATTRIBUTES.map((attribute) => attribute.toLowerCase()))
  for (const element of elements) {
    if (forbiddenTags.has(element.localName.toLowerCase())) throw new Error('unsafe_svg_element')
    for (const attribute of Array.from(element.attributes)) {
      const name = attribute.name.toLowerCase()
      const value = attribute.value
      if ((name === 'xmlns' && value === 'http://www.w3.org/2000/svg') || (name === 'xmlns:xlink' && value === 'http://www.w3.org/1999/xlink')) continue
      if (name.startsWith('on') || forbiddenAttributes.has(name)) throw new Error('unsafe_svg_attribute')
      if (containsUnsafeCss(value)) throw new Error('unsafe_svg_reference')
    }
    if (element.localName.toLowerCase() === 'style' && containsUnsafeCss(element.textContent ?? '')) {
      throw new Error('unsafe_svg_style')
    }
  }

  root.setAttribute('aria-hidden', 'true')
  root.setAttribute('focusable', 'false')
  root.removeAttribute('role')
  root.removeAttribute('tabindex')
  root.removeAttribute('height')
  root.removeAttribute('width')
  return new XMLSerializer().serializeToString(root)
}
