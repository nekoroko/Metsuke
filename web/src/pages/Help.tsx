// src/pages/Help.tsx — ヘルプ。
//
// 設定画面と同じ2階層（左に項目、右に本文）。困ったときに画面から
// たどり着けることが目的なので、サイドバーの状態表示（Podman 未構築など）
// から直接この中の該当ページへ飛べるようにしてある。

import { NavLink, useParams } from 'react-router-dom'
import Markdown from 'react-markdown'

import { useMeta } from '../api/hooks'
import { HELP_TOPICS, findTopic } from '../help/topics'
import { Kicker, Tag } from '../components/ui'

/** sandbox.image_status() の state を、そのまま画面の言葉にする。 */
const SANDBOX_STATE_LABEL: Record<string, string> = {
  ready: 'イメージは最新です。対応は不要です。',
  stale: 'requirements-tools.txt が変わっています。次のプレビュー実行時に自動で作り直されます。',
  missing: 'イメージがまだありません。下の手順で作成してください。',
  user_managed: 'ユーザー管理のイメージが指定されています。自動ビルドの対象外です。',
  unavailable: 'podman が見つからないか、状態を取得できませんでした。',
}

export function HelpPage() {
  const { topic: slug } = useParams<{ topic: string }>()
  const topic = findTopic(slug) ?? HELP_TOPICS[0]
  const meta = useMeta()

  return (
    <div className="settings-layout">
      <nav className="settings-nav sidenav">
        <div style={{ padding: '14px 16px', borderBottom: '2px solid var(--color-divider)' }}>
          <Kicker>Help</Kicker>
        </div>
        {HELP_TOPICS.map((t) => (
          <NavLink
            key={t.slug}
            to={`/help/${t.slug}`}
            className={({ isActive }) =>
              `sidenav-item${isActive || t.slug === topic.slug ? ' active' : ''}`
            }
          >
            <span className="sidenav-label">{t.title}</span>
          </NavLink>
        ))}
      </nav>

      <div style={{ flex: 1, minWidth: 0, overflow: 'auto' }}>
        <header className="page-header">
          <div>
            <div className="kicker">{topic.en}</div>
            <h2 className="page-title">{topic.title}</h2>
          </div>
        </header>

        <div className="page-body">
          {/* いまの状態を本文の前に出す。手順だけ読んでも
              「自分がどれに当てはまるか」が分からないため */}
          {topic.slug === 'podman' && meta.data && (
            <div className="notice" style={{ marginBottom: 20 }}>
              <div className="row" style={{ gap: 8, marginBottom: 4 }}>
                <Kicker>いまの状態</Kicker>
                <Tag kind={meta.data.podman_ok ? 'neutral' : 'accent'}>
                  {meta.data.sandbox_state}
                </Tag>
              </div>
              {SANDBOX_STATE_LABEL[meta.data.sandbox_state] ?? ''}
            </div>
          )}
          {topic.slug === 'llm' && meta.data && (
            <div className="notice" style={{ marginBottom: 20 }}>
              <Kicker>いまの既定</Kicker>
              <div style={{ marginTop: 4 }}>
                {meta.data.default_profile?.name}（{meta.data.default_profile?.model}）
                {meta.data.default_profile?.base_url
                  ? ` — ${meta.data.default_profile.base_url}`
                  : ''}
              </div>
            </div>
          )}
          {topic.slug === 'search' && meta.data && (
            <div className="notice" style={{ marginBottom: 20 }}>
              <Kicker>いまの設定</Kicker>
              <div style={{ marginTop: 4 }}>{meta.data.search_provider}</div>
            </div>
          )}

          <div className="help-body">
            <Markdown>{topic.body}</Markdown>
          </div>
        </div>
      </div>
    </div>
  )
}
