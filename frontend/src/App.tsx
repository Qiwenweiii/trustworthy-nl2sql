import { useEffect, useRef, useState } from 'react'
import { fetchMeta, streamAsk } from './api/client'
import type { Meta, StreamEvent } from './api/types'
import { ChatThread } from './components/ChatThread'
import type { Turn } from './components/ChatThread'
import { QuickChips } from './components/QuickChips'
import './App.css'

export default function App() {
  const [meta, setMeta] = useState<Meta | null>(null)
  const [turns, setTurns] = useState<Turn[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    fetchMeta().then(setMeta).catch(() => setMeta(null))
  }, [])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [turns])

  const ask = async (question: string) => {
    const trimmed = question.trim()
    if (!trimmed || busy) return
    setBusy(true)
    setInput('')
    setTurns((prev) => [...prev, { role: 'user', question: trimmed }, { role: 'waiting', stages: [] }])
    const stages: Extract<StreamEvent, { kind: 'stage' }>[] = []
    const replaceLast = (turn: Turn) => setTurns((prev) => [...prev.slice(0, -1), turn])
    try {
      await streamAsk(trimmed, (event) => {
        if (event.kind === 'stage') {
          stages.push(event)
          replaceLast({ role: 'waiting', stages: [...stages] })
        } else if (event.kind === 'result') {
          replaceLast({ role: 'outcome', outcome: event.outcome })
        } else {
          replaceLast({ role: 'notice', message: event.message })
        }
      })
    } catch (err) {
      replaceLast({ role: 'notice', message: err instanceof Error ? err.message : String(err) })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="app">
      <header>
        <h1>DataAgent 问数演示</h1>
        <p className="sub">自然语言 → 可信 SQL → 带证据的答案 · 等待时直播的步骤，就是最后证据面板里记的账</p>
      </header>
      {turns.length === 0 && meta && <QuickChips questions={meta.sample_questions} onPick={setInput} />}
      <ChatThread turns={turns} meta={meta} onBackfill={setInput} />
      <div ref={bottomRef} />
      <form
        className="composer"
        onSubmit={(e) => {
          e.preventDefault()
          void ask(input)
        }}
      >
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder={meta ? '例如：上个月各渠道销售额是多少' : '先启动后端服务（看 /api/health 自检）'}
          maxLength={200}
          disabled={busy}
        />
        <button type="submit" disabled={busy || !input.trim()}>
          {busy ? '查询中…' : '提问'}
        </button>
      </form>
    </div>
  )
}
