import type { ReactNode } from 'react'
import type { EnforcementAction, Outcome, TimeFixAction } from '../api/types'

// EvidencePanel：折叠的证据账（§4.2）。直播说的是同一步的现在进行时，
// 这里列的是完成时——from→to 对照渲染系统改写，不另编账。
export function EvidencePanel({ outcome }: { outcome: Outcome }) {
  const actions: ReactNode[] = []

  const pushDiff = (prefix: string, action: { from?: string; to?: string; detail?: string }, key: string) => {
    if (action.from && action.to) {
      actions.push(
        <p className="diff-line" key={key}>
          <span className="tag">{prefix}</span>
          <s className="from">{action.from}</s>
          <span className="arrow">→</span>
          <mark className="to">{action.to}</mark>
        </p>,
      )
    } else if (action.to) {
      actions.push(
        <p className="diff-line" key={key}>
          <span className="tag">{prefix}</span>
          <mark className="to">{action.to}</mark>
        </p>,
      )
    } else {
      actions.push(
        <p key={key}>
          <span className="tag">{prefix}</span>
          {action.detail ?? '无动作明细'}
        </p>,
      )
    }
  }

  outcome.enforcement?.actions.forEach((a: EnforcementAction, i) => {
    if (a.type === 'already_present') return
    pushDiff(a.type === 'added' ? '补写口径' : a.type === 'conflict_replaced' ? '冲突改写' : '未注入', a, `e${i}`)
  })
  outcome.time_fix?.actions.forEach((a: TimeFixAction, i) => {
    if (a.type === 'already_correct') return
    pushDiff(a.type === 'boundary_rewritten' ? '边界修正' : '未修正', { from: a.from, to: a.to, detail: a.detail }, `t${i}`)
  })

  const retry = outcome.retry
  const violations = outcome.guard?.violations ?? []
  const blocked = outcome.status === 'blocked'

  return (
    <details className="evidence">
      <summary>{blocked ? '拦截现场' : outcome.status === 'no_sql' ? '拒答现场（未生成 SQL，未调模型）' : '这次回答的证据'}</summary>
      {outcome.sql && <pre className={`sql ${blocked ? 'blocked-sql' : ''}`}>{outcome.sql}</pre>}
      {violations.length > 0 && (
        <ul className="violations">
          {violations.map((v, i) => (
            <li key={i}>
              <code>{v.rule}</code>：{v.detail}
            </li>
          ))}
        </ul>
      )}
      {actions.length > 0 && (
        <div className="actions">
          <h4>系统改写</h4>
          {actions}
        </div>
      )}
      {retry && (
        <div className="retry">
          <h4>报错回炉</h4>
          <p className="diff-line">
            <span className="tag">闯祸 SQL</span>
            <code>{retry.failed_sql}</code>
          </p>
          <p className="diff-line">
            <span className="tag">数据库报错</span>
            {retry.db_error}
          </p>
          {retry.regenerated_sql && (
            <p className="diff-line">
              <span className="tag">修复后</span>
              <mark className="to">{retry.regenerated_sql}</mark>
            </p>
          )}
          <p className="muted">回炉结果：{retry.result}</p>
        </div>
      )}
      {outcome.caliber_note && (
        <p className="caliber">
          <span className="tag">口径声明</span>
          {outcome.caliber_note}
        </p>
      )}
    </details>
  )
}
