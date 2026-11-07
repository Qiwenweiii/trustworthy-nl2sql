// outcome 字段字典的前端镜像（README「pipeline.ask() 返回」小节，一字不多一字不少）
export type Status = 'ok' | 'blocked' | 'no_sql' | 'exec_error' | 'timeout' | 'llm_error'

export interface Violation {
  rule: string
  detail: string
  token?: string
}

export interface EnforcementAction {
  type: 'added' | 'conflict_replaced' | 'already_present' | 'skipped'
  detail?: string
  from?: string
  to?: string
}

export interface TimeFixAction {
  type: 'boundary_rewritten' | 'already_correct' | 'skipped'
  detail?: string
  from?: string
  to?: string
}

export interface Outcome {
  question: string
  status: Status
  sql: string | null
  guard: { passed: boolean; violations: Violation[] } | null
  enforcement: {
    metric: string | null
    required_filters?: unknown[]
    changed: boolean
    actions: EnforcementAction[]
  } | null
  time_fix: { changed: boolean; actions: TimeFixAction[] } | null
  result: {
    columns: string[]
    rows: Record<string, unknown>[]
    row_count: number
    empty_result: boolean
    truncated: boolean
    max_rows: number
    elapsed_ms: number
  } | null
  error: { type: string; message: string } | null
  llm_response: string | null
  caliber_note: string | null
  retry: {
    attempted: boolean
    failed_sql: string
    db_error: string
    regenerated_sql: string | null
    result: string
  } | null
  elapsed_ms: number
}

export interface MetaDimension {
  name: string
  aliases: string[]
  column: string | null
  enum?: string[]
}

export interface Meta {
  metrics: { name: string; synonyms: string[] }[]
  dimensions: MetaDimension[]
  sample_questions: string[]
}

export type StreamEvent =
  | { kind: 'stage'; name: string; elapsed_ms: number }
  | { kind: 'result'; outcome: Outcome }
  | { kind: 'error'; message: string }
