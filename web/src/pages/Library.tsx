// src/pages/Library.tsx — ツール（Type1）のコード編集・プレビュー・AI生成/修正。
//
// Streamlit 版の「ツール作成」タブと同じことができること（受け入れ条件 §11）。
// 生成と修正は SSE で流れてくるので、途中経過をそのまま出す。

import { useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Play, Sparkles, Trash2, Wrench } from 'lucide-react'

import { api, streamPost } from '../api/client'
import { keys, useAgentTasks, useInvalidate, useTools } from '../api/hooks'
import type { Tool } from '../api/types'
import { Empty, Kicker, StatusTag, Tag } from '../components/ui'

export function LibraryPage() {
  const [params, setParams] = useSearchParams()
  const tools = useTools()
  const tasks = useAgentTasks()
  const invalidate = useInvalidate()

  const selectedId = params.get('tool')
  const selected = (tools.data ?? []).find((t) => t.id === selectedId)

  return (
    <>
      <header className="page-header">
        <div>
          <div className="kicker">Library</div>
          <h2 className="page-title">ライブラリ</h2>
        </div>
        <div className="page-header-actions">
          <button
            className="btn btn-secondary"
            onClick={() => {
              params.delete('tool')
              params.set('new', '1')
              setParams(params)
            }}
          >
            ＋ 新規ツール
          </button>
        </div>
      </header>

      <div className="page-body">
        {params.get('new') === '1' || !selected ? (
          <Creator
            onCreated={(id) => {
              invalidate(keys.tools)
              params.delete('new')
              params.set('tool', id)
              setParams(params)
            }}
          />
        ) : null}

        {selected && <ToolEditor tool={selected} onChanged={() => invalidate(keys.tools)} />}

        <div style={{ marginTop: 32 }}>
          <Kicker>保存済みツール / Saved</Kicker>
          {(tools.data ?? []).length === 0 ? (
            <Empty message="まだツールがありません。" />
          ) : (
            <table className="grid" style={{ marginTop: 8 }}>
              <thead>
                <tr><th>名前</th><th>状態</th><th>使っているタスク</th><th /></tr>
              </thead>
              <tbody>
                {(tools.data ?? []).map((t) => {
                  const users = (tasks.data ?? []).filter((a) => a.allowed_tool_ids.includes(t.id))
                  return (
                    <tr key={t.id}>
                      <td>
                        <div className="cell-name">{t.name}</div>
                        <div className="cell-sub">{t.description}</div>
                      </td>
                      <td className="cell-nowrap"><StatusTag status={t.status} /></td>
                      <td className="cell-sub">{users.map((u) => u.name).join('、') || '—'}</td>
                      <td>
                        <div className="row-actions">
                          <button
                            className="btn btn-secondary btn-sm"
                            onClick={() => {
                              params.delete('new')
                              params.set('tool', t.id)
                              setParams(params)
                            }}
                          >
                            開く
                          </button>
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </>
  )
}

/** AIにツールを作ってもらう（ai_creator.generate_tool の SSE を出す）。 */
function Creator({ onCreated }: { onCreated: (id: string) => void }) {
  const [prompt, setPrompt] = useState('')
  const [log, setLog] = useState<string[]>([])
  const [code, setCode] = useState('')
  const [toolName, setToolName] = useState('')
  const [description, setDescription] = useState('')
  const [busy, setBusy] = useState(false)
  const abort = useRef<AbortController | null>(null)

  useEffect(() => () => abort.current?.abort(), [])

  async function generate() {
    setBusy(true)
    setLog([])
    setCode('')
    abort.current = new AbortController()
    try {
      await streamPost('/tools/generate', { prompt }, (e) => {
        if (e.type === 'error') {
          setLog((prev) => [...prev, `エラー: ${String(e.message)}`])
          return
        }
        const turn = e.turn ? `#${e.turn} ` : ''
        if (e.code) {
          setCode(String(e.code))
          setToolName(String(e.tool_name ?? ''))
          setDescription(String(e.description ?? ''))
          setLog((prev) => [...prev, `${turn}コードを生成しました`])
        } else if (e.not_suitable) {
          setLog((prev) => [...prev, `${turn}ツール化に向きません: ${String(e.not_suitable)}`])
        } else if (e.raw) {
          setLog((prev) => [...prev, `${turn}${String(e.raw).slice(0, 200)}`])
        }
      }, abort.current.signal)
    } catch (e) {
      setLog((prev) => [...prev, `失敗: ${e instanceof Error ? e.message : String(e)}`])
    } finally {
      setBusy(false)
    }
  }

  return (
    <div>
      <Kicker>AI にツールを作ってもらう</Kicker>
      <div className="form-grid" style={{ marginTop: 8 }}>
        <div className="field">
          <textarea
            className="input" rows={3} value={prompt}
            placeholder="例: 日経平均の終値を取得して表示する"
            onChange={(e) => setPrompt(e.target.value)}
          />
        </div>
        <div className="row">
          <button className="btn btn-primary" onClick={() => void generate()} disabled={!prompt.trim() || busy}>
            <Sparkles size={13} style={{ marginRight: 6 }} />
            {busy ? '生成中…' : 'コードを生成'}
          </button>
          {busy && <button className="btn btn-secondary" onClick={() => abort.current?.abort()}>中止</button>}
        </div>
        {log.length > 0 && (
          <div className="obs">
            <div className="obs-head"><span>generator</span></div>
            <div className="obs-body open">{log.join('\n')}</div>
          </div>
        )}
        {code && (
          <ToolEditor
            tool={{
              id: '', name: toolName || '新しいツール', description,
              code, status: 'draft', created_at: '', updated_at: '',
            }}
            onCreated={onCreated}
          />
        )}
      </div>
    </div>
  )
}

function ToolEditor({
  tool, onChanged, onCreated,
}: {
  tool: Tool
  onChanged?: () => void
  onCreated?: (id: string) => void
}) {
  const invalidate = useInvalidate()
  const [name, setName] = useState(tool.name)
  const [description, setDescription] = useState(tool.description)
  const [code, setCode] = useState(tool.code)
  const [network, setNetwork] = useState(false)
  const [result, setResult] = useState<{ status: string; stdout: string; stderr: string } | null>(null)
  const [busy, setBusy] = useState(false)
  const [fixing, setFixing] = useState(false)

  useEffect(() => {
    setName(tool.name)
    setDescription(tool.description)
    setCode(tool.code)
    setResult(null)
  }, [tool.id, tool.code])                            // eslint-disable-line react-hooks/exhaustive-deps

  async function preview() {
    setBusy(true)
    try {
      setResult(await api.preview({ code, network }))
    } finally {
      setBusy(false)
    }
  }

  async function fix() {
    if (!result) return
    setFixing(true)
    try {
      await streamPost('/tools/fix', {
        code, stdout: result.stdout, stderr: result.stderr, original_prompt: description,
      }, (e) => {
        if (e.code) setCode(String(e.code))
      })
    } finally {
      setFixing(false)
    }
  }

  async function save(status: 'draft' | 'verified') {
    if (!tool.id) {
      const created = await api.tools.create({ name, description, code, status })
      invalidate(keys.tools)
      onCreated?.(created.id)
      return
    }
    await api.tools.update(tool.id, { name, description, code, status })
    invalidate(keys.tools)
    onChanged?.()
  }

  return (
    <div style={{ marginTop: 20, borderTop: '2px solid var(--color-divider)', paddingTop: 16 }}>
      <div className="row" style={{ justifyContent: 'space-between' }}>
        <Kicker>{tool.id ? 'ツールを編集' : '生成されたコード'}</Kicker>
        {tool.id && <StatusTag status={tool.status} />}
      </div>
      <div className="form-grid" style={{ marginTop: 8 }}>
        <div className="field">
          <label htmlFor="tool-name">名前</label>
          <input id="tool-name" className="input" value={name} onChange={(e) => setName(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="tool-desc">説明</label>
          <input id="tool-desc" className="input" value={description} onChange={(e) => setDescription(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="tool-code">コード</label>
          <textarea id="tool-code" className="input" rows={14} value={code} onChange={(e) => setCode(e.target.value)} />
        </div>
        <div className="row">
          <label className="row" style={{ gap: 6, fontSize: 12 }}>
            <input type="checkbox" checked={network} onChange={(e) => setNetwork(e.target.checked)} />
            ネットワークを許可して実行
          </label>
          <button className="btn btn-secondary" onClick={() => void preview()} disabled={busy}>
            <Play size={13} style={{ marginRight: 6 }} />
            {busy ? '実行中…' : 'プレビュー実行'}
          </button>
          {result?.status === 'error' && (
            <button className="btn btn-secondary" onClick={() => void fix()} disabled={fixing}>
              <Wrench size={13} style={{ marginRight: 6 }} />
              {fixing ? '修正中…' : 'AIに修正させる'}
            </button>
          )}
          <div style={{ flex: 1 }} />
          <button className="btn btn-secondary" onClick={() => void save('draft')}>下書き保存</button>
          <button className="btn btn-primary" onClick={() => void save('verified')}>検証済みとして保存</button>
          {tool.id && (
            <button
              className="btn btn-ghost btn-sm"
              onClick={async () => {
                await api.tools.remove(tool.id)
                invalidate(keys.tools)
                onChanged?.()
              }}
            >
              <Trash2 size={12} />
            </button>
          )}
        </div>

        {result && (
          <div className="obs">
            <div className="obs-head">
              <span>preview</span>
              <Tag kind={result.status === 'done' ? 'neutral' : 'accent'}>
                {result.status === 'done' ? '成功' : 'エラー'}
              </Tag>
            </div>
            <div className="obs-body open">{result.stdout || result.stderr || '(出力なし)'}</div>
          </div>
        )}
      </div>
    </div>
  )
}
