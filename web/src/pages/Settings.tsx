// src/pages/Settings.tsx — 設定（手順書 5.4 の モデル管理 / 5.5 の分割）
//
// 現状の縦積み1画面を「モデル / 生成の予算 / 検索 / サンドボックス」に分ける。
// 接続テストは各項目の隣で完結させる（現状は画面末尾に離れている）。
//
// **APIキーは表示しない。** サーバは has_api_key しか返さないので、
// 入力欄は常に空から始まる。空のまま保存しても既存の鍵は消えない。

import { useEffect, useState } from 'react'
import { NavLink, useParams } from 'react-router-dom'
import { Cloud, MonitorSmartphone, Plug, Trash2 } from 'lucide-react'

import { api } from '../api/client'
import { keys, useInvalidate, useLlmProfiles, useSettings } from '../api/hooks'
import type { LlmProfile, MountCheck, ProfileTestResult } from '../api/types'
import { Empty, Kicker, Tag } from '../components/ui'

const SECTIONS = [
  { key: 'models', ja: 'モデル', en: 'Models' },
  { key: 'budget', ja: '生成の予算', en: 'Budget' },
  { key: 'search', ja: '検索', en: 'Search' },
  { key: 'sandbox', ja: 'サンドボックス', en: 'Sandbox' },
]

export function SettingsPage() {
  const { section = 'models' } = useParams<{ section: string }>()
  return (
    <div className="settings-layout">
      <nav className="settings-nav sidenav">
        <div style={{ padding: '14px 16px', borderBottom: '2px solid var(--color-divider)' }}>
          <Kicker>Settings</Kicker>
        </div>
        {SECTIONS.map((s) => (
          <NavLink
            key={s.key}
            to={`/settings/${s.key}`}
            className={({ isActive }) => `sidenav-item${isActive ? ' active' : ''}`}
          >
            <span className="sidenav-label">{s.ja} / {s.en}</span>
          </NavLink>
        ))}
        <div style={{ padding: 16, marginTop: 'auto' }}>
          <div className="cell-sub">
            設定は agent_studio.db に保存されます。APIキーは平文なので
            .gitignore 済みです。
          </div>
        </div>
      </nav>

      <div style={{ flex: 1, minWidth: 0, overflow: 'auto' }}>
        {section === 'models' && <ModelsSection />}
        {section === 'budget' && <BudgetSection />}
        {section === 'search' && <SearchSection />}
        {section === 'sandbox' && <SandboxSection />}
      </div>
    </div>
  )
}

/* ===== モデル（2a） ===== */

interface ProfileForm {
  name: string
  provider: 'local' | 'api'
  provider_kind: 'openai_compatible' | 'anthropic'
  base_url: string
  model: string
  api_key: string
  max_output_tokens: string
  disable_thinking: boolean
}

const EMPTY_FORM: ProfileForm = {
  name: '', provider: 'local', provider_kind: 'openai_compatible',
  base_url: '', model: '', api_key: '', max_output_tokens: '3000', disable_thinking: true,
}

function ModelsSection() {
  const profiles = useLlmProfiles()
  const invalidate = useInvalidate()
  const [editing, setEditing] = useState<string | 'new' | null>(null)
  const [tests, setTests] = useState<Record<string, ProfileTestResult>>({})
  const [testing, setTesting] = useState<string | null>(null)
  const [error, setError] = useState('')

  async function testOne(id: string) {
    setTesting(id)
    try {
      const result = await api.llmProfiles.test(id)
      setTests((prev) => ({ ...prev, [id]: result }))
    } finally {
      setTesting(null)
    }
  }

  return (
    <>
      <header className="page-header">
        <div>
          <div className="kicker">Models</div>
          <h2 className="page-title">モデル</h2>
        </div>
        <div className="page-header-actions">
          <button
            className="btn btn-secondary"
            onClick={async () => {
              for (const p of profiles.data ?? []) await testOne(p.id)
            }}
          >
            <Plug size={13} style={{ marginRight: 6 }} />全件テスト / Test all
          </button>
          <button className="btn btn-primary" onClick={() => setEditing('new')}>＋ モデルを追加</button>
        </div>
      </header>

      <div className="page-body">
        {error && <div className="notice" style={{ marginBottom: 12 }}>{error}</div>}
        <table className="grid">
          <thead>
            <tr>
              <th style={{ width: 26 }} />
              <th style={{ minWidth: 270 }}>名前</th>
              <th style={{ width: 200 }}>接続先</th>
              <th style={{ width: 92 }}>上限</th>
              <th style={{ width: 130 }}>状態</th>
              <th style={{ width: 1 }} />
            </tr>
          </thead>
          <tbody>
            {(profiles.data ?? []).map((p) => {
              const t = tests[p.id]
              return (
                <tr key={p.id} style={p.is_default ? { background: 'var(--color-accent-100)' } : undefined}>
                  <td>{p.provider === 'api' ? <Cloud size={14} /> : <MonitorSmartphone size={14} />}</td>
                  <td>
                    <div className="row cell-nowrap" style={{ gap: 6 }}>
                      <span className="cell-name">{p.name}</span>
                      {p.is_default && <Tag kind="accent">★ 既定 / DEFAULT</Tag>}
                    </div>
                    <div className="cell-sub">
                      {p.provider === 'local' ? 'ローカル' : 'API'}
                      {p.provider_kind === 'anthropic' ? '（Anthropic）' : '（OpenAI互換）'}
                      {' · '}Thinking {p.disable_thinking ? '無効化ON' : '有効'}
                      {p.has_api_key ? ' · APIキー登録済み' : ''}
                    </div>
                  </td>
                  <td className="mono" style={{ fontSize: 11 }}>
                    {p.base_url || 'api.anthropic.com'}
                    <div className="cell-sub mono">{p.model}</div>
                  </td>
                  <td className="mono cell-nowrap" style={{ fontSize: 11.5 }}>
                    {p.max_output_tokens ? Number(p.max_output_tokens).toLocaleString() : '—'}
                  </td>
                  <td className="cell-nowrap">
                    {testing === p.id ? (
                      <Tag kind="outline">テスト中…</Tag>
                    ) : t ? (
                      t.ok ? (
                        <Tag kind="neutral">✓ OK · {t.latency_ms}ms</Tag>
                      ) : (
                        <Tag kind="accent">✕ 要確認</Tag>
                      )
                    ) : (
                      <Tag kind="outline">— 未テスト</Tag>
                    )}
                  </td>
                  <td>
                    <div className="row-actions">
                      <button className="btn btn-ghost btn-sm" onClick={() => void testOne(p.id)}>テスト</button>
                      <button className="btn btn-secondary btn-sm" onClick={() => setEditing(p.id)}>編集</button>
                      {!p.is_default && (
                        <button
                          className="btn btn-ghost btn-sm"
                          onClick={async () => {
                            await api.llmProfiles.setDefault(p.id)
                            invalidate(keys.llmProfiles)
                            invalidate(keys.meta)
                          }}
                        >
                          既定に
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>

        {Object.entries(tests).some(([, t]) => !t.ok) && (
          <div className="notice" style={{ marginTop: 12 }}>
            {Object.entries(tests)
              .filter(([, t]) => !t.ok)
              .map(([id, t]) => `${(profiles.data ?? []).find((p) => p.id === id)?.name}: ${t.error}`)
              .join(' / ')}
          </div>
        )}

        {editing && (
          <ProfileDialog
            profile={editing === 'new' ? null : (profiles.data ?? []).find((p) => p.id === editing) ?? null}
            onClose={() => setEditing(null)}
            onError={setError}
          />
        )}
      </div>
    </>
  )
}

function ProfileDialog({
  profile, onClose, onError,
}: {
  profile: LlmProfile | null
  onClose: () => void
  onError: (msg: string) => void
}) {
  const invalidate = useInvalidate()
  const [form, setForm] = useState<ProfileForm>({ ...EMPTY_FORM })
  const [probe, setProbe] = useState<ProfileTestResult | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!profile) {
      setForm({ ...EMPTY_FORM })
      return
    }
    setForm({
      name: profile.name,
      provider: profile.provider,
      provider_kind: profile.provider_kind,
      base_url: profile.base_url,
      model: profile.model,
      api_key: '',                       // 既存の鍵は取得できない。空から始める
      max_output_tokens: profile.max_output_tokens || '3000',
      disable_thinking: profile.disable_thinking,
    })
  }, [profile?.id])                                   // eslint-disable-line react-hooks/exhaustive-deps

  const needsBaseUrl = !(form.provider === 'api' && form.provider_kind === 'anthropic')
  const canSave = form.name.trim() && form.model.trim() && (!needsBaseUrl || form.base_url.trim())

  async function save() {
    setBusy(true)
    try {
      if (profile) {
        await api.llmProfiles.update(profile.id, { ...form })
      } else {
        await api.llmProfiles.create({ ...form })
      }
      invalidate(keys.llmProfiles)
      invalidate(keys.meta)
      onClose()
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="launcher-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="dialog" role="dialog" aria-modal="true" style={{ width: 560, background: 'var(--color-bg)', boxShadow: 'var(--shadow-lg)' }}>
        <div style={{ padding: '14px 16px', borderBottom: '2px solid var(--color-divider)' }}>
          <Kicker>{profile ? 'モデルを編集' : 'モデルを追加'}</Kicker>
        </div>
        <div style={{ padding: 16, maxHeight: '60vh', overflow: 'auto' }}>
          <div className="form-grid">
            <div className="field">
              <label htmlFor="p-name">プロファイル名</label>
              <input
                id="p-name" className="input" value={form.name}
                placeholder="例: Gemma 4 12B（ローカル）"
                onChange={(e) => setForm({ ...form, name: e.target.value })}
              />
            </div>

            <div className="field">
              <label>接続先</label>
              <div className="seg">
                {(['local', 'api'] as const).map((v) => (
                  <button
                    key={v}
                    className={`seg-opt${form.provider === v ? ' on' : ''}`}
                    onClick={() => setForm({ ...form, provider: v })}
                  >
                    {v === 'local' ? '🖥️ ローカルLLM' : '☁️ API'}
                  </button>
                ))}
              </div>
            </div>

            {form.provider === 'api' && (
              <div className="field">
                <label>プロバイダ種別</label>
                <div className="seg">
                  {(['openai_compatible', 'anthropic'] as const).map((v) => (
                    <button
                      key={v}
                      className={`seg-opt${form.provider_kind === v ? ' on' : ''}`}
                      onClick={() => setForm({ ...form, provider_kind: v })}
                    >
                      {v === 'openai_compatible' ? 'OpenAI互換' : 'Anthropic'}
                    </button>
                  ))}
                </div>
                <div className="hint">
                  AnthropicはOpenAI互換レイヤーが公式に本番非推奨とされているため、
                  ネイティブライブラリ（langchain-anthropic）で接続します。
                </div>
              </div>
            )}

            {needsBaseUrl && (
              <div className="field">
                <label htmlFor="p-base">Base URL</label>
                <input
                  id="p-base" className="input mono" value={form.base_url}
                  placeholder={form.provider === 'local' ? 'http://10.0.2.2:1234/v1' : 'https://api.openai.com/v1'}
                  onChange={(e) => setForm({ ...form, base_url: e.target.value })}
                />
              </div>
            )}

            <div className="field">
              <label htmlFor="p-model">モデル名</label>
              <input
                id="p-model" className="input mono" value={form.model}
                placeholder={form.provider === 'local' ? 'gemma-4-12b-qat' : 'claude-sonnet-5'}
                onChange={(e) => setForm({ ...form, model: e.target.value })}
              />
            </div>

            <div className="field">
              <label htmlFor="p-key">API Key</label>
              <input
                id="p-key" className="input" type="password" value={form.api_key}
                placeholder={profile?.has_api_key ? '登録済み（変更する場合のみ入力）' : ''}
                onChange={(e) => setForm({ ...form, api_key: e.target.value })}
              />
              <div className="hint">
                保存済みの鍵は取得できません（サーバが返しません）。
                空のまま保存すれば既存の鍵はそのまま残ります。
              </div>
            </div>

            <div className="field">
              <label htmlFor="p-tok">最大出力トークン数</label>
              <div className="row">
                <input
                  id="p-tok" className="input mono" style={{ width: 140 }}
                  value={form.max_output_tokens}
                  onChange={(e) => setForm({ ...form, max_output_tokens: e.target.value })}
                />
                {profile && (
                  <button
                    className="btn btn-secondary btn-sm"
                    onClick={async () => setProbe(await api.llmProfiles.test(profile.id))}
                  >
                    <Plug size={12} style={{ marginRight: 5 }} />接続テスト
                  </button>
                )}
              </div>
              {probe && (
                <div className="hint">
                  {probe.ok ? `接続成功（${probe.latency_ms}ms）` : `接続失敗: ${probe.error}`}
                  {probe.max_tokens ? ` / 自動取得: ${probe.max_tokens}` : ' / 上限は自動取得できませんでした'}
                </div>
              )}
              {Number(form.max_output_tokens) < 2500 && (
                <div className="notice" style={{ marginTop: 6 }}>
                  ⚠️ 2500未満だと、Thinking機能を持つモデル（Gemma 4等）が内部思考だけで
                  上限を使い切り、実際の回答が空になることがあります。3000〜4000程度を推奨します。
                </div>
              )}
            </div>

            <label className="row" style={{ gap: 6, fontSize: 13 }}>
              <input
                type="checkbox" checked={form.disable_thinking}
                onChange={(e) => setForm({ ...form, disable_thinking: e.target.checked })}
              />
              Thinking を無効化する（推奨・高速）
            </label>
          </div>
        </div>
        <div className="row" style={{ padding: 16, borderTop: '2px solid var(--color-divider)' }}>
          {profile && (
            <button
              className="btn btn-ghost btn-sm"
              onClick={async () => {
                try {
                  await api.llmProfiles.remove(profile.id)
                  invalidate(keys.llmProfiles)
                  onClose()
                } catch (e) {
                  onError(e instanceof Error ? e.message : String(e))
                }
              }}
            >
              <Trash2 size={12} style={{ marginRight: 5 }} />削除
            </button>
          )}
          <div style={{ flex: 1 }} />
          <button className="btn btn-secondary" onClick={onClose}>キャンセル</button>
          <button className="btn btn-primary" onClick={() => void save()} disabled={!canSave || busy}>
            保存
          </button>
        </div>
      </div>
    </div>
  )
}

/* ===== 生成の予算（1h） ===== */

function BudgetSection() {
  const profiles = useLlmProfiles()
  return (
    <>
      <header className="page-header">
        <div>
          <div className="kicker">Budget</div>
          <h2 className="page-title">生成の予算</h2>
        </div>
      </header>
      <div className="page-body">
        <div className="hint" style={{ marginBottom: 16 }}>
          最大出力トークン数と Thinking の設定は、モデルごとに持ちます。
          変更は「モデル」から各プロファイルを編集してください。
        </div>
        {(profiles.data ?? []).map((p) => {
          const value = Number(p.max_output_tokens || 0)
          const danger = 2500
          const ceiling = Math.max(value, 8192)
          return (
            <div key={p.id} style={{ marginBottom: 20 }}>
              <div className="row" style={{ justifyContent: 'space-between' }}>
                <span className="cell-name">{p.name}</span>
                <span className="mono" style={{ fontSize: 12 }}>
                  {value.toLocaleString()} / ctx {ceiling.toLocaleString()}
                </span>
              </div>
              <div className="gauge" style={{ marginTop: 6 }}>
                <span style={{ width: `${Math.min(100, (value / ceiling) * 100)}%` }} />
                <b style={{ left: `${Math.min(100, (danger / ceiling) * 100)}%` }} />
              </div>
              {value < danger && (
                <div className="notice" style={{ marginTop: 6 }}>
                  2500 未満です。内部思考だけで上限を使い切り、回答が空になることがあります。
                </div>
              )}
            </div>
          )
        })}
      </div>
    </>
  )
}

/* ===== 検索（1h） ===== */

const PROVIDERS = [
  { key: 'duckduckgo', label: 'DuckDuckGo（APIキー不要）', fields: [] as string[] },
  { key: 'tavily', label: 'Tavily', fields: ['tavily_api_key'] },
  { key: 'google', label: 'Google CSE', fields: ['google_api_key', 'google_cse_id'] },
  { key: 'brave', label: 'Brave', fields: ['brave_api_key'] },
]

function SearchSection() {
  const settings = useSettings()
  const invalidate = useInvalidate()
  const [provider, setProvider] = useState('')
  const [values, setValues] = useState<Record<string, string>>({})
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    if (settings.data) setProvider(settings.data.search_provider ?? 'duckduckgo')
  }, [settings.data])

  const current = PROVIDERS.find((p) => p.key === provider)

  async function save() {
    await api.settings.put({ search_provider: provider, ...values })
    invalidate(keys.settings)
    setValues({})
    setSaved(true)
    window.setTimeout(() => setSaved(false), 2000)
  }

  return (
    <>
      <header className="page-header">
        <div>
          <div className="kicker">Search</div>
          <h2 className="page-title">検索</h2>
        </div>
        <div className="page-header-actions">
          {saved && <Tag kind="neutral">保存しました</Tag>}
          <button className="btn btn-primary" onClick={() => void save()}>💾 保存</button>
        </div>
      </header>
      <div className="page-body">
        <div className="form-grid">
          <div className="field">
            <label htmlFor="sp">検索プロバイダ</label>
            <select id="sp" className="input" value={provider} onChange={(e) => setProvider(e.target.value)}>
              {PROVIDERS.map((p) => (
                <option key={p.key} value={p.key}>{p.label}</option>
              ))}
            </select>
          </div>
          {provider === 'google' && (
            <div className="notice">
              Google CSE は 2026年時点で新規受付を終了しています。既存のキーがある場合のみ使えます。
            </div>
          )}
          {(current?.fields ?? []).map((f) => (
            <div className="field" key={f}>
              <label htmlFor={f}>{f}</label>
              <input
                id={f} className="input" type="password"
                placeholder={settings.data?.[f] ? '登録済み（変更する場合のみ入力）' : ''}
                value={values[f] ?? ''}
                onChange={(e) => setValues({ ...values, [f]: e.target.value })}
              />
            </div>
          ))}
        </div>
      </div>
    </>
  )
}

/* ===== サンドボックス（1h） ===== */

function SandboxSection() {
  const settings = useSettings()
  const invalidate = useInvalidate()
  const [mounts, setMounts] = useState('')
  const [saved, setSaved] = useState(false)
  const [checked, setChecked] = useState<MountCheck[]>([])

  useEffect(() => {
    if (settings.data) setMounts(settings.data.sandbox_extra_mounts ?? '')
  }, [settings.data])

  // 入力のたびにサーバへ下見を投げる。パスの解決（realpath）は
  // サーバ側でしかできないので、画面で判定しない
  useEffect(() => {
    let cancelled = false
    const timer = window.setTimeout(async () => {
      try {
        const result = await api.settings.checkMounts(mounts)
        if (!cancelled) setChecked(result)
      } catch {
        if (!cancelled) setChecked([])
      }
    }, 300)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [mounts])

  return (
    <>
      <header className="page-header">
        <div>
          <div className="kicker">Sandbox</div>
          <h2 className="page-title">サンドボックス</h2>
        </div>
        <div className="page-header-actions">
          {saved && <Tag kind="neutral">保存しました</Tag>}
          <button
            className="btn btn-primary"
            onClick={async () => {
              await api.settings.put({ sandbox_extra_mounts: mounts })
              invalidate(keys.settings)
              setSaved(true)
              window.setTimeout(() => setSaved(false), 2000)
            }}
          >
            💾 保存
          </button>
        </div>
      </header>
      <div className="page-body">
        <div className="form-grid">
          <div className="field">
            <label htmlFor="mounts">追加マウント</label>
            <textarea
              id="mounts" className="input" rows={6} value={mounts}
              placeholder="host:container:ro"
              onChange={(e) => setMounts(e.target.value)}
            />
            <div className="hint">
              1行1マウント（host:container:ro|rw）。全実行がコンテナ内で行われるため、
              作業ディレクトリ以外のホストファイルに触るツールはここで指定します。
            </div>
          </div>
          {checked.length > 0 && (
            <div>
              <Kicker>この設定でどうなるか</Kicker>
              <table className="grid" style={{ marginTop: 6 }}>
                <thead>
                  <tr><th>ホスト側</th><th>コンテナ側</th><th>権限</th><th>結果</th></tr>
                </thead>
                <tbody>
                  {checked.map((m, i) => (
                    <tr key={i}>
                      <td className="mono" style={{ fontSize: 11.5 }}>{m.host}</td>
                      <td className="mono" style={{ fontSize: 11.5 }}>{m.container}</td>
                      <td className="mono cell-nowrap">{m.mode}</td>
                      <td>
                        {m.rejected
                          ? <Tag kind="accent">却下</Tag>
                          : <Tag kind="neutral">マウントする</Tag>}
                        {m.rejected && <div className="cell-sub">{m.rejected}</div>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <div className="notice">
            設定DBが見える指定は自動で却下します。DBにはLLMプロバイダの
            APIキーが平文で入っており、コンテナの中で動くのは AI が生成した
            コードだからです。親ディレクトリを指定した場合も同じく落とします。
          </div>
        </div>
      </div>
    </>
  )
}

export { Empty }
