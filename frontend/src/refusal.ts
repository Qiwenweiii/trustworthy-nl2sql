import type { Meta } from './api/types'

// 拒答补全判定：前缀/子串匹配，规则集中在这一个函数（WEB_DEMO_PLAN §4.2 记录的代价）。
// 拒答文案若演化，frontend/scripts/test_refusal.mjs 的样例夹具会先红提醒。

export type GapKind = 'metric' | 'dimension' | 'question' | 'unknown'

export interface RefusalGuide {
  kind: GapKind
  metricNames: string[]
  dimensionNames: string[]
  missingTerms: string[]
}

const METRIC_MARK = '在指标定义里没有登记'
const DIMENSION_MARKS = ['不是当前支持的维度', '维度没有登记为']
const CALIBER_MARK = '是未登记的限定词'

export function classifyRefusal(message: string | null | undefined, meta?: Meta): RefusalGuide {
  const text = message ?? ''
  const metricNames = sliceBetween(text, '当前已定义指标：', ['；', '。'])
  const dimensionNames = sliceBetween(text, '支持的维度：', ['。'])
  // 文案里的「」不全是缺口：合法指标/维度名也会被点名（如 R1 的「销售额」），
  // 以 /api/meta 的登记表名字为准剔除
  const registered = new Set([
    ...metricNames,
    ...dimensionNames,
    ...sliceBetween(text, '指标「', ['」']),
    ...(meta?.metrics.map((m) => m.name) ?? []),
    ...(meta?.dimensions.map((d) => d.name) ?? []),
  ])
  const missingTerms = [...text.matchAll(/「([^」]+)」/g)]
    .map((m) => m[1])
    .filter((t) => !registered.has(t))
  let kind: GapKind = 'unknown'
  if (text.startsWith('无法回答') && text.includes(METRIC_MARK)) kind = 'metric'
  else if (text.startsWith('无法回答') && DIMENSION_MARKS.some((m) => text.includes(m))) kind = 'dimension'
  else if (text.startsWith('无法回答') && text.includes(CALIBER_MARK)) kind = 'question'
  else if (text.includes('问题为空')) kind = 'question'
  return { kind, metricNames, dimensionNames, missingTerms }
}

function sliceBetween(text: string, startMark: string, stopChars: string[]): string[] {
  const start = text.indexOf(startMark)
  if (start < 0) return []
  let rest = text.slice(start + startMark.length)
  let stop = rest.length
  for (const ch of stopChars) {
    const i = rest.indexOf(ch)
    if (i >= 0) stop = Math.min(stop, i)
  }
  return rest
    .slice(0, stop)
    .split('、')
    .map((s) => s.trim())
    .filter(Boolean)
}

export interface GuideButton {
  label: string
  backfill: string
}

const ENUM_VALUES_PER_DIM = 3
const MAX_BUTTONS = 12

// 补全按钮：缺指标 → meta 指标名；缺维度/未登记限定词 → 支持维度，带 enum 的换列取值
// （演示"补一个已登记取值就能答"）。点击回填输入框，不自动提交。
export function guideButtons(guide: RefusalGuide, meta: Meta, question: string): GuideButton[] {
  const buttons: GuideButton[] = []
  if (guide.kind === 'metric') {
    for (const m of meta.metrics) {
      buttons.push({ label: m.name, backfill: spliceFirstGap(question, guide, m.name) })
    }
    return buttons.slice(0, MAX_BUTTONS)
  }
  if (guide.kind === 'dimension' || guide.kind === 'question') {
    for (const name of guide.dimensionNames) {
      const dim = meta.dimensions.find((d) => d.name === name)
      // 只给带登记取值的维度出按钮：维度名本身回填进题面仍撑不住，不误导
      if (!dim?.enum?.length) continue
      for (const value of dim.enum.slice(0, ENUM_VALUES_PER_DIM)) {
        buttons.push({ label: `${name}：${value}`, backfill: spliceFirstGap(question, guide, value) })
      }
    }
  }
  return buttons.slice(0, MAX_BUTTONS)
}

// 回填策略：题面第一处缺口（连续残片段，中间只允许连接词"的/和/与/、"）整段换成
// 补全词，片段之后的文字原样保留；题面不含缺口词时不猜位置，追加括号提示
function spliceFirstGap(question: string, guide: RefusalGuide, replacement: string): string {
  if (guide.missingTerms.length === 0) return `${question}（改成：${replacement}）`
  const connectors = new Set(['的', '和', '与', '、', ' '])
  const termAt = (t: number) => guide.missingTerms.find((term) => question.startsWith(term, t))
  for (let i = 0; i < question.length; i += 1) {
    const term = termAt(i)
    if (!term) continue
    let end = i + term.length
    for (;;) {
      const next = termAt(end)
      if (next) {
        end += next.length
        continue
      }
      if (connectors.has(question[end]) && termAt(end + 1)) {
        end += 1
        continue
      }
      break
    }
    return question.slice(0, i) + replacement + question.slice(end)
  }
  return `${question}（改成：${replacement}）`
}
