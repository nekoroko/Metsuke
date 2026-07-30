// src/pages/RunDetail.tsx — 実行詳細（手順書 5.3）。製品の売り。
//
// 4つのビューを持つ: タイムライン(1d) / グラフ(1e) / トレース(1f) /
// ステートマシン(3b)。どれも state["trace"] と history から描き、
// ノード名や状態名はハードコードしない（graph.py 側が正）。

import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { ArrowLeft, ArrowLeftRight, RotateCcw, Square } from 'lucide-react'
import Markdown from 'react-markdown'

import { api, subscribeRun } from '../api/client'
import { keys, useExecution, useInvalidate, useLlmProfiles } from '../api/hooks'
import type { RunEvent, Step, TraceEntry } from '../api/types'
import { Empty, Kicker, Progress, StatusTag, Tag, elapsed, hhmmss } from '../components/ui'

type View = 'timeline' | 'graph' | 'trace'

/** ReAct のレビュアー。graph.py の REVIEWER_REGISTRY と同じ並び。 */
const REVIEWERS = ['fact_checker', 'code_reviewer', 'security_reviewer', 'data_analyst', 'generic_reviewer']

export function RunDetailPage() {
  const { execId } = useParams<{ execId: string }>()
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()
  const query = useExecution(execId)
  const profiles = useLlmProfiles()
  const invalidate = useInvalidate()

  const view = (params.get('view') as View) ?? 'timeline'
  const [live, setLive] = useState<{ steps: Step[]; trace: TraceEntry[]; status?: string; stdout?: string }>({
    steps: [], trace: [],
  })
  const [openSteps, setOpenSteps] = useState<Set<number>>(new Set())
  const followTail = useRef(true)
  const mainRef = useRef<HTMLDivElement>(null)

  const exec = query.data
  const isRunning = (live.status ?? exec?.status) === 'running'

  // 実行中なら SSE で追う。ページを離れて戻ってきた場合も、サーバが
  // 履歴を頭から流し直すので、ここで組み立て直せば復元できる
  useEffect(() => {
    if (!execId) return
    setLive({ steps: [], trace: [] })
    const stop = subscribeRun(execId, (e: RunEvent) => {
      setLive((prev) => {
        if (e.type === 'step' || e.type === 'result') {
          const { type, ...step } = e
          void type
          return { ...prev, steps: [...prev.steps, step as Step] }
        }
        if (e.type === 'trace') {
          const { type, ...entry } = e
          void type
          return { ...prev, trace: [...prev.trace, entry as TraceEntry] }
        }
        if (e.type === 'status') return { ...prev, status: e.status }
        if (e.type === 'final') {
          // 完了したら一覧側も更新する（ポーリングはしない）
          invalidate(keys.executions)
          invalidate(keys.execution(execId))
          return { ...prev, status: e.status, stdout: e.stdout }
        }
        return prev
      })
    })
    return stop
  }, [execId])                                        // eslint-disable-line react-hooks/exhaustive-deps

  // 末尾にいるときだけ自動スクロール。上を読んでいる間は動かさない
  useEffect(() => {
    const el = mainRef.current
    if (!el || !followTail.current) return
    el.scrollTop = el.scrollHeight
  }, [live.steps.length])

  const steps: Step[] = live.steps.length ? live.steps : (exec?.steps ?? [])
  const trace: TraceEntry[] = live.trace.length ? live.trace : (exec?.trace ?? [])
  const stdout = live.stdout ?? exec?.stdout ?? ''

  if (!execId) return null
  if (query.isLoading) return <div className="page-body">読み込み中…</div>
  if (query.isError || !exec) return <Empty message="この実行は見つかりませんでした。" />

  const profile = (profiles.data ?? []).find((p) => p.id === exec.llm_info?.id)
  const maxSteps = 12
  const currentStep = steps.filter((s) => s.role === 'assistant').length

  function setView(v: View) {
    params.set('view', v)
    setParams(params, { replace: true })
  }

  async function rerunWithOtherModel() {
    const others = (profiles.data ?? []).filter((p) => p.id !== exec!.llm_info?.id)
    const next = others[0]
    if (!next) return
    const res = await api.agentTasks.run(exec!.target_id, { llm_profile_id: next.id })
    invalidate(keys.executions)
    navigate(`/runs/${res.exec_id}`)
  }

  return (
    <>
      <header className="run-header">
        <Link to="/runs" className="btn btn-ghost btn-sm">
          <ArrowLeft size={13} style={{ marginRight: 5 }} />
          実行履歴
        </Link>
        <span className="vrule" />
        <div>
          <div className="run-name">{exec.target_name}</div>
          <div className="run-meta">
            EXEC {exec.id} · {exec.trigger === 'manual' ? '手動実行 / MANUAL' : '定期実行 / SCHEDULE'} ·{' '}
            {hhmmss(exec.started_at)}
          </div>
        </div>
        <span className="vrule" />
        <div>
          <Kicker>Model</Kicker>
          <div className="row" style={{ marginTop: 4 }}>
            <span className="model-chip">
              {exec.llm_info?.provider === 'api' ? '☁️' : '🖥️'} {exec.llm_info?.model ?? '—'}
            </span>
            <span className="run-meta" style={{ margin: 0 }}>
              {exec.llm_info?.max_output_tokens
                ? `${Number(exec.llm_info.max_output_tokens).toLocaleString()} tok`
                : ''}
              {exec.llm_info?.disable_thinking === 'false' ? ' · Extended Thinking' : ''}
            </span>
          </div>
        </div>
        <div style={{ flex: 1 }} />
        <div className="row">
          <StatusTag status={live.status ?? exec.status} />
          {isRunning && <span className="run-meta" style={{ margin: 0 }}>{elapsed(exec.started_at)}</span>}
        </div>
        <div className="seg">
          {(['timeline', 'graph', 'trace'] as View[]).map((v) => (
            <button key={v} className={`seg-opt${view === v ? ' on' : ''}`} onClick={() => setView(v)}>
              {v === 'timeline' ? 'タイムライン' : v === 'graph' ? 'グラフ' : '生ログ'}
            </button>
          ))}
        </div>
        {isRunning ? (
          <button
            className="btn btn-secondary btn-sm"
            onClick={async () => {
              await api.executions.cancel(exec.id)
              invalidate(keys.execution(exec.id))
            }}
          >
            <Square size={12} style={{ marginRight: 5 }} />停止
          </button>
        ) : (
          <button className="btn btn-secondary btn-sm" onClick={() => void rerunWithOtherModel()}>
            <RotateCcw size={12} style={{ marginRight: 5 }} />別モデルで再実行
          </button>
        )}
      </header>

      <div className="run-body">
        <div
          className="run-main"
          ref={mainRef}
          onScroll={(e) => {
            const el = e.currentTarget
            followTail.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40
          }}
        >
          {view === 'timeline' && (
            <>
              <div className="row" style={{ justifyContent: 'space-between', marginBottom: 8 }}>
                <Kicker>{exec.graph_label || 'React Loop'}</Kicker>
                <span className="run-meta" style={{ margin: 0 }}>
                  STEP {currentStep} / {maxSteps}
                </span>
              </div>
              <Progress value={currentStep} max={maxSteps} ink />
              <div className="timeline" style={{ marginTop: 20 }}>
                {steps.map((s, i) => (
                  <TimelineRow
                    key={`${s.step}-${i}`}
                    step={s}
                    time={hhmmss(exec.started_at)}
                    live={isRunning && i === steps.length - 1}
                    open={openSteps.has(i)}
                    onToggle={() =>
                      setOpenSteps((prev) => {
                        const next = new Set(prev)
                        next.has(i) ? next.delete(i) : next.add(i)
                        return next
                      })
                    }
                  />
                ))}
                {steps.length === 0 && <div style={{ opacity: 0.6 }}>まだステップがありません。</div>}
              </div>
            </>
          )}

          {view === 'graph' && <GraphView trace={trace} kind={exec.graph_kind} />}
          {view === 'trace' && <TraceView trace={trace} steps={steps} />}
        </div>

        <aside className="run-side">
          <div className="side-block">
            <Kicker>Critic / Reviewers</Kicker>
            <div style={{ marginTop: 8 }}>
              {REVIEWERS.map((r) => {
                const used = trace.some((t) => (t.summary ?? '').includes(r))
                return (
                  <div key={r} className="row" style={{ opacity: used ? 1 : 0.5, padding: '3px 0' }}>
                    <span className="mono" style={{ fontSize: 12 }}>{r}</span>
                  </div>
                )
              })}
            </div>
            <div className="cell-sub" style={{ marginTop: 8 }}>
              ACTION=DONE の後に起動。要修正なら react に戻る（最大2回）。
            </div>
          </div>

          <div className="side-block">
            <Kicker>Budget</Kicker>
            <div style={{ marginTop: 8 }}>
              <div className="run-meta" style={{ margin: '0 0 4px' }}>
                ステップ {currentStep} / {maxSteps}
              </div>
              <Progress value={currentStep} max={maxSteps} ink />
            </div>
            <div className="row" style={{ marginTop: 10 }}>
              {profile?.disable_thinking === false && <Tag kind="outline">Thinking 有効</Tag>}
              {exec.llm_info?.max_output_tokens && (
                <Tag kind="neutral">上限 {Number(exec.llm_info.max_output_tokens).toLocaleString()} tok</Tag>
              )}
            </div>
          </div>

          <div className="side-block">
            <div className="row" style={{ justifyContent: 'space-between' }}>
              <Kicker>最終結果 / Final Answer</Kicker>
              {stdout && (
                <button className="btn btn-ghost btn-sm" onClick={() => void navigator.clipboard.writeText(stdout)}>
                  📋 コピー
                </button>
              )}
            </div>
            {stdout ? (
              <div style={{ marginTop: 8, fontSize: 13.5, lineHeight: 1.7 }}>
                <Markdown>{stdout}</Markdown>
              </div>
            ) : (
              <div
                style={{
                  marginTop: 8, border: '1px dashed var(--color-divider)',
                  padding: 16, opacity: 0.6, fontSize: 12.5,
                }}
              >
                実行が完了すると、ここに最終結果が出ます。
              </div>
            )}
            {exec.stderr && (
              <div className="notice" style={{ marginTop: 10 }}>{exec.stderr}</div>
            )}
          </div>

          <div className="side-block">
            <button className="btn btn-secondary btn-block" onClick={() => void rerunWithOtherModel()}>
              <ArrowLeftRight size={13} style={{ marginRight: 6 }} />
              別モデルで再実行
            </button>
          </div>
        </aside>
      </div>
    </>
  )
}

function TimelineRow({
  step, time, live, open, onToggle,
}: {
  step: Step; time: string; live: boolean; open: boolean; onToggle: () => void
}) {
  const isResult = step.role === 'result'
  return (
    <div className="tl-row">
      <div className="tl-time">
        {time}
        <br />
        {isResult ? '観測' : ''}
      </div>
      <div className="tl-axis">
        <i className={`tl-node${isResult ? ' tool' : ''}${live ? ' live' : ''}`} />
      </div>
      <div className="tl-body">
        {step.thought && (
          <>
            <div className="tl-kind">🧠 思考 / THOUGHT</div>
            <div className="tl-text">
              {step.thought}
              {live && <span className="caret" />}
            </div>
          </>
        )}
        {step.action && (
          <>
            <div className="tl-kind action">🔧 ツール実行 / ACTION</div>
            <div className="tl-text mono" style={{ fontSize: 12.5 }}>{step.action}</div>
          </>
        )}
        {step.done && (
          <>
            <div className="tl-kind">✅ 完了 / DONE</div>
            <div className="tl-text">{step.done}</div>
          </>
        )}
        {step.text && <div className="tl-text">{step.text}</div>}
        {isResult && step.result && (
          <div className="obs">
            <div className="obs-head">
              <span>observation</span>
              <span style={{ opacity: 0.65 }}>{step.result.length.toLocaleString()} 文字</span>
            </div>
            <div className={`obs-body${open ? ' open' : ''}`}>{step.result}</div>
            {step.result.length > 200 && (
              <button className="obs-toggle" onClick={onToggle}>
                {open ? '閉じる / Collapse ↑' : '全文を開く / Expand ↓'}
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

/** グラフ（1e）と、ステートマシンのとき（3b）は同じ trace から描き分ける。 */
function GraphView({ trace, kind }: { trace: TraceEntry[]; kind: string | null }) {
  const visits = useMemo(() => {
    const m = new Map<string, number>()
    for (const t of trace) {
      const n = t.node ?? '?'
      m.set(n, (m.get(n) ?? 0) + 1)
    }
    return m
  }, [trace])

  const order = useMemo(() => {
    const seen: string[] = []
    for (const t of trace) {
      const n = t.node ?? '?'
      if (!seen.includes(n)) seen.push(n)
    }
    return seen
  }, [trace])

  if (!trace.length) return <Empty message="ノード遷移がまだ記録されていません。" />

  // ステートマシンは工程が固定なので、横並びの状態カードで見せる（3b）
  if (kind === 'research') {
    const last = trace[trace.length - 1]?.node
    return (
      <>
        <Kicker>State Machine</Kicker>
        <div className="sm-row" style={{ marginTop: 12 }}>
          {order.map((node, i) => {
            const done = order.indexOf(last ?? '') > i
            const active = node === last
            return (
              <div key={node} style={{ display: 'flex' }}>
                <div className={`sm-card${active ? ' active' : done ? ' done' : ''}`}>
                  <div className="sm-name">{node}</div>
                  <div className="gnode-meta">visits {visits.get(node)}</div>
                </div>
                {i < order.length - 1 && <div className="sm-arrow">→</div>}
              </div>
            )
          })}
        </div>
        {trace.some((t) => t.skipped) && (
          <div className="sm-guard">
            素通りしたノード:{' '}
            {trace.filter((t) => t.skipped).map((t) => `${t.node}（${t.note ?? t.summary ?? ''}）`).join(' / ')}
          </div>
        )}
        <div style={{ marginTop: 20 }}>
          <Kicker>状態ログ / State Log</Kicker>
          <table className="grid" style={{ marginTop: 8 }}>
            <thead>
              <tr><th>#</th><th>STATE</th><th>内容</th><th>次</th></tr>
            </thead>
            <tbody>
              {trace.map((t, i) => (
                <tr key={i} style={t.skipped ? { opacity: 0.55 } : undefined}>
                  <td className="mono">{t.seq ?? i + 1}</td>
                  <td className="cell-nowrap mono">{t.node}</td>
                  <td>{t.summary}{t.note ? ` — ${t.note}` : ''}</td>
                  <td className="cell-nowrap mono">{t.next}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </>
    )
  }

  return (
    <>
      <Kicker>React Loop</Kicker>
      <div className="graph-row" style={{ marginTop: 12 }}>
        <div className="gnode filled">
          <div className="gnode-title">タスク投入</div>
        </div>
        {order.map((node) => (
          <div key={node} className={`gnode${node === 'react' ? ' active' : ''}`}>
            <div className="gnode-title">{node}</div>
            <div className="gnode-meta">visits {visits.get(node)}</div>
          </div>
        ))}
      </div>
      {trace.some((t) => t.next === 'react' && t.node !== 'react') && (
        <div className="gloop">↰ 差し戻し → react へ戻す</div>
      )}
      <div style={{ marginTop: 20 }}>
        <Kicker>Reviewers</Kicker>
        <div className="reviewer-grid" style={{ marginTop: 8 }}>
          {REVIEWERS.map((r) => {
            const used = trace.some((t) => (t.summary ?? '').includes(r))
            return (
              <div key={r} className={`gnode${used ? '' : ' dim'}`} style={{ minWidth: 0 }}>
                <div className="gnode-meta">{r}</div>
                {used && <div className="kicker" style={{ marginTop: 4 }}>Selected</div>}
              </div>
            )
          })}
        </div>
      </div>
    </>
  )
}

/** トレース（1f）。所要時間は持っていないので、ステップ順の帯で見せる。 */
function TraceView({ trace, steps }: { trace: TraceEntry[]; steps: Step[] }) {
  if (!trace.length) return <Empty message="ノード遷移がまだ記録されていません。" />
  const total = trace.length
  const thoughts = steps.filter((s) => s.thought).length
  const actions = steps.filter((s) => s.action).length
  const retries = trace.filter((t) => t.next === 'react' && t.node !== 'react').length
  const skipped = trace.filter((t) => t.skipped).length

  return (
    <>
      <div className="row" style={{ marginBottom: 12 }}>
        <Tag kind="neutral">🧠 思考 {thoughts}</Tag>
        <Tag kind="neutral">🔧 ツール {actions}</Tag>
        <Tag kind="accent">↩ 差し戻し {retries}</Tag>
        <Tag kind="outline">素通り {skipped}</Tag>
      </div>
      {trace.map((t, i) => {
        const retry = t.next === 'react' && t.node !== 'react'
        const width = Math.max(6, Math.round((1 / total) * 100 * 3))
        return (
          <div className="trace-row" key={i}>
            <div className="trace-label">{t.node}</div>
            <div className="trace-track">
              <div
                className={`trace-bar${retry ? ' retry' : t.node === 'react' ? '' : ' tool'}`}
                style={{ left: `${Math.round((i / total) * 100)}%`, width: `${width}%` }}
              />
            </div>
            <div className="trace-secs">#{t.seq ?? i + 1}</div>
          </div>
        )
      })}
      <div className="cell-sub" style={{ marginTop: 12 }}>
        凡例: ink = LLM思考 / グレー = ツール・検索 / アクセント = 差し戻し。
        {total > 0 && ` やり直しが全体の ${Math.round((retries / total) * 100)}%。`}
      </div>
    </>
  )
}
