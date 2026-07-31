// src/lib/cost.ts — 所要時間とトークンの整形・集計。
//
// バックエンドの metrics.py と同じ方針で書く。**取れていない値を
// 推定しない**のが要点。usage を返さないプロバイダがあり、そこで
// 文字数から推定すると根拠のない数字が「計測値」として並ぶ。
// 欠測は欠測のまま「未報告」と出す。

import type { RunMetrics, TraceEntry } from '../api/types'

export function fmtMs(ms: number | undefined | null): string {
  if (typeof ms !== 'number' || ms < 0) return '—'
  if (ms < 1000) return `${Math.round(ms)}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`
  return `${Math.floor(ms / 60_000)}m${Math.round((ms % 60_000) / 1000)}s`
}

export function fmtTokens(n: number | undefined | null): string {
  if (typeof n !== 'number') return '—'
  return n.toLocaleString()
}

/** そのノード・その実行で、トークンが1つでも報告されたか。 */
export function hasTokens(m: { input_tokens?: number; output_tokens?: number }): boolean {
  return Boolean(m.input_tokens || m.output_tokens)
}

/** 計測が付いている trace 行だけ。古い実行は elapsed_ms を持たない。 */
export function measured(trace: TraceEntry[]): TraceEntry[] {
  return trace.filter((t) => typeof t.elapsed_ms === 'number')
}

/**
 * trace から合計を出す。
 *
 * 実行中は SSE で trace が届くので、保存済みの metrics 列を待たずに
 * 画面で合計を出せる。終了後は exec.metrics と一致する。
 */
export function totalsOf(trace: TraceEntry[]): RunMetrics {
  const rows = measured(trace)
  const sum = (k: keyof TraceEntry) =>
    rows.reduce((a, t) => a + ((t[k] as number) || 0), 0)
  return {
    nodes: rows.length,
    elapsed_ms: sum('elapsed_ms'),
    llm_ms: sum('llm_ms'),
    llm_calls: sum('llm_calls'),
    input_tokens: sum('input_tokens'),
    output_tokens: sum('output_tokens'),
    reasoning_tokens: sum('reasoning_tokens'),
    missing_usage: sum('missing_usage'),
  }
}

/** LLM待ちが全体の何割か。0除算を避ける。 */
export function llmShare(m: RunMetrics): number | null {
  if (!m.elapsed_ms) return null
  return Math.round(((m.llm_ms || 0) / m.elapsed_ms) * 100)
}

export interface NodeCost {
  node: string
  count: number
  elapsed_ms: number
  llm_ms: number
  llm_calls: number
  input_tokens: number
  output_tokens: number
  missing_usage: number
  share: number
}

/**
 * ノード名でまとめる。react は何周もするので、1行ずつ見ても
 * 「どこに時間を使ったか」が分からない。所要の降順に並べる。
 */
export function byNode(trace: TraceEntry[]): NodeCost[] {
  const acc = new Map<string, NodeCost>()
  for (const t of measured(trace)) {
    const key = t.node || '?'
    const cur = acc.get(key) ?? {
      node: key, count: 0, elapsed_ms: 0, llm_ms: 0, llm_calls: 0,
      input_tokens: 0, output_tokens: 0, missing_usage: 0, share: 0,
    }
    cur.count += 1
    cur.elapsed_ms += t.elapsed_ms || 0
    cur.llm_ms += t.llm_ms || 0
    cur.llm_calls += t.llm_calls || 0
    cur.input_tokens += t.input_tokens || 0
    cur.output_tokens += t.output_tokens || 0
    cur.missing_usage += t.missing_usage || 0
    acc.set(key, cur)
  }
  const rows = [...acc.values()]
  const total = rows.reduce((a, r) => a + r.elapsed_ms, 0) || 1
  for (const r of rows) r.share = Math.round((r.elapsed_ms / total) * 100)
  return rows.sort((a, b) => b.elapsed_ms - a.elapsed_ms)
}
