import { useEffect, useState } from 'react'
import { stageText } from '../stageNames'
import type { StreamEvent } from '../api/types'

// 分档等待体验（WEB_DEMO_PLAN §4.3）：0–5s 骨架屏通用文案；之后只播真实阶段，不谎称步骤
export function WaitingCard({ stages }: { stages: Extract<StreamEvent, { kind: 'stage' }>[] }) {
  const [seconds, setSeconds] = useState(0)
  useEffect(() => {
    const timer = setInterval(() => setSeconds((s) => s + 1), 1000)
    return () => clearInterval(timer)
  }, [])
  const latest = stages[stages.length - 1]
  if (seconds < 5 || !latest) {
    return (
      <div className="card waiting">
        <div className="skeleton" />
        <p>正在理解问题…</p>
      </div>
    )
  }
  return (
    <div className={`card waiting ${latest.name === 'retry' ? 'retry' : ''}`}>
      <p className="stage-line">
        {stageText(latest.name)}
        <span className="stage-time">已 {latest.elapsed_ms.toFixed(0)} ms</span>
      </p>
      {latest.name === 'retry' && (
        <p className="hint">首版 SQL 执行失败，已送回给模型修复并重走全部安检，会多等一会。</p>
      )}
      <ul className="stage-log">
        {stages.map((s, i) => (
          <li key={i}>{stageText(s.name)}</li>
        ))}
      </ul>
    </div>
  )
}
