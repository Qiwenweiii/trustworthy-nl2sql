import type { Meta, Outcome, StreamEvent } from '../api/types'
import { AnswerCard } from './AnswerCard'
import { EvidencePanel } from './EvidencePanel'
import { RefusalCard } from './RefusalCard'
import { WaitingCard } from './WaitingCard'

export type Turn =
  | { role: 'user'; question: string }
  | { role: 'waiting'; stages: Extract<StreamEvent, { kind: 'stage' }>[] }
  | { role: 'outcome'; outcome: Outcome }
  | { role: 'notice'; message: string }

export function ChatThread({
  turns,
  meta,
  onBackfill,
}: {
  turns: Turn[]
  meta: Meta | null
  onBackfill: (text: string) => void
}) {
  return (
    <div className="thread">
      {turns.map((turn, i) => {
        if (turn.role === 'user') {
          return (
            <div key={i} className="bubble user">
              {turn.question}
            </div>
          )
        }
        if (turn.role === 'waiting') {
          return <WaitingCard key={i} stages={turn.stages} />
        }
        if (turn.role === 'notice') {
          return (
            <div key={i} className="card notice">
              {turn.message}
            </div>
          )
        }
        const { outcome } = turn
        if (outcome.status === 'ok') {
          return (
            <div key={i} className="assistant">
              <AnswerCard outcome={outcome} />
              <EvidencePanel outcome={outcome} />
            </div>
          )
        }
        if (outcome.error?.type === 'semantic_refusal') {
          return (
            <div key={i} className="assistant">
              <RefusalCard outcome={outcome} meta={meta} onBackfill={onBackfill} />
            </div>
          )
        }
        return (
          <div key={i} className="card fallback">
            <p className="badge">{statusTitle(outcome.status)}</p>
            <p>{outcome.error?.message ?? outcome.llm_response ?? '没有更多信息'}</p>
            <EvidencePanel outcome={outcome} />
          </div>
        )
      })}
    </div>
  )
}

function statusTitle(status: Outcome['status']): string {
  if (status === 'blocked') return '静态安检拦截'
  if (status === 'exec_error') return '数据库执行报错'
  if (status === 'timeout') return '执行超时'
  if (status === 'llm_error') return '模型调用失败'
  return '没有可用的 SQL'
}
