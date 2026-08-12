import { Children, isValidElement, type ComponentProps, type ReactNode } from 'react'
import ReactMarkdown from 'react-markdown'
import rehypeSanitize from 'rehype-sanitize'
import remarkGfm from 'remark-gfm'
import { MermaidDiagram } from './MermaidDiagram'

function mermaidSource(children: ReactNode): string {
  return String(Children.toArray(children).join('')).replace(/\n$/, '')
}

function MarkdownPre({ children, ...props }: ComponentProps<'pre'>) {
  const items = Children.toArray(children)
  const child = items.length === 1 && isValidElement<ComponentProps<'code'>>(items[0]) ? items[0] : null
  if (child && /(?:^|\s)language-mermaid(?:\s|$)/i.test(child.props.className ?? '')) {
    return <MermaidDiagram source={mermaidSource(child.props.children)} />
  }
  return <pre {...props}>{children}</pre>
}

export function MarkdownRenderer({ children }: { children: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      rehypePlugins={[rehypeSanitize]}
      components={{ pre: MarkdownPre }}
    >
      {children}
    </ReactMarkdown>
  )
}
