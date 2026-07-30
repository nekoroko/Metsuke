// src/components/ui.tsx — 小さな共通部品。
//
// デザインシステムのクラス（.btn / .tag 等）をそのまま使い、
// ここで独自のスタイルを足さない。トークンが二重になるため。

import type { ReactNode } from 'react'

export function Kicker({ children, lg }: { children: ReactNode; lg?: boolean }) {
  return <div className={lg ? 'kicker kicker-lg' : 'kicker'}>{children}</div>
}

type TagKind = 'accent' | 'neutral' | 'outline'

export function Tag({ kind = 'neutral', children }: { kind?: TagKind; children: ReactNode }) {
  return <span className={`tag tag-${kind}`}>{children}</span>
}

/** 実行状態のタグ。語彙は現行UIに合わせる（絵文字は識別子として機能している）。 */
export function StatusTag({ status }: { status: string }) {
  if (status === 'running') return <Tag kind="accent">⏳ 実行中</Tag>
  if (status === 'error') return <Tag kind="accent">❌ エラー</Tag>
  if (status === 'done') return <Tag kind="neutral">✅ 完了</Tag>
  if (status === 'verified') return <Tag kind="neutral">検証済み</Tag>
  if (status === 'draft') return <Tag kind="outline">下書き</Tag>
  return <Tag kind="outline">{status}</Tag>
}

export function Progress({ value, max, ink }: { value: number; max: number; ink?: boolean }) {
  const pct = max > 0 ? Math.min(100, Math.round((value / max) * 100)) : 0
  return (
    <div className={`progress progress-wide${ink ? ' progress-ink' : ''}`}>
      <span style={{ width: `${pct}%` }} />
    </div>
  )
}

export function Empty({ message, action }: { message: string; action?: ReactNode }) {
  return (
    <div className="empty">
      <p>{message}</p>
      {action}
    </div>
  )
}

export function Keycap({ children }: { children: ReactNode }) {
  return <span className="keycap">{children}</span>
}

/** 秒数を「1.9s」の形にする。時刻表示と揃えて等幅で出す。 */
export function secs(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return '—'
  return ms >= 10_000 ? `${Math.round(ms / 1000)}s` : `${(ms / 1000).toFixed(1)}s`
}

export function hhmmss(iso?: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toTimeString().slice(0, 8)
}

export function elapsed(from?: string | null, to?: string | null): string {
  if (!from) return '—'
  const start = new Date(from).getTime()
  const end = to ? new Date(to).getTime() : Date.now()
  if (Number.isNaN(start) || Number.isNaN(end)) return '—'
  const total = Math.max(0, Math.floor((end - start) / 1000))
  const mm = String(Math.floor(total / 60)).padStart(2, '0')
  const ss = String(total % 60).padStart(2, '0')
  return `${mm}:${ss}`
}
