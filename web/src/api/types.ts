// src/api/types.ts — サーバが返す形。
//
// API 側（api/schemas.py）と1対1で対応させる。とくに llm_profiles は
// api_key を持たない（has_api_key だけ）。ここに api_key を足さないこと。

export type ExecStatus = 'running' | 'done' | 'error'
export type ExecType = 'tool' | 'agent'
export type ToolStatus = 'draft' | 'verified'

export interface Tool {
  id: string
  name: string
  description: string
  code: string
  status: ToolStatus
  created_at: string
  updated_at: string
}

export interface AgentTask {
  id: string
  name: string
  description: string
  task_prompt: string
  allowed_tool_ids: string[]
  graph_kind: string | null
  llm_profile_id: string | null
  created_at: string
  updated_at: string
}

/** executor.parse_history_for_display と同じ形。フロントで再パースしない。 */
export interface Step {
  step: number
  role: 'assistant' | 'result'
  thought?: string
  action?: string
  done?: string
  text?: string
  result?: string
}

export interface TraceEntry {
  seq?: number
  node?: string
  from?: string
  next?: string
  summary?: string
  note?: string
  skipped?: boolean
}

export interface LlmInfo {
  id?: string
  name?: string
  provider?: string
  provider_kind?: string
  model?: string
  base_url?: string
  max_output_tokens?: string
  disable_thinking?: string
}

export interface Execution {
  id: string
  exec_type: ExecType
  target_id: string
  target_name: string
  trigger: string
  status: ExecStatus
  stdout: string | null
  stderr: string | null
  history: unknown[]
  trace: TraceEntry[]
  llm_info: LlmInfo
  graph_kind: string | null
  graph_label: string
  model_label: string
  started_at: string
  finished_at: string | null
  steps?: Step[]
  /** そのグラフのステップ上限。画面で決め打ちしない */
  max_steps: number
}

export interface Schedule {
  id: string
  exec_type: ExecType
  target_id: string
  target_name: string
  cron_expr: string
  enabled: number
  created_at: string
}

export interface LlmProfile {
  id: string
  name: string
  provider: 'local' | 'api'
  provider_kind: 'openai_compatible' | 'anthropic'
  base_url: string
  model: string
  max_output_tokens: string
  disable_thinking: boolean
  has_api_key: boolean
  created_at: string
  is_default?: boolean
}

export interface GraphKind {
  value: string
  label: string
  description: string
}

export interface Meta {
  provider_label: string
  default_profile: LlmProfile
  graph_kinds: GraphKind[]
  default_graph_kind: string
  search_provider: string
  podman_ok: boolean
  /** ready / stale / missing / user_managed / unavailable（sandbox.image_status） */
  sandbox_state: string
}

/** SSE で流れてくるイベント。api/events.py と対応。 */
export type RunEvent =
  | ({ type: 'step' } & Step)
  | ({ type: 'result' } & Step)
  | ({ type: 'trace' } & TraceEntry)
  | { type: 'status'; status: ExecStatus; graph_kind?: string | null; started_at?: string; finished_at?: string | null }
  | { type: 'final'; status: ExecStatus; stdout: string; stderr: string }
  | { type: 'error'; message: string }

export interface ProfileTestResult {
  ok: boolean
  max_tokens: number | null
  latency_ms: number
  sample?: string
  error?: string
}
