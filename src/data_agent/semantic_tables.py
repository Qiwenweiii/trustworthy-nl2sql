"""语义层登记表与词表：纯数据，不放任何逻辑。

想回答"系统认识哪些指标/维度/列/说法"，只看这个文件；消费方分别是
services/metrics.py（订单范围强制注入）、services/semantic_check.py（拒答判定）、
services/schema.py（schema 渲染）。加指标、加维度、补词表只改这里。

登记表的字段含义写在各自表格上方的原注释里；判定规则本身不在本文件，
见消费方模块的 docstring。
"""

from __future__ import annotations

# ---------------------------------------------------------------- 指标登记表
# 订单范围：销售额/订单数都只算已支付订单；退款(refunded)、取消(cancelled)不计入。
ORDER_SCOPE_PAID = {
    "column": "status",
    "value": "paid",
    "sql": "status = 'paid'",
    "note": "订单范围：只算已支付（status='paid'），退款与取消不计入",
}

METRICS: dict[str, dict] = {
    "销售额": {
        "name": "销售额",
        "synonyms": ["销售额", "营收", "GMV", "成交额", "销售金额", "收入"],
        "table": "orders",
        "time_field": "order_date",
        "formula": "含税 SUM(amount)；不含税 SUM(amount - COALESCE(tax_amount, 0))",
        "required_filters": [ORDER_SCOPE_PAID],
    },
    "订单数": {
        "name": "订单数",
        "synonyms": ["订单数", "订单量", "下单量", "成交订单数", "单量"],
        "table": "orders",
        "time_field": "order_date",
        "formula": "COUNT(*)",
        "required_filters": [ORDER_SCOPE_PAID],
    },
}

# ---------------------------------------------------------------- 维度登记表
# 维度是唯一登记表：以后加维度只加这一条，指标侧零维护。
# applies_to 默认 "*"（对所有指标可用）；写指标名列表表示"只有这些指标能按它拆"。
# column 必须是 schema 里真实存在的列，test_semantic_check 的一致性用例会双向核对。
DIMENSIONS: dict[str, dict] = {
    "时间": {
        "name": "时间",
        "aliases": ["时间", "时间段", "月份", "日期", "下单时间", "下单日期", "支付时间", "付款时间", "成交时间", "支付日期"],
        "column": "orders.order_date",
        "applies_to": "*",
    },
    "渠道": {
        "name": "渠道",
        "aliases": ["渠道", "下单渠道", "渠道来源"],
        "column": "orders.channel",
        "enum": ["app", "web", "miniapp", "offline", "线上", "线下", "小程序"],
        "applies_to": "*",
    },
    "城市": {
        "name": "城市",
        "aliases": ["城市", "所在城市"],
        "column": "customers.city",
        # 取值来自演示库 customers.city 的 distinct 清单（阶段 1 收口时补登记，
        # 让 D21/D22 缺口探针转绿；换种子/换库时同步更新，与 test_init_db 同理）
        "enum": ["杭州", "武汉", "上海", "北京", "南京", "成都", "广州", "深圳"],
        "applies_to": "*",
    },
    "客户类型": {
        "name": "客户类型",
        "aliases": ["客户类型", "客群", "客户分层"],
        "column": "customers.segment",
        "applies_to": "*",
    },
    "订单状态": {
        "name": "订单状态",
        "aliases": ["订单状态", "状态", "支付状态"],
        "column": "orders.status",
        "enum": ["paid", "refunded", "cancelled", "已支付", "未支付", "已退款", "已取消"],
        "applies_to": "*",
    },
}

# ---------------------------------------------------------------- 列说明
# schema 渲染进提示词时挂在列名后面的中文说明（services/schema.py 消费）。
COLUMN_NOTES = {
    "customers.id": "客户ID",
    "customers.name": "客户名称",
    "customers.city": "所在城市",
    "customers.segment": "客户类型：企业/中小企业/个人",
    "orders.id": "订单ID",
    "orders.order_no": "订单号",
    "orders.customer_id": "关联 customers.id",
    "orders.order_date": "下单日期（YYYY-MM-DD）",
    "orders.paid_at": "支付时间，未支付为 NULL",
    "orders.status": "订单状态：paid=已支付, refunded=已退款, cancelled=已取消",
    "orders.amount": "订单金额（元，含税）",
    "orders.tax_amount": "税额（元），已取消订单为 NULL",
    "orders.channel": "下单渠道：app/web/miniapp/offline",
}

# ---------------------------------------------------------------- 拒答判定词表
# 以下各组只被 services/semantic_check.py 消费；放行规则三条，见该模块 docstring。

# 金额口径词：来自「销售额」定义里已有的含税/不含税两种算法，不是新问题里冒出来的词。
CALIBER_ALIASES = ["不含税", "含税", "税前", "税后", "净额", "毛额", "折前", "折后"]

# 业务对象与问法用词：它们不携带口径信息，落到 SQL 里也不存在"自造算法"的风险。
# 只登记整词，不登记单字（登了"客"就会把"新客"一起放过）。
ENTITY_NOUNS = ["订单", "单子", "客户", "金额", "总数", "总额", "明细", "数据", "情况", "排名", "排行", "趋势"]

GRAMMAR_WORDS = [
    "的", "了", "是", "多少", "几", "各", "每", "按", "照", "算", "看", "查", "统计", "汇总",
    "请问", "帮我", "帮忙", "我", "你", "给", "一下", "呢", "吗", "吧", "啊", "以及", "还有", "和", "与",
    "或", "在", "从", "到", "对", "中", "里", "这个", "那个", "分别", "依次", "期间", "时候", "什么",
    "怎么", "怎样", "哪", "哪些", "最", "比较", "列出", "然后", "写", "用",
    # 量词副词与否定词：不携带口径，也不指定算法，纳进来避免误杀正常问法
    "一共", "总共", "共有", "所有", "全部", "没有", "只要", "除去", "除了",
]

TIME_PHRASES = [
    "上个月", "上月", "本月", "这个月", "当月", "上上个月", "本周", "这周", "上周", "今年", "去年",
    "前年", "明年", "今天", "昨天", "前天", "现在", "至今", "以来", "年初", "年底", "月初", "月底",
    "月末", "中旬", "上旬", "半年", "一年", "两年", "上半年", "下半年", "整月", "同期",
]

TIME_PATTERNS = [
    r"\d{4}\s*年(?:\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?)?",
    r"\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?",
    r"(?:最近|过去|前|后|近|未来)\s*[0-9一二三四五六七八九十两几]+\s*(?:个?(?:月|周|天|日|年)|季度)",
    r"第?\s*[0-9一二三四1-4]\s*季度",
]

# 残片兜底放行用的时间线索字符集：整段残片都只由这些字（和数字）组成时才当时间表达放过。
# 收紧到"整段"而不是"沾一个就放"，否则「月度环比」会一起漏过去，模型转头就自造环比口径。
TIME_HINT_CHARS = set(
    "年月日时间周期季半天号以来过去前今明昨上本当每初底中旬载一三四五六七八九十几两整"
    "0123456789"
)

# 残片里出现这些词，说明用户问的是"按什么拆"，拒答文案要说它缺的是维度而不是指标。
DIMENSION_HINT_WORDS = [
    "地区", "区域", "大区", "片区", "省份", "省", "国家", "产品线", "品类", "类目", "品线",
    "品牌", "门店", "店铺", "部门", "团队", "等级", "级别", "来源", "平台",
]

SKIPPABLE_CHARS = set(" \t\r\n，,。.、；;：:!！?？(（)）\"'`【】[]{}<>《》/\\-+*_~^%$#@&|=×")

# check_question 结论里 reason_code 的全部取值（对外契约，评测与文案据此分流）。
REASON_CODES = (
    "empty_question",
    "missing_metric",
    "missing_dimension",
    "dimension_not_supported",
    "unknown_qualifier",
)
