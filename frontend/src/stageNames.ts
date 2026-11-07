// 阶段直播文案：label 与管线 trace 记录点一一对应（tests/test_pipeline_tracer.py 锁同源）
export type StageLabel =
  | 'question'
  | 'refuse'
  | 'schema'
  | 'sql'
  | 'metric'
  | 'time'
  | 'guard'
  | 'exec'
  | 'retry'
  | 'outcome'
  | 'llm_error'

const STAGE_TEXT: Record<StageLabel, string> = {
  question: '正在理解问题…',
  refuse: '核对语义登记',
  schema: '读取表结构',
  sql: '生成 SQL',
  metric: '核对并补写口径条件',
  time: '校验时间边界',
  guard: '静态安检',
  exec: '只读执行查询',
  retry: '首版 SQL 执行失败，已送回修复',
  outcome: '整理结果',
  llm_error: '模型调用出错',
}

export function stageText(label: string): string {
  return STAGE_TEXT[label as StageLabel] ?? label
}

export const isRetryStage = (label: string) => label === 'retry'
