import type { Meta, Outcome, StreamEvent } from './types'

// fetch 的 POST + SSE 解析：EventSource 不支持 POST，按规范手拆 \n\n 分帧
export async function streamAsk(question: string, onEvent: (event: StreamEvent) => void): Promise<void> {
  const resp = await fetch('/api/ask', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question }),
  })
  if (resp.status === 429) {
    const body = await resp.json().catch(() => null)
    throw new Error(body?.detail ?? '演示服务繁忙，请稍后再试')
  }
  if (resp.status === 422) throw new Error('问题不合法：1–200 个字符且不能是空白')
  if (!resp.ok || !resp.body) {
    throw new Error(`服务异常（HTTP ${resp.status}），请稍后再试`)
  }
  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let sep: number
    while ((sep = buffer.indexOf('\n\n')) >= 0) {
      const frame = buffer.slice(0, sep)
      buffer = buffer.slice(sep + 2)
      const parsed = parseFrame(frame)
      if (parsed) onEvent(parsed)
    }
  }
  const tail = parseFrame(buffer)
  if (tail) onEvent(tail)
}

function parseFrame(frame: string): StreamEvent | null {
  let event = ''
  const dataLines: string[] = []
  for (const line of frame.split('\n')) {
    if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart())
  }
  if (!event || dataLines.length === 0) return null
  let payload: unknown
  try {
    payload = JSON.parse(dataLines.join('\n'))
  } catch {
    return null
  }
  if (event === 'stage') {
    const p = payload as { name?: string; elapsed_ms?: number }
    return { kind: 'stage', name: p.name ?? 'unknown', elapsed_ms: p.elapsed_ms ?? 0 }
  }
  if (event === 'result') return { kind: 'result', outcome: payload as Outcome }
  if (event === 'error') {
    const p = payload as { message?: string }
    return { kind: 'error', message: p.message ?? '服务内部错误，请联系演示负责人' }
  }
  return null
}

export async function fetchMeta(): Promise<Meta> {
  const resp = await fetch('/api/meta')
  if (!resp.ok) throw new Error(`meta 加载失败（HTTP ${resp.status}）`)
  return (await resp.json()) as Meta
}
