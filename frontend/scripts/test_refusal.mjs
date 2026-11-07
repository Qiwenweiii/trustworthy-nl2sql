// 匹配规则用例（方案 §6：文案若改，这条测试先红提醒）。
// 夹具 = 2026-11-06 从真实判定模块导出的三条 R 类拒答文案（problems/eval_cases.json）。
// 运行：node --experimental-strip-types scripts/test_refusal.mjs（node >= 22.6）

import assert from 'node:assert/strict'
import { classifyRefusal, guideButtons } from '../src/refusal.ts'

const MESSAGES = {
  R1: '无法回答：「华东地区」不是当前支持的维度，维度定义表里没有它对应的字段，按它拆分只能靠自造映射。当前「销售额」支持的维度：时间、渠道、城市、客户类型、订单状态。',
  R2: '无法回答：「产品线」「毛利率」在指标定义里没有登记，算它需要哪个字段、什么公式都没定义，系统不替你猜。当前已定义指标：销售额、订单数；支持的维度：时间、渠道、城市、客户类型、订单状态。',
  R3: '无法回答：「新客」是未登记的限定词，指标「销售额」的定义里没有它的口径（怎么判、按哪个字段判都没写），系统不替你发明算法。当前已定义指标：销售额、订单数；支持的维度：时间、渠道、城市、客户类型、订单状态。',
}

const META = {
  metrics: [
    { name: '销售额', synonyms: ['营收'] },
    { name: '订单数', synonyms: ['单量'] },
  ],
  dimensions: [
    { name: '时间', aliases: ['时间'], column: 'orders.order_date' },
    { name: '渠道', aliases: ['渠道'], column: 'orders.channel', enum: ['app', 'web', 'miniapp', 'offline', '线上', '线下', '小程序'] },
    { name: '城市', aliases: ['城市'], column: 'customers.city', enum: ['杭州', '武汉', '上海', '北京', '南京', '成都', '广州', '深圳'] },
    { name: '订单状态', aliases: ['状态'], column: 'orders.status', enum: ['paid', 'refunded', 'cancelled', '已支付', '未支付', '已退款', '已取消'] },
  ],
  sample_questions: [],
}

let failed = 0
function check(name, fn) {
  try {
    fn()
    console.log(`ok   ${name}`)
  } catch (err) {
    failed += 1
    console.error(`FAIL ${name}\n     ${err.message}`)
  }
}

check('R1 判为缺维度', () => {
  const g = classifyRefusal(MESSAGES.R1)
  assert.equal(g.kind, 'dimension')
  assert.deepEqual(g.missingTerms.filter((t) => t === '华东地区'), ['华东地区'])
  assert.deepEqual(g.dimensionNames, ['时间', '渠道', '城市', '客户类型', '订单状态'])
})

check('R2 判为缺指标并列出已定义指标', () => {
  const g = classifyRefusal(MESSAGES.R2)
  assert.equal(g.kind, 'metric')
  assert.deepEqual(g.metricNames, ['销售额', '订单数'])
})

check('R3 判为未登记限定词（问法缺口）', () => {
  const g = classifyRefusal(MESSAGES.R3)
  assert.equal(g.kind, 'question')
})

check('R2 补全按钮原位替换全部缺口词', () => {
  const g = classifyRefusal(MESSAGES.R2, META)
  const buttons = guideButtons(g, META, '上个月各产品线的毛利率是多少')
  assert.equal(buttons.length, 2)
  assert.equal(buttons[0].backfill, '上个月各销售额是多少')
})

check('R1 缺口词剔除被点名的合法指标（「销售额」不是缺口）', () => {
  const g = classifyRefusal(MESSAGES.R1, META)
  assert.ok(!g.missingTerms.includes('销售额'))
  assert.deepEqual(g.missingTerms, ['华东地区'])
})

check('R1 补全按钮用城市 enum 取值', () => {
  const g = classifyRefusal(MESSAGES.R1, META)
  const buttons = guideButtons(g, META, '上个月华东地区的销售额是多少')
  const cityButtons = buttons.filter((b) => b.label.startsWith('城市：'))
  assert.equal(cityButtons.length, 3)
  assert.ok(cityButtons.some((b) => b.backfill === '上个月上海的销售额是多少'))
  assert.ok(buttons.every((b) => !b.backfill.includes('华东')))
})

check('R3 未登记限定词给已登记取值当改问法按钮', () => {
  const g = classifyRefusal(MESSAGES.R3, META)
  const buttons = guideButtons(g, META, '上个月新客销售额是多少')
  // 只给带 enum 的维度出取值按钮；缺口词"新客"被换成取值后其余原样
  assert.ok(buttons.some((b) => b.backfill === '上个月app销售额是多少'))
  assert.ok(buttons.every((b) => !b.backfill.includes('新客')))
})

check('非拒答文案不误判', () => {
  assert.equal(classifyRefusal(null).kind, 'unknown')
  assert.equal(classifyRefusal('Execution interrupted').kind, 'unknown')
  assert.deepEqual(guideButtons(classifyRefusal('Execution interrupted'), META, 'x'), [])
})

if (failed > 0) {
  console.error(`\n${failed} 条失败`)
  process.exit(1)
}
console.log('\n全部通过：拒答匹配规则与文案夹具一致')
