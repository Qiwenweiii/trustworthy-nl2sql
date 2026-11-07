import type { Outcome } from '../api/types'

function formatCell(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'number') return Number.isInteger(value) ? value.toLocaleString('zh-CN') : value.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
  return String(value)
}

// AnswerCard：结论取首行首列；空结果/截断/口径声明都用管线写的原文，前端不另编（§4.2）
export function AnswerCard({ outcome }: { outcome: Outcome }) {
  const result = outcome.result
  if (!result) return null
  const headline = result.rows.length > 0 ? formatCell(result.rows[0][result.columns[0]]) : null
  return (
    <div className="card answer">
      {result.empty_result ? (
        <p className="empty-notice">{outcome.llm_response?.split('\n').at(-1) ?? '当前条件下没有数据'}</p>
      ) : (
        <p className="headline">{headline}</p>
      )}
      {result.row_count > 1 && (
        <table className="result-table">
          <thead>
            <tr>
              {result.columns.map((c) => (
                <th key={c}>{c}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {result.rows.map((row, i) => (
              <tr key={i}>
                {result.columns.map((c) => (
                  <td key={c}>{formatCell(row[c])}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {result.truncated && <p className="badge truncate">结果超过 {result.max_rows} 行，已按上限截断</p>}
      {outcome.caliber_note && <p className="caliber">口径声明：{outcome.caliber_note}</p>}
      <p className="muted">
        {result.row_count} 行 · 端到端 {(outcome.elapsed_ms / 1000).toFixed(1)}s
      </p>
    </div>
  )
}
