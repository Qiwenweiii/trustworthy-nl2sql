import { useState } from 'react'
import type { Meta, Outcome } from '../api/types'
import { classifyRefusal, guideButtons } from '../refusal'
import { EvidencePanel } from './EvidencePanel'

// RefusalCard：拒答不做红字报错，做成缺槽补全（§4.2）。
// 拒答路径零模型调用，这张卡必须秒出——它就是"拦在模型前"的现场证据。
export function RefusalCard({
  outcome,
  meta,
  onBackfill,
}: {
  outcome: Outcome
  meta: Meta | null
  onBackfill: (text: string) => void
}) {
  const [filledIndex, setFilledIndex] = useState<number | null>(null)
  const guide = classifyRefusal(outcome.error?.message ?? outcome.llm_response, meta ?? undefined)
  const buttons = meta ? guideButtons(guide, meta, outcome.question) : []
  return (
    <div className="card refusal">
      <p className="refuse-title">这个问题我答不了（没有调用模型，不瞎猜）</p>
      <p className="refuse-message">{outcome.error?.message ?? outcome.llm_response}</p>
      {buttons.length > 0 && (
        <div className="chips">
          {buttons.map((b, i) => (
            <button
              type="button"
              key={i}
              className={`chip ${filledIndex === i ? 'filled' : ''}`}
              onClick={() => {
                onBackfill(b.backfill)
                setFilledIndex(i)
              }}
            >
              {filledIndex === i ? `已填入：${b.label}` : b.label}
            </button>
          ))}
        </div>
      )}
      {filledIndex !== null && <p className="muted">已回填输入框，可继续编辑后提交。</p>}
      <EvidencePanel outcome={outcome} />
    </div>
  )
}
