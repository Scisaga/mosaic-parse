import type { ReactNode } from 'react'

export type WorkspaceStateVariant = 'input-empty' | 'output-empty' | 'pages-empty' | 'loading' | 'info' | 'error'
type WorkspaceScene = 'input' | 'markdown' | 'text' | 'pages' | 'loading' | 'info' | 'error'

interface WorkspaceStateProps {
  variant: WorkspaceStateVariant
  contentKind?: 'markdown' | 'text'
  title?: ReactNode
  description: ReactNode
  role?: 'status' | 'alert'
  live?: 'polite' | 'assertive'
  busy?: boolean
  className?: string
}

function sceneFor(variant: WorkspaceStateVariant, contentKind?: 'markdown' | 'text'): WorkspaceScene {
  if (variant === 'output-empty') return contentKind === 'markdown' ? 'markdown' : 'text'
  if (variant === 'input-empty') return 'input'
  if (variant === 'pages-empty') return 'pages'
  return variant
}

export function WorkspaceState({ variant, contentKind, title, description, role, live, busy = false, className = '' }: WorkspaceStateProps) {
  const scene = sceneFor(variant, contentKind)

  return (
    <div
      className={`workspace-state workspace-state-${variant} ${className}`.trim()}
      role={role}
      aria-live={live}
      aria-busy={busy || undefined}
    >
      <span className="workspace-state-art-frame" aria-hidden="true">
        <img
          className={`workspace-state-art workspace-state-art-${scene}`}
          src={`/illustrations/workspace-${scene}.png`}
          width="640"
          height="512"
          alt=""
          aria-hidden="true"
          draggable={false}
          decoding="async"
          data-scene={scene}
        />
      </span>
      {title && <strong>{title}</strong>}
      <p>{description}</p>
    </div>
  )
}
