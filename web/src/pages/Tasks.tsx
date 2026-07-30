// src/pages/Tasks.tsx — タスク一覧（手順書 5.1 / 案 1b・1c）
//
// ツール（Type1）とエージェント（Type2）を1つのリストに統合する。
// 現状は別タブに分かれていて、実行するまでに何度も選択が要る。
// **一覧から直接1クリックで実行できること**が要件。

import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { LayoutGrid, List as ListIcon, Play, Search } from 'lucide-react'

import {
  unify, useAgentTasks, useExecutions, useLlmProfiles, useMeta, useRunAgent,
  useRunTool, useTools,
} from '../api/hooks'
import { Empty, Keycap, StatusTag, Tag } from '../components/ui'

type Filter = 'all' | 'tool' | 'agent' | 'draft'

export function TasksPage({ onOpenLauncher }: { onOpenLauncher: () => void }) {
  const navigate = useNavigate()
  const tools = useTools()
  const tasks = useAgentTasks()
  const executions = useExecutions(30)
  const profiles = useLlmProfiles()
  const meta = useMeta()
  const runAgent = useRunAgent()
  const runTool = useRunTool()

  const [filter, setFilter] = useState<Filter>('all')
  const [query, setQuery] = useState('')
  const [dense, setDense] = useState(true)

  const all = useMemo(() => unify(tools.data, tasks.data), [tools.data, tasks.data])
  const counts = {
    all: all.length,
    tool: all.filter((t) => t.kind === 'tool').length,
    agent: all.filter((t) => t.kind === 'agent').length,
    draft: all.filter((t) => t.status === 'draft').length,
  }

  const rows = all
    .filter((t) => {
      if (filter === 'tool') return t.kind === 'tool'
      if (filter === 'agent') return t.kind === 'agent'
      if (filter === 'draft') return t.status === 'draft'
      return true
    })
    .filter((t) => {
      const q = query.trim().toLowerCase()
      if (!q) return true
      return t.name.toLowerCase().includes(q) || t.description.toLowerCase().includes(q)
    })

  /** その対象の直近の実行。状態列と「最終実行」に使う。 */
  function latestRun(id: string) {
    return (executions.data ?? []).find((e) => e.target_id === id)
  }

  async function run(id: string, kind: 'tool' | 'agent') {
    // 確認ダイアログは出さない。即実行してステータスバーに出す（手順書 §6）
    if (kind === 'tool') {
      const res = await runTool.mutateAsync(id)
      navigate(`/runs/${res.exec_id}`)
    } else {
      const res = await runAgent.mutateAsync({ id })
      navigate(`/runs/${res.exec_id}`)
    }
  }

  function modelOf(profileId: string | null) {
    const p = (profiles.data ?? []).find(
      (x) => x.id === (profileId ?? meta.data?.default_profile?.id),
    )
    return p ? `${p.provider === 'api' ? '☁️' : '🖥️'} ${p.model}` : '—'
  }

  return (
    <>
      <header className="page-header">
        <div>
          <div className="kicker">Tasks</div>
          <h2 className="page-title">タスク</h2>
        </div>
        <div className="page-header-actions">
          <div className="seg">
            <button className={`seg-opt${dense ? ' on' : ''}`} onClick={() => setDense(true)} aria-label="テーブル表示">
              <ListIcon size={13} />
            </button>
            <button className={`seg-opt${!dense ? ' on' : ''}`} onClick={() => setDense(false)} aria-label="カード表示">
              <LayoutGrid size={13} />
            </button>
          </div>
          <button className="btn btn-primary" onClick={onOpenLauncher}>
            <Play size={13} style={{ marginRight: 6 }} />
            実行 / Run
          </button>
        </div>
      </header>

      <div className="filters">
        {([
          ['all', `すべて / All ${counts.all}`],
          ['tool', `🔧 ツール / Tools ${counts.tool}`],
          ['agent', `🧠 エージェント / Agents ${counts.agent}`],
          ['draft', `📝 下書き / Drafts ${counts.draft}`],
        ] as [Filter, string][]).map(([key, label]) => (
          <button key={key} className={`filter${filter === key ? ' on' : ''}`} onClick={() => setFilter(key)}>
            {label}
          </button>
        ))}
      </div>

      <div className="searchbar">
        <Search size={14} />
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="名前・タグで絞り込む"
        />
        <button className="btn btn-ghost btn-sm" onClick={onOpenLauncher}>
          <Keycap>⌘K</Keycap>
        </button>
      </div>

      {rows.length === 0 ? (
        <Empty
          message="まだツールがありません。"
          action={
            <button className="btn btn-primary" onClick={() => navigate('/library')}>
              ＋ 新規タスク
            </button>
          }
        />
      ) : dense ? (
        <div className="table-wrap">
          <table className="grid">
            <thead>
              <tr>
                <th style={{ width: 26 }} />
                <th>名前</th>
                <th>モデル</th>
                <th>種別</th>
                <th>状態</th>
                <th>最終実行</th>
                <th style={{ textAlign: 'right' }}>操作</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((t) => {
                const run0 = latestRun(t.id)
                return (
                  <tr key={`${t.kind}-${t.id}`}>
                    <td>{t.kind === 'agent' ? '🧠' : '🔧'}</td>
                    <td>
                      <div className="cell-name">{t.name}</div>
                      {t.description && <div className="cell-sub">{t.description}</div>}
                    </td>
                    <td className="cell-nowrap mono" style={{ fontSize: 11.5 }}>
                      {t.kind === 'agent' ? modelOf(t.llm_profile_id) : '—'}
                    </td>
                    <td className="cell-nowrap">
                      <Tag kind="outline">{t.kind === 'agent' ? 'エージェント' : 'ツール'}</Tag>
                    </td>
                    <td className="cell-nowrap">
                      {run0?.status === 'running' ? <StatusTag status="running" /> : <StatusTag status={t.status} />}
                    </td>
                    <td className="cell-nowrap mono" style={{ fontSize: 11 }}>
                      {run0 ? run0.started_at.slice(0, 19).replace('T', ' ') : '—'}
                    </td>
                    <td>
                      <div className="row-actions">
                        <button className="btn btn-primary btn-sm" onClick={() => void run(t.id, t.kind)}>
                          ▶ 実行
                        </button>
                        <button
                          className="btn btn-secondary btn-sm"
                          onClick={() => navigate(t.kind === 'agent' ? `/tasks/${t.id}` : `/library?tool=${t.id}`)}
                        >
                          編集
                        </button>
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="page-body roomy">
          <div className="cards">
            {rows.map((t) => {
              const run0 = latestRun(t.id)
              const recent = (executions.data ?? []).filter((e) => e.target_id === t.id).slice(0, 8).reverse()
              return (
                <div key={`${t.kind}-${t.id}`} className={`card-flat${run0?.status === 'running' ? ' running' : ''}`}>
                  <div className="kicker">{t.kind === 'agent' ? 'Agent' : 'Tool'}</div>
                  <div style={{ font: '800 18px var(--font-heading)', marginTop: 4 }}>{t.name}</div>
                  {t.description && <div className="cell-sub" style={{ marginTop: 6 }}>{t.description}</div>}
                  <div className="row" style={{ marginTop: 12 }}>
                    {run0?.status === 'running' ? <StatusTag status="running" /> : <StatusTag status={t.status} />}
                    {t.kind === 'agent' && <Tag kind="outline">{modelOf(t.llm_profile_id)}</Tag>}
                  </div>
                  {recent.length > 1 && (
                    <div className="spark" aria-hidden>
                      {recent.map((e, i) => (
                        <i
                          key={e.id}
                          className={i === recent.length - 1 ? 'latest' : ''}
                          style={{ height: `${8 + ((i * 7) % 16)}px` }}
                        />
                      ))}
                    </div>
                  )}
                  <div className="row" style={{ marginTop: 14 }}>
                    <button className="btn btn-primary btn-sm" onClick={() => void run(t.id, t.kind)}>
                      ▶ 実行
                    </button>
                    <button
                      className="btn btn-secondary btn-sm"
                      onClick={() => navigate(t.kind === 'agent' ? `/tasks/${t.id}` : `/library?tool=${t.id}`)}
                    >
                      編集
                    </button>
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      )}
    </>
  )
}
