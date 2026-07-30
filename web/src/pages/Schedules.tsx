// src/pages/Schedules.tsx — cron 登録・停止/再開。

import { useState } from 'react'

import { api } from '../api/client'
import { keys, unify, useAgentTasks, useInvalidate, useSchedules, useTools } from '../api/hooks'
import { Empty, Kicker, Tag } from '../components/ui'

export function SchedulesPage() {
  const schedules = useSchedules()
  const tools = useTools()
  const tasks = useAgentTasks()
  const invalidate = useInvalidate()

  const targets = unify(tools.data, tasks.data)
  const [targetKey, setTargetKey] = useState('')
  const [cron, setCron] = useState('0 9 * * *')
  const [error, setError] = useState('')

  async function add() {
    setError('')
    const target = targets.find((t) => `${t.kind}:${t.id}` === targetKey)
    if (!target) return
    try {
      await api.schedules.create({
        exec_type: target.kind, target_id: target.id, cron_expr: cron,
      })
      invalidate(keys.schedules)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <>
      <header className="page-header">
        <div>
          <div className="kicker">Schedules</div>
          <h2 className="page-title">スケジュール</h2>
        </div>
      </header>

      <div className="page-body">
        <Kicker>新しい定期実行</Kicker>
        <div className="row" style={{ marginTop: 8, alignItems: 'flex-end' }}>
          <div className="field" style={{ minWidth: 280 }}>
            <label htmlFor="s-target">対象</label>
            <select id="s-target" className="input" value={targetKey} onChange={(e) => setTargetKey(e.target.value)}>
              <option value="">選択してください</option>
              {targets.map((t) => (
                <option key={`${t.kind}:${t.id}`} value={`${t.kind}:${t.id}`}>
                  {t.kind === 'agent' ? '🧠' : '🔧'} {t.name}
                </option>
              ))}
            </select>
          </div>
          <div className="field" style={{ width: 180 }}>
            <label htmlFor="s-cron">cron 式</label>
            <input id="s-cron" className="input mono" value={cron} onChange={(e) => setCron(e.target.value)} />
          </div>
          <button className="btn btn-primary" onClick={() => void add()} disabled={!targetKey}>
            ＋ 登録
          </button>
        </div>
        {error && <div className="notice" style={{ marginTop: 10 }}>{error}</div>}

        <div style={{ marginTop: 28 }}>
          <Kicker>登録済み</Kicker>
          {(schedules.data ?? []).length === 0 ? (
            <Empty message="まだスケジュールがありません。" />
          ) : (
            <table className="grid" style={{ marginTop: 8 }}>
              <thead>
                <tr><th style={{ width: 26 }} /><th>対象</th><th>cron</th><th>状態</th><th /></tr>
              </thead>
              <tbody>
                {(schedules.data ?? []).map((s) => (
                  <tr key={s.id}>
                    <td>{s.exec_type === 'agent' ? '🧠' : '🔧'}</td>
                    <td className="cell-name">{s.target_name}</td>
                    <td className="mono cell-nowrap">{s.cron_expr}</td>
                    <td className="cell-nowrap">
                      <Tag kind={s.enabled ? 'neutral' : 'outline'}>{s.enabled ? '有効' : '停止中'}</Tag>
                    </td>
                    <td>
                      <div className="row-actions">
                        <button
                          className="btn btn-secondary btn-sm"
                          onClick={async () => {
                            await api.schedules.toggle(s.id, !s.enabled)
                            invalidate(keys.schedules)
                          }}
                        >
                          {s.enabled ? '停止' : '再開'}
                        </button>
                        <button
                          className="btn btn-ghost btn-sm"
                          onClick={async () => {
                            await api.schedules.remove(s.id)
                            invalidate(keys.schedules)
                          }}
                        >
                          削除
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </>
  )
}
