import type { SVGProps } from 'react'

type IconProps = SVGProps<SVGSVGElement>

function BaseIcon({ children, ...props }: IconProps) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" {...props}>
      {children}
    </svg>
  )
}

export function UploadIcon(props: IconProps) {
  return <BaseIcon {...props}><path d="M12 16V4m0 0L7.5 8.5M12 4l4.5 4.5"/><path d="M5 14v4.5A1.5 1.5 0 006.5 20h11a1.5 1.5 0 001.5-1.5V14"/></BaseIcon>
}

export function LinkIcon(props: IconProps) {
  return <BaseIcon {...props}><path d="M10 13a5 5 0 007.1.1l2-2a5 5 0 00-7.1-7.1l-1.1 1.1"/><path d="M14 11a5 5 0 00-7.1-.1l-2 2A5 5 0 0012 20l1.1-1.1"/></BaseIcon>
}

export function FileIcon(props: IconProps) {
  return <BaseIcon {...props}><path d="M6 3h8l4 4v14H6z"/><path d="M14 3v5h4M9 13h6M9 17h6"/></BaseIcon>
}

export function CopyIcon(props: IconProps) {
  return <BaseIcon {...props}><rect x="8" y="8" width="11" height="12" rx="2"/><path d="M16 8V6a2 2 0 00-2-2H6a2 2 0 00-2 2v9a2 2 0 002 2h2"/></BaseIcon>
}

export function DownloadIcon(props: IconProps) {
  return <BaseIcon {...props}><path d="M12 4v11m0 0l4-4m-4 4l-4-4"/><path d="M5 20h14"/></BaseIcon>
}

export function RotateIcon(props: IconProps) {
  return <BaseIcon {...props}><path d="M20 11a8 8 0 10-2.3 5.7"/><path d="M20 4v7h-7"/></BaseIcon>
}

export function ChevronLeftIcon(props: IconProps) {
  return <BaseIcon {...props}><path d="M15 18l-6-6 6-6"/></BaseIcon>
}

export function ChevronRightIcon(props: IconProps) {
  return <BaseIcon {...props}><path d="M9 18l6-6-6-6"/></BaseIcon>
}

export function CloseIcon(props: IconProps) {
  return <BaseIcon {...props}><path d="M6 6l12 12M18 6L6 18"/></BaseIcon>
}

export function WarningIcon(props: IconProps) {
  return <BaseIcon {...props}><path d="M10.3 4.5L2.7 18a2 2 0 001.7 3h15.2a2 2 0 001.7-3L13.7 4.5a2 2 0 00-3.4 0z"/><path d="M12 9v4m0 4h.01"/></BaseIcon>
}

export function RefreshIcon(props: IconProps) {
  return <BaseIcon {...props}><path d="M20 6v5h-5"/><path d="M18.5 15a7 7 0 11-.7-7.8L20 11"/></BaseIcon>
}

export function PlayIcon(props: IconProps) {
  return <BaseIcon {...props}><path d="M8 5v14l11-7z"/></BaseIcon>
}

export function TrashIcon(props: IconProps) {
  return <BaseIcon {...props}><path d="M4 7h16M9 7V4h6v3m3 0l-1 14H7L6 7"/><path d="M10 11v6m4-6v6"/></BaseIcon>
}

export function StopIcon(props: IconProps) {
  return <BaseIcon {...props}><rect x="6" y="6" width="12" height="12" rx="1"/></BaseIcon>
}

export function SettingsIcon(props: IconProps) {
  return <BaseIcon {...props}><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 00.3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 00-1.9-.3 1.7 1.7 0 00-1 1.6v.2h-4V21a1.7 1.7 0 00-1-1.6 1.7 1.7 0 00-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 00.3-1.9A1.7 1.7 0 003 14H2.8v-4H3a1.7 1.7 0 001.6-1 1.7 1.7 0 00-.3-1.9L4.2 7 7 4.2l.1.1A1.7 1.7 0 009 4.6 1.7 1.7 0 0010 3v-.2h4V3a1.7 1.7 0 001 1.6 1.7 1.7 0 001.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 00-.3 1.9 1.7 1.7 0 001.6 1h.2v4H21a1.7 1.7 0 00-1.6 1z"/></BaseIcon>
}

export function ExternalLinkIcon(props: IconProps) {
  return <BaseIcon {...props}><path d="M14 5h5v5M19 5l-8 8"/><path d="M18 13v5a1 1 0 01-1 1H6a1 1 0 01-1-1V7a1 1 0 011-1h5"/></BaseIcon>
}
