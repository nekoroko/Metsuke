// src/components/Shell.tsx — 全画面共通のシェル（サイドバー＋ステータスバー）。
//
// 実行中のジョブは全画面から常に見えること（手順書 §5 の要件）。
// そのため running の監視はここに置き、ページ側には持たせない。

import { Link, NavLink, useNavigate } from 'react-router-dom'
import {
  CalendarClock, CircleHelp, Clock, List, Package, Settings as SettingsIcon,
} from 'lucide-react'
import type { ReactNode } from 'react'

import { useAgentTasks, useExecutions, useMeta, useTools } from '../api/hooks'
import { BRAND_NAME, BRAND_NAME_UPPER, BRAND_TAGLINE } from '../brand'
import { Kicker, Progress, Tag } from './ui'

const NAV = [
  { to: '/tasks', icon: List, ja: 'タスク', en: 'Tasks' },
  { to: '/runs', icon: Clock, ja: '実行履歴', en: 'Runs' },
  { to: '/library', icon: Package, ja: 'ライブラリ', en: 'Library' },
  { to: '/schedules', icon: CalendarClock, ja: 'スケジュール', en: 'Schedules' },
  { to: '/settings/models', icon: SettingsIcon, ja: '設定', en: 'Settings' },
  { to: '/help/about', icon: CircleHelp, ja: 'ヘルプ', en: 'Help' },
]

export function Shell({ children }: { children: ReactNode }) {
  const meta = useMeta()
  const tools = useTools()
  const tasks = useAgentTasks()
  const executions = useExecutions(30)
  const navigate = useNavigate()

  const running = (executions.data ?? []).filter((e) => e.status === 'running')
  const counts: Record<string, number> = {
    '/tasks': (tools.data?.length ?? 0) + (tasks.data?.length ?? 0),
    '/runs': running.length,
    '/library': tools.data?.length ?? 0,
  }

  return (
    <div className="shell">
      <aside className="sidebar">
        {/* サービス名は常に目に入る位置に出す。クリックで「Metsuke とは」へ */}
        <Link to="/help/about" className="brand" title={`${BRAND_NAME} とは`}>
          <div className="brand-mark" aria-hidden>⚡</div>
          <div>
            <div className="brand-name">{BRAND_NAME_UPPER}</div>
            <div className="brand-sub">{BRAND_TAGLINE}</div>
          </div>
        </Link>

        <nav className="sidenav">
          {NAV.map(({ to, icon: Icon, ja, en }) => (
            <NavLink
              key={to}
              to={to}
              className={({ isActive }) => `sidenav-item${isActive ? ' active' : ''}`}
            >
              <Icon size={15} strokeWidth={2} />
              <span className="sidenav-label">
                {ja} / {en}
              </span>
              {to === '/runs' && running.length > 0 ? (
                <span className="sidenav-badge">
                  <i className="sidenav-dot" />
                  {running.length}
                </span>
              ) : counts[to] ? (
                <span className="sidenav-badge">{counts[to]}</span>
              ) : null}
            </NavLink>
          ))}
        </nav>

        <div className="sidebar-foot">
          <Kicker>Model</Kicker>
          <div className="sidebar-model">
            {meta.data?.default_profile?.name ?? '—'}
          </div>
          <div className="sidebar-endpoint">
            {meta.data?.default_profile?.base_url || 'api.anthropic.com'}
          </div>
          {/* 状態タグは、それぞれの直し方を書いたヘルプへのリンクにする。
              「Podman 未構築」と出ている人が知りたいのは直し方なので、
              表示から1クリックで手順に着けるようにする */}
          <div className="sidebar-tags">
            <Link to="/help/search" className="tag-link" title="検索プロバイダの設定">
              <Tag kind="outline">{meta.data?.search_provider ?? '—'}</Tag>
            </Link>
            <Link to="/help/podman" className="tag-link" title="サンドボックスの構築手順">
              <Tag kind={meta.data?.podman_ok ? 'neutral' : 'accent'}>
                {meta.data?.podman_ok ? 'Podman OK' : 'Podman 未構築 →'}
              </Tag>
            </Link>
          </div>
        </div>
      </aside>

      <div className="main">
        <div className="main-scroll">{children}</div>

        {running.length > 0 && (
          <div className="statusbar">
            <Kicker>Running · {running.length}</Kicker>
            {running.slice(0, 2).map((e) => (
              <button
                key={e.id}
                className="statusbar-item"
                style={{ border: 0, background: 'none', cursor: 'pointer' }}
                onClick={() => navigate(`/runs/${e.id}`)}
              >
                <span className="statusbar-name">{e.target_name}</span>
                <span className="statusbar-meta">
                  {e.graph_label || e.exec_type}
                  {e.llm_info?.model ? ` · ${e.llm_info.model}` : ''}
                </span>
                <span className="progress">
                  <span style={{ width: '40%' }} />
                </span>
              </button>
            ))}
            {running.length > 2 && (
              <span className="statusbar-meta">ほか {running.length - 2} 件</span>
            )}
            <div style={{ flex: 1 }} />
            <span style={{ width: 120 }}>
              <Progress value={running.length} max={Math.max(running.length, 3)} />
            </span>
          </div>
        )}
      </div>
    </div>
  )
}
