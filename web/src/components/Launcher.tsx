// src/components/Launcher.tsx — ⌘K 実行ランチャー（手順書 5.2 / 案 3a）
//
// 「対象・モデル・実行方式・今すぐ/定期」を1枚で決めて即実行する。
// ↵ だけで既定のまま実行できることが要件なので、オプションを開かなくても
// 走る状態を常に保つ。

import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Cloud, MonitorSmartphone, Play, Search } from 'lucide-react'

import { api } from '../api/client'
import {
  keys, unify, useAgentTasks, useInvalidate, useLlmProfiles, useMeta,
  useRunAgent, useRunTool, useTools,
} from '../api/hooks'
import { Kicker, Keycap, Tag } from './ui'

interface Props {
  open: boolean
  onClose: () => void
}

export function Launcher({ open, onClose }: Props) {
  const navigate = useNavigate()
  const meta = useMeta()
  const tools = useTools()
  const tasks = useAgentTasks()
  const profiles = useLlmProfiles()
  const runAgent = useRunAgent()
  const runTool = useRunTool()
  const invalidate = useInvalidate()

  const [query, setQuery] = useState('')
  const [index, setIndex] = useState(0)
  const [graphKind, setGraphKind] = useState<string | null>(null)
  const [profileId, setProfileId] = useState<string | null>(null)
  const [when, setWhen] = useState<'now' | 'cron'>('now')
  const [cron, setCron] = useState('0 9 * * *')
  const [saveDefault, setSaveDefault] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const inputRef = useRef<HTMLInputElement>(null)
  const dialogRef = useRef<HTMLDivElement>(null)

  const items = useMemo(() => {
    const all = unify(tools.data, tasks.data)
    const q = query.trim().toLowerCase()
    if (!q) return all.slice(0, 8)
    return all
      .filter((t) => t.name.toLowerCase().includes(q) || t.description.toLowerCase().includes(q))
      .slice(0, 8)
  }, [tools.data, tasks.data, query])

  const selected = items[index]

  // 選択が変わったら、そのタスクの既定（保存値）に合わせる。
  // 「タスクの既定」バッジで、どれが保存値かを見せる
  useEffect(() => {
    if (!selected) return
    setGraphKind(selected.graph_kind)
    setProfileId(selected.llm_profile_id)
  }, [selected?.id])                                  // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (open) {
      setQuery('')
      setIndex(0)
      setError('')
      setBusy(false)
      // 開いた瞬間に検索入力へフォーカス
      window.setTimeout(() => inputRef.current?.focus(), 0)
    }
  }, [open])

  const effectiveGraph = graphKind ?? meta.data?.default_graph_kind ?? 'react'
  const effectiveProfile = profileId ?? meta.data?.default_profile?.id ?? null

  async function execute() {
    if (!selected || busy) return
    setBusy(true)
    setError('')
    try {
      if (when === 'cron') {
        await api.schedules.create({
          exec_type: selected.kind, target_id: selected.id, cron_expr: cron,
        })
        invalidate(keys.schedules)
        onClose()
        navigate('/schedules')
        return
      }

      if (selected.kind === 'tool') {
        const res = await runTool.mutateAsync(selected.id)
        onClose()
        navigate(`/runs/${res.exec_id}`)
        return
      }

      if (saveDefault) {
        await api.agentTasks.update(selected.id, {
          graph_kind: graphKind, llm_profile_id: profileId,
        })
        invalidate(keys.agentTasks)
      }
      const res = await runAgent.mutateAsync({
        id: selected.id, graphKind, profileId,
      })
      onClose()
      navigate(`/runs/${res.exec_id}`)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Escape') {
      e.preventDefault()
      onClose()
      return
    }
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setIndex((i) => Math.min(items.length - 1, i + 1))
      return
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault()
      setIndex((i) => Math.max(0, i - 1))
      return
    }
    if (e.key === 'Enter') {
      e.preventDefault()
      void execute()
      return
    }
    // ⌘E / ⌘M で実行方式・モデルを循環切替
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'e') {
      e.preventDefault()
      const kinds = meta.data?.graph_kinds ?? []
      if (kinds.length) {
        const at = kinds.findIndex((k) => k.value === effectiveGraph)
        setGraphKind(kinds[(at + 1) % kinds.length].value)
      }
      return
    }
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'm') {
      e.preventDefault()
      const list = profiles.data ?? []
      if (list.length) {
        const at = list.findIndex((p) => p.id === effectiveProfile)
        setProfileId(list[(at + 1) % list.length].id)
      }
    }
  }

  // フォーカストラップ。Tab が背後の画面へ抜けないようにする
  function onKeyDownCapture(e: React.KeyboardEvent) {
    if (e.key !== 'Tab' || !dialogRef.current) return
    const focusables = dialogRef.current.querySelectorAll<HTMLElement>(
      'button:not(:disabled), input, select, textarea, [tabindex]:not([tabindex="-1"])',
    )
    if (!focusables.length) return
    const first = focusables[0]
    const last = focusables[focusables.length - 1]
    if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault()
      first.focus()
    } else if (e.shiftKey && document.activeElement === first) {
      e.preventDefault()
      last.focus()
    }
  }

  if (!open) return null

  return (
    <div className="launcher-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div
        className="launcher"
        role="dialog"
        aria-modal="true"
        aria-label="実行ランチャー"
        ref={dialogRef}
        onKeyDown={onKeyDown}
        onKeyDownCapture={onKeyDownCapture}
      >
        <div className="launcher-search">
          <Search size={17} strokeWidth={2} />
          <input
            ref={inputRef}
            value={query}
            placeholder="タスク・ツールを検索して実行"
            onChange={(e) => {
              setQuery(e.target.value)
              setIndex(0)
            }}
          />
          <Keycap>ESC</Keycap>
        </div>

        <div className="launcher-list">
          <div className="launcher-section">
            <Kicker>実行する / RUN</Kicker>
          </div>
          {items.length === 0 && (
            <div style={{ padding: '10px 16px', opacity: 0.6, fontSize: 13 }}>
              一致するものがありません。
            </div>
          )}
          {items.map((item, i) => {
            const p = (profiles.data ?? []).find(
              (x) => x.id === (item.llm_profile_id ?? meta.data?.default_profile?.id),
            )
            const kindLabel =
              meta.data?.graph_kinds.find(
                (k) => k.value === (item.graph_kind ?? meta.data?.default_graph_kind),
              )?.label ?? ''
            return (
              <button
                key={`${item.kind}-${item.id}`}
                className={`launcher-row${i === index ? ' sel' : ''}`}
                onMouseEnter={() => setIndex(i)}
                onClick={() => void execute()}
              >
                <span>{item.kind === 'agent' ? '🧠' : '🔧'}</span>
                <span className="launcher-main">
                  <span className="launcher-title">{item.name}</span>
                  <span className="launcher-sub">
                    {item.kind === 'agent' ? 'AGENT' : 'TOOL'}
                    {p ? ` · ${p.provider === 'api' ? '☁️' : '🖥️'} ${p.model}` : ''}
                    {item.kind === 'agent' && kindLabel ? ` · ${kindLabel}` : ''}
                  </span>
                </span>
                <span className="launcher-enter">↵ 実行</span>
              </button>
            )
          })}
        </div>

        <div className="launcher-options">
          {selected?.kind === 'agent' && (
            <>
              <div className="opt-block">
                <div className="opt-head">
                  <Kicker>実行方式 / Strategy</Kicker>
                  <Keycap>⌘E</Keycap>
                </div>
                <div className="radio-cards">
                  {(meta.data?.graph_kinds ?? []).map((k) => (
                    <button
                      key={k.value}
                      className={`radio-card${effectiveGraph === k.value ? ' on' : ''}`}
                      onClick={() => setGraphKind(k.value)}
                    >
                      <div className="radio-card-title">{k.label}</div>
                      <div className="radio-card-desc">{k.description}</div>
                    </button>
                  ))}
                </div>
              </div>

              <div className="opt-block">
                <div className="opt-head">
                  <Kicker>モデル / Model</Kicker>
                  <Keycap>⌘M</Keycap>
                </div>
                <div className="model-list">
                  {(profiles.data ?? []).map((p) => (
                    <button
                      key={p.id}
                      className={`model-row${effectiveProfile === p.id ? ' on' : ''}`}
                      onClick={() => setProfileId(p.id)}
                    >
                      <span>{p.provider === 'api' ? <Cloud size={13} /> : <MonitorSmartphone size={13} />}</span>
                      <span className="model-name">{p.name}</span>
                      {selected?.llm_profile_id === p.id && <Tag kind="outline">タスクの既定</Tag>}
                      {p.provider === 'local' && <Tag kind="neutral">ローカル・無料</Tag>}
                      <span className="model-meta">
                        {p.max_output_tokens ? `${Number(p.max_output_tokens).toLocaleString()} tok` : '—'}
                      </span>
                    </button>
                  ))}
                </div>
              </div>
            </>
          )}

          <div className="opt-block">
            <div className="row">
              <div className="seg">
                <button className={`seg-opt${when === 'now' ? ' on' : ''}`} onClick={() => setWhen('now')}>
                  今すぐ / Now
                </button>
                <button className={`seg-opt${when === 'cron' ? ' on' : ''}`} onClick={() => setWhen('cron')}>
                  cron で定期
                </button>
              </div>
              {when === 'cron' && (
                <input
                  className="input mono"
                  style={{ width: 160 }}
                  value={cron}
                  onChange={(e) => setCron(e.target.value)}
                  aria-label="cron式"
                />
              )}
              {selected?.kind === 'agent' && when === 'now' && (
                <label className="row" style={{ gap: 6, fontSize: 12 }}>
                  <input
                    type="checkbox"
                    checked={saveDefault}
                    onChange={(e) => setSaveDefault(e.target.checked)}
                  />
                  この選択をタスクの既定にする
                </label>
              )}
              <div style={{ flex: 1 }} />
              <button className="btn btn-primary" onClick={() => void execute()} disabled={!selected || busy}>
                <Play size={13} style={{ marginRight: 6 }} />
                {busy ? '実行中…' : '実行 / Run'}
              </button>
            </div>
            {error && <div className="notice" style={{ marginTop: 8 }}>{error}</div>}
          </div>
        </div>
      </div>
    </div>
  )
}
