// src/api/hooks.ts — TanStack Query のラッパ。
//
// 楽観更新は使わない（実行系は副作用が重い）。実行中の追従は SSE で行い、
// ポーリングはしない。SSE の status が変わったところで invalidate する。

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from './client'
import type { AgentTask, Tool } from './types'

export const keys = {
  meta: ['meta'] as const,
  tools: ['tools'] as const,
  agentTasks: ['agentTasks'] as const,
  executions: ['executions'] as const,
  execution: (id: string) => ['execution', id] as const,
  schedules: ['schedules'] as const,
  llmProfiles: ['llmProfiles'] as const,
  settings: ['settings'] as const,
}

export const useMeta = () => useQuery({ queryKey: keys.meta, queryFn: api.meta, staleTime: 60_000 })
export const useTools = () => useQuery({ queryKey: keys.tools, queryFn: api.tools.list })
export const useAgentTasks = () => useQuery({ queryKey: keys.agentTasks, queryFn: api.agentTasks.list })
export const useExecutions = (limit = 30) =>
  useQuery({ queryKey: [...keys.executions, limit], queryFn: () => api.executions.list(limit) })
export const useExecution = (id: string | undefined) =>
  useQuery({ queryKey: keys.execution(id ?? ''), queryFn: () => api.executions.get(id!), enabled: !!id })
export const useSchedules = () => useQuery({ queryKey: keys.schedules, queryFn: api.schedules.list })
export const useLlmProfiles = () => useQuery({ queryKey: keys.llmProfiles, queryFn: api.llmProfiles.list })
export const useSettings = () => useQuery({ queryKey: keys.settings, queryFn: api.settings.get })

/** 一覧に出す「タスク」— ツールとエージェントを1本にまとめたもの（5.1）。 */
export interface UnifiedTask {
  id: string
  kind: 'tool' | 'agent'
  name: string
  description: string
  status: string
  graph_kind: string | null
  llm_profile_id: string | null
  updated_at: string
  raw: Tool | AgentTask
}

export function unify(tools: Tool[] | undefined, tasks: AgentTask[] | undefined): UnifiedTask[] {
  const out: UnifiedTask[] = []
  for (const t of tools ?? []) {
    out.push({
      id: t.id, kind: 'tool', name: t.name, description: t.description,
      status: t.status, graph_kind: null, llm_profile_id: null,
      updated_at: t.updated_at, raw: t,
    })
  }
  for (const t of tasks ?? []) {
    out.push({
      id: t.id, kind: 'agent', name: t.name, description: t.description,
      status: 'agent', graph_kind: t.graph_kind, llm_profile_id: t.llm_profile_id,
      updated_at: t.updated_at, raw: t,
    })
  }
  return out.sort((a, b) => (a.updated_at < b.updated_at ? 1 : -1))
}

export function useInvalidate() {
  const qc = useQueryClient()
  return (key: readonly unknown[]) => qc.invalidateQueries({ queryKey: key })
}

export function useRunAgent() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, graphKind, profileId }: { id: string; graphKind?: string | null; profileId?: string | null }) =>
      api.agentTasks.run(id, { graph_kind: graphKind ?? null, llm_profile_id: profileId ?? null }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: keys.executions })
    },
  })
}

export function useRunTool() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => api.tools.run(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.executions }),
  })
}
