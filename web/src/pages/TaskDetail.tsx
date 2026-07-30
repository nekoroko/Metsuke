// src/pages/TaskDetail.tsx — エージェントタスクの編集。
//
// 実行方式（graph_kind）と使用モデル（llm_profile_id）の「タスクの既定」を
// ここで保存する。どちらも未指定なら設定画面の既定に従う（backend の
// resolve_kind / resolve_profile が解決する）。

import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { Play, Trash2 } from 'lucide-react'

import { api } from '../api/client'
import {
  keys, useAgentTasks, useInvalidate, useLlmProfiles, useMeta, useRunAgent, useTools,
} from '../api/hooks'
import { Empty, Kicker, Tag } from '../components/ui'

export function TaskDetailPage({ isNew = false }: { isNew?: boolean } = {}) {
  const { taskId } = useParams<{ taskId: string }>()
  const navigate = useNavigate()
  const tasks = useAgentTasks()
  const tools = useTools()
  const profiles = useLlmProfiles()
  const meta = useMeta()
  const invalidate = useInvalidate()
  const runAgent = useRunAgent()

  const task = isNew ? undefined : (tasks.data ?? []).find((t) => t.id === taskId)

  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [prompt, setPrompt] = useState('')
  const [graphKind, setGraphKind] = useState<string>('default')
  const [profileId, setProfileId] = useState<string>('default')
  const [toolIds, setToolIds] = useState<string[]>([])
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    if (!task) return
    setName(task.name)
    setDescription(task.description ?? '')
    setPrompt(task.task_prompt)
    setGraphKind(task.graph_kind ?? 'default')
    setProfileId(task.llm_profile_id ?? 'default')
    setToolIds(task.allowed_tool_ids ?? [])
  }, [task?.id])                                      // eslint-disable-line react-hooks/exhaustive-deps

  if (!isNew && tasks.isLoading) return <div className="page-body">読み込み中…</div>
  if (!isNew && !task) return <Empty message="このタスクは見つかりませんでした。" />

  const fields = {
    name, description, task_prompt: prompt,
    allowed_tool_ids: toolIds,
    graph_kind: graphKind === 'default' ? null : graphKind,
    llm_profile_id: profileId === 'default' ? null : profileId,
  }
  const canSave = name.trim().length > 0 && prompt.trim().length > 0

  async function save() {
    if (isNew) {
      const created = await api.agentTasks.create(fields)
      invalidate(keys.agentTasks)
      // 作った直後は編集画面へ。続けて実行できるようにする
      navigate(`/tasks/${created.id}`)
      return
    }
    await api.agentTasks.update(task!.id, fields)
    invalidate(keys.agentTasks)
    setSaved(true)
    window.setTimeout(() => setSaved(false), 2000)
  }

  async function remove() {
    if (!task) return
    await api.agentTasks.remove(task.id)
    invalidate(keys.agentTasks)
    navigate('/tasks')
  }

  const verified = (tools.data ?? []).filter((t) => t.status === 'verified')
  const defaultProfileName = meta.data?.default_profile?.name ?? ''
  const defaultKindLabel =
    meta.data?.graph_kinds.find((k) => k.value === meta.data?.default_graph_kind)?.label ?? ''

  return (
    <>
      <header className="page-header">
        <div>
          <div className="kicker">Agent Task</div>
          <h2 className="page-title">{isNew ? '新しいタスク' : task!.name}</h2>
        </div>
        <div className="page-header-actions">
          {saved && <Tag kind="neutral">保存しました</Tag>}
          <button
            className={isNew ? 'btn btn-primary' : 'btn btn-secondary'}
            onClick={() => void save()}
            disabled={!canSave}
          >
            {isNew ? '＋ 作成' : '💾 保存'}
          </button>
          {!isNew && (
            <button
              className="btn btn-primary"
              onClick={async () => {
                const res = await runAgent.mutateAsync({ id: task!.id })
                navigate(`/runs/${res.exec_id}`)
              }}
            >
              <Play size={13} style={{ marginRight: 6 }} />実行
            </button>
          )}
        </div>
      </header>

      <div className="page-body">
        <div className="form-grid">
          <div className="field">
            <label htmlFor="t-name">タスク名</label>
            <input id="t-name" className="input" value={name} onChange={(e) => setName(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="t-desc">説明</label>
            <input id="t-desc" className="input" value={description} onChange={(e) => setDescription(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="t-prompt">タスクプロンプト</label>
            <textarea
              id="t-prompt" className="input" rows={8}
              value={prompt} onChange={(e) => setPrompt(e.target.value)}
              placeholder={isNew
                ? '例: SKハイニックスの直近の決算（実績値）と直近1週間の株価動向を調べて、'
                  + '数値には出典と時点を付けて日本語でまとめてください。'
                : undefined}
            />
            {isNew && (
              <div className="hint">
                エージェントに渡す指示です。数値を扱う調査では「出典と時点を付ける」
                まで書いておくと、検証が効きやすくなります。
              </div>
            )}
          </div>

          <div className="field">
            <label htmlFor="t-kind">実行方式</label>
            <select id="t-kind" className="input" value={graphKind} onChange={(e) => setGraphKind(e.target.value)}>
              <option value="default">設定の既定に従う（{defaultKindLabel}）</option>
              {(meta.data?.graph_kinds ?? []).map((k) => (
                <option key={k.value} value={k.value}>{k.label}</option>
              ))}
            </select>
            <div className="hint">
              {meta.data?.graph_kinds.find((k) => k.value === graphKind)?.description ?? ''}
            </div>
          </div>

          <div className="field">
            <label htmlFor="t-model">使用するモデル</label>
            <select id="t-model" className="input" value={profileId} onChange={(e) => setProfileId(e.target.value)}>
              <option value="default">設定の既定に従う（{defaultProfileName}）</option>
              {(profiles.data ?? []).map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}（{p.model}）
                </option>
              ))}
            </select>
            <div className="hint">
              未指定なら設定画面の既定に従います。実行時のランチャーで今回だけ差し替えることもできます。
            </div>
          </div>

          <div className="field">
            <label>使用可能なツール</label>
            {verified.length === 0 ? (
              <div className="hint">保存済みの検証済みツールはまだありません。</div>
            ) : (
              verified.map((t) => (
                <label key={t.id} className="row" style={{ gap: 6, padding: '3px 0', fontSize: 13 }}>
                  <input
                    type="checkbox"
                    checked={toolIds.includes(t.id)}
                    onChange={(e) =>
                      setToolIds((prev) =>
                        e.target.checked ? [...prev, t.id] : prev.filter((x) => x !== t.id),
                      )
                    }
                  />
                  {t.name}
                  <span className="cell-sub" style={{ margin: 0 }}>{t.description}</span>
                </label>
              ))
            )}
          </div>

          {!isNew && (
            <div>
              <Kicker>Danger</Kicker>
              <button className="btn btn-secondary btn-sm" style={{ marginTop: 6 }} onClick={() => void remove()}>
                <Trash2 size={12} style={{ marginRight: 5 }} />このタスクを削除
              </button>
            </div>
          )}
          {isNew && !canSave && (
            <div className="hint">タスク名とタスクプロンプトを入力すると作成できます。</div>
          )}
        </div>
      </div>
    </>
  )
}
