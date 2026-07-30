// src/api/client.ts — HTTP と SSE の薄いラッパ。
//
// ここには表示の都合を持ち込まない。サーバが返した形をそのまま渡す。

import type {
  AgentTask, Execution, LlmProfile, Meta, ProfileTestResult, RunEvent,
  Schedule, Tool,
} from './types'

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message)
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  })
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      detail = body?.detail ?? detail
    } catch {
      /* 本文が JSON でないことがある（500 等）。statusText のままにする */
    }
    throw new ApiError(res.status, typeof detail === 'string' ? detail : JSON.stringify(detail))
  }
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

const get = <T,>(p: string) => request<T>(p)
const post = <T,>(p: string, body?: unknown) =>
  request<T>(p, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) })
const patch = <T,>(p: string, body: unknown) =>
  request<T>(p, { method: 'PATCH', body: JSON.stringify(body) })
const put = <T,>(p: string, body: unknown) =>
  request<T>(p, { method: 'PUT', body: JSON.stringify(body) })
const del = (p: string) => request<void>(p, { method: 'DELETE' })

export const api = {
  meta: () => get<Meta>('/meta'),

  tools: {
    list: () => get<Tool[]>('/tools'),
    get: (id: string) => get<Tool>(`/tools/${id}`),
    create: (body: Partial<Tool>) => post<{ id: string }>('/tools', body),
    update: (id: string, body: Partial<Tool>) => patch<Tool>(`/tools/${id}`, body),
    remove: (id: string) => del(`/tools/${id}`),
    run: (id: string) => post<{ exec_id: string; status: string; stdout: string; stderr: string }>(`/tools/${id}/run`),
  },

  preview: (body: { code: string; network?: boolean; writable_workspace?: boolean }) =>
    post<{ status: string; stdout: string; stderr: string }>('/preview', body),

  agentTasks: {
    list: () => get<AgentTask[]>('/agent-tasks'),
    get: (id: string) => get<AgentTask>(`/agent-tasks/${id}`),
    create: (body: Partial<AgentTask>) => post<{ id: string }>('/agent-tasks', body),
    update: (id: string, body: Partial<AgentTask>) => patch<AgentTask>(`/agent-tasks/${id}`, body),
    remove: (id: string) => del(`/agent-tasks/${id}`),
    run: (id: string, body?: { graph_kind?: string | null; llm_profile_id?: string | null }) =>
      post<{ exec_id: string }>(`/agent-tasks/${id}/run`, body ?? {}),
  },

  executions: {
    list: (limit = 30) => get<Execution[]>(`/executions?limit=${limit}`),
    get: (id: string) => get<Execution>(`/executions/${id}`),
    cancel: (id: string) => post<{ cancelled: boolean; status: string }>(`/executions/${id}/cancel`),
  },

  schedules: {
    list: () => get<Schedule[]>('/schedules'),
    create: (body: { exec_type: string; target_id: string; cron_expr: string }) =>
      post<{ id: string }>('/schedules', body),
    toggle: (id: string, enabled: boolean) => patch<{ enabled: boolean }>(`/schedules/${id}`, { enabled }),
    remove: (id: string) => del(`/schedules/${id}`),
  },

  llmProfiles: {
    list: () => get<LlmProfile[]>('/llm-profiles'),
    create: (body: Record<string, unknown>) => post<LlmProfile>('/llm-profiles', body),
    update: (id: string, body: Record<string, unknown>) => patch<LlmProfile>(`/llm-profiles/${id}`, body),
    remove: (id: string) => del(`/llm-profiles/${id}`),
    test: (id: string) => post<ProfileTestResult>(`/llm-profiles/${id}/test`),
    setDefault: (id: string) => put<{ profile_id: string }>('/settings/default-profile', { profile_id: id }),
  },

  settings: {
    get: () => get<Record<string, string>>('/settings'),
    put: (values: Record<string, string>) => put<Record<string, string>>('/settings', { values }),
  },
}

/**
 * 実行の SSE を購読する。戻り値を呼ぶと切断する。
 *
 * EventSource を使うのは、再接続を実装しないため。ブラウザが勝手に
 * つなぎ直してくれるが、こちらは冪等（サーバは毎回、履歴を頭から流す）
 * なので二重表示にならない。イベントの取り込み側で seq / step を見て
 * 重複を潰す。
 */
export function subscribeRun(
  execId: string,
  onEvent: (e: RunEvent) => void,
  onOpen?: () => void,
): () => void {
  const source = new EventSource(`/api/executions/${execId}/stream`)

  // 接続が開くたびに呼ぶ。**再接続のたびにサーバは履歴を頭から流し直す**ので、
  // 受け側は毎回まっさらにしないと同じステップが積み上がる。
  // 実測で STEP 130 / 10 のような表示になった（再接続のたびに全履歴が
  // 追加されていた）。EventSource は自動で再接続するため、必ず起きる。
  source.onopen = () => onOpen?.()

  source.onmessage = (msg) => {
    try {
      onEvent(JSON.parse(msg.data) as RunEvent)
    } catch {
      /* 壊れた行は捨てる。落とすより表示を続ける方がよい */
    }
  }
  source.onerror = () => {
    // ブラウザが自動再接続する。閉じたい場合は呼び出し側が戻り値を呼ぶ
  }
  return () => source.close()
}

/**
 * POST の SSE（ツール生成・修正）を読む。
 * EventSource は GET しか出せないので fetch + ReadableStream で読む。
 */
export async function streamPost(
  path: string,
  body: unknown,
  onEvent: (e: Record<string, unknown>) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`/api${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  })
  if (!res.ok || !res.body) throw new ApiError(res.status, res.statusText)

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    // SSE は空行でイベント区切り
    const chunks = buffer.split('\n\n')
    buffer = chunks.pop() ?? ''
    for (const chunk of chunks) {
      for (const line of chunk.split('\n')) {
        if (!line.startsWith('data:')) continue
        try {
          onEvent(JSON.parse(line.slice(5).trim()))
        } catch {
          /* 壊れた行は捨てる */
        }
      }
    }
  }
}
