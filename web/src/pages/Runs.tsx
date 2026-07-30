// src/pages/Runs.tsx — 実行履歴（一覧）。
//
// 現状の Streamlit 版では expander の入れ子に埋もれていた。ここでは
// 一覧は行だけにして、中身は実行詳細（/runs/:id）に任せる。

import { useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { useExecutions } from '../api/hooks'
import { Empty, StatusTag, Tag } from '../components/ui'

type Filter = 'all' | 'running' | 'done' | 'error'

export function RunsPage() {
  const navigate = useNavigate()
  const executions = useExecutions(100)
  const [filter, setFilter] = useState<Filter>('all')

  const rows = (executions.data ?? []).filter((e) => filter === 'all' || e.status === filter)

  return (
    // 見出しとフィルタは固定。スクロールするのは表だけ
    <div className="page">
      <header className="page-header">
        <div>
          <div className="kicker">Runs</div>
          <h2 className="page-title">実行履歴</h2>
        </div>
      </header>

      <div className="filters">
        {([
          ['all', 'すべて / All'],
          ['running', '⏳ 実行中'],
          ['done', '✅ 完了'],
          ['error', '❌ エラー'],
        ] as [Filter, string][]).map(([key, label]) => (
          <button key={key} className={`filter${filter === key ? ' on' : ''}`} onClick={() => setFilter(key)}>
            {label}
          </button>
        ))}
      </div>

      {rows.length === 0 ? (
        <Empty message="まだ実行履歴がありません。" />
      ) : (
        <div className="page-scroll">
          <table className="grid">
            <thead>
              <tr>
                <th style={{ width: 26 }} />
                <th>対象</th>
                <th>実行方式</th>
                <th>モデル</th>
                <th>状態</th>
                <th>開始</th>
                <th>完了</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((e) => (
                <tr key={e.id} style={{ cursor: 'pointer' }} onClick={() => navigate(`/runs/${e.id}`)}>
                  <td>{e.exec_type === 'agent' ? '🧠' : '🔧'}</td>
                  <td>
                    <div className="cell-name">{e.target_name}</div>
                    <div className="cell-sub mono">EXEC {e.id}</div>
                  </td>
                  <td className="cell-nowrap">
                    {e.graph_label ? <Tag kind="outline">{e.graph_label}</Tag> : '—'}
                  </td>
                  <td className="cell-nowrap mono" style={{ fontSize: 11.5 }}>
                    {e.llm_info?.model ?? '—'}
                  </td>
                  <td className="cell-nowrap"><StatusTag status={e.status} /></td>
                  <td className="cell-nowrap mono" style={{ fontSize: 11 }}>
                    {e.started_at.slice(0, 19).replace('T', ' ')}
                  </td>
                  <td className="cell-nowrap mono" style={{ fontSize: 11 }}>
                    {e.finished_at ? e.finished_at.slice(11, 19) : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
