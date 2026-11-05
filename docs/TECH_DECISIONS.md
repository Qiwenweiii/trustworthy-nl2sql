# 技术选型与数据结构决策

> **状态（2026-11-04）**：本文 §一～§三 是**立项时的原始方案**，其中多项在 v0/v1 落地时被有意收窄或替换，另有几项根本没做。**读这份文档必须先对照 §四「原决策 vs 实际落地」**——照 §一～§三 直接实现会得到一个与当前仓库完全不同的项目。当前实现以根目录 `README.md`（对外契约）与 `NOTES.md`（接口地图）为准。
>
> 保留原文不删的理由：这些决策里的**安全约束类**内容（DuckDB 的 `enable_external_access` / 看门狗、断言不读 SQL 的零共享代码原则、trace 一旦写入不可改）仍是换数据库或补 trace 时**唯一成文的依据**，删掉等于把理由销毁。
>
> 本文档记录项目关键技术选型和核心数据结构。这些是决策，不是讨论稿——按此实现，有异议先提。（**注**：这句仅对尚未实现的条目有效；已实现的条目以代码为准。）

## 一、技术选型

### 1. 编排层：LangGraph StateGraph（不用 create_agent）

> 落地：**收窄**——图只覆盖语义拒答一个岔路（2 节点 + 1 条件边），checkpointer 未做；但下面三条约束里前两条完整保留。详见 §四 的 1.1 / 1.1b。

**决策**：用 `TypedDict` 定义状态，节点函数只返回部分状态，通过声明式 Reducer 合并，条件边做路由。

**理由**：本项目的流程是确定性多阶段管道，门控和断言必须是硬节点，而不是"模型可选调用的工具"。StateGraph 让"不通过就不往下走"成为流程约束，而不是 Prompt 约束。

**约束**：

- 每个节点只写自己负责的 key，不返回整份 state；
- 条件边路由函数必须是纯函数，只读 state 返回路由 key；
- 用 `thread_id` 关联会话，配合 SQLite checkpointer 实现断点恢复。

### 2. Web 层：FastAPI + SSE

> 落地：**未做**，整层推迟。入口是 CLI（`cli.py:89`）+ `pipeline.ask()`。下面这条分层约束换了个宿主：业务逻辑全在 pipeline/services，渲染在 CLI，CLI 里没有 SQL、没有模型调用。

**决策**：FastAPI 提供 `/ask` 和 `/health`，SSE 推送步骤事件。

**约束**：路由层只做参数校验和事件转发，业务逻辑全部在 service / graph 层，routers 里不写 SQL、不调 LLM。

### 3. 演示数据库：DuckDB

> 落地：**替换为 SQLite**。下面三条硬性安全约束有两条不适用（DuckDB 专有的配置锁定与看门狗），但**风险本身换了数据库仍然存在**，SQLite 侧的等价手段是：只读 URI `file:…?mode=ro` + `enable_load_extension(False)` + authorizer 白名单 + `set_progress_handler` 超时中断 + 应用层 `LIMIT`（`services/executor.py`）。第三条「行数上限在应用层截断」原样成立。
>
> **仍然值得记住的一条**：`read_only=True` 单独使用不安全。SQLite 侧对应的外部能力入口是 `load_extension`，所以它被显式关闭，并在静态校验（`forbidden_function`）与 authorizer 层各拦一次。

**决策**：DuckDB 作为演示分析库，严格只读 + 配置锁定 + 看门狗超时。

**理由**：DuckDB 是列式存储，分析查询比 SQLite 快 10–100 倍，适合聚合类问数；进程内运行，不需要连接池或网络跳转。

**安全约束（硬性）**：

- `read_only=True` 单独使用不安全，必须同时设 `enable_external_access=false` 和 `lock_configuration=true`，防止通过 SQL 读写文件或访问网络；
- DuckDB 没有内置的 progress-handler 超时，必须用看门狗线程调用 `connection.interrupt()` 来中断超时查询；
- 返回行数上限在应用层截断，不依赖数据库。

### 4. LLM 接入：LiteLLM 作为统一网关

> 落地：**替换**为 `openai` SDK 直连 OpenAI 兼容端点（`llm_client.py:9`）。下面两条约束**都成立**：key 只从环境变量/`.env` 读、不进代码库；LLM 调用封装在 `services/llm_client.py`，pipeline 与 graph 节点都不持有 SDK 实例。达到「不改业务代码换 provider」目的的方式是改 `OPENAI_BASE_URL`，代价是没有 `ollama/llama3.2` 这种一行前缀切换。

**决策**：通过 LiteLLM 的 OpenAI 兼容接口调用模型，默认云端模型，可通过环境变量切到本地 Ollama。

**理由**：LiteLLM 把不同 provider 统一成一个 OpenAI 兼容端点；本地 Ollama 接入用 `ollama/llama3.2` + `base_url=http://localhost:11434`，不需要改业务代码。

**约束**：API Key 只从环境变量读，绝不进代码库；LLM 调用封装在独立 client 里，graph 节点不直接持有 SDK 实例。

### 5. 数据层：SQLite + DuckDB + JSONL

> 落地：**只做了「演示与查询用 SQLite」这一格**。指标定义与维度定义既不在 SQLite 表也不在 JSON 文件，而是 Python 常量 `METRICS`（`semantic_tables.py:22`）/ `DIMENSIONS`（`semantic_tables.py:45`）；会话、checkpoint、trace 索引、JSONL 事件流**全部未做**。「不混用」的原则保留：查询走只读连接，配置走环境变量，评测期望值走用例 JSON。

**决策**：三个存储各司其职，不混用。

| 存储   | 存什么                                           | 理由                                        |
| ------ | ------------------------------------------------ | ------------------------------------------- |
| SQLite | 指标定义、口径选项、会话、checkpoint、trace 索引 | 单文件、零运维、支持事务，适合元数据        |
| DuckDB | 演示数据（事实表 + 维度表）                      | 只读分析查询，列式快                        |
| JSONL  | 每次查询的完整事件流（逐步追加）                 | append-only，天然可回放，不需要 schema 迁移 |

**约束**：DuckDB 连接必须用只读模式并做上述安全加固；trace 一旦写入，不允许修改历史记录。

### 6. 语义层：JSON 文件 + 确定性别名匹配

> 落地：**匹配方式这条完整保留**（不做模糊匹配、不做向量检索，这正是语义拒答能单测、能复现的前提，见 `README.md`「v0 已知边界」最后第二条）；**「放在 JSON 文件里」未做到**，登记表是 Python 字典，加一条指标要改代码。「模糊匹配留到 v2 且只作召回兜底」仍是待办。

**决策**：指标定义放在 JSON 文件里，匹配用精确别名查找（小写归一化），不做模糊匹配、不做向量检索。

**理由**：语义层的价值在确定性，不在"智能"。精确别名查找可测试、可复现。模糊匹配留到 v2，且只作为召回兜底，不参与门控判断。

## 二、目录结构

> ⚠️ 以下是**原方案的目录规划，未按此落地**。实际结构见 §四 第 2 行；照此建目录会造出仓库里不存在的 `src/api/`、`services/trace.py` 等空壳。

```
src/
├── api/
│   ├── main.py              # FastAPI app、lifespan、/health
│   ├── routers/
│   │   └── ask.py           # POST /ask、SSE 流
│   └── schemas.py           # 请求/响应 Pydantic DTO
├── graph/
│   ├── state.py             # AgentState (TypedDict) + Reducer
│   ├── builder.py           # StateGraph 装配、条件边
│   └── nodes/
│       ├── semantic_resolve.py   # 指标匹配 + 口径信息
│       ├── clarification_gate.py # 门控（规则，不调 LLM）
│       ├── generate_sql.py       # LLM 生成 SQL
│       ├── static_audit.py       # 只读/单条/危险函数检查
│       ├── execute.py            # DuckDB 执行（超时/行数限制）
│       ├── result_assert.py      # 独立结构断言
│       └── answer.py             # 答案组装 + 证据
├── services/
│   ├── semantic.py          # 加载指标 JSON、别名匹配
│   ├── llm_client.py        # LiteLLM 封装
│   ├── sql_guard.py         # 静态审计实现
│   ├── executor.py          # DuckDB 只读执行器
│   └── trace.py             # JSONL 写入 + SQLite 索引
├── data/
│   ├── semantic/
│   │   └── metrics.json     # 指标与口径定义
│   └── demo.duckdb          # 演示分析库
└── tests/
    ├── test_semantic.py
    ├── test_gate.py
    ├── test_sql_guard.py
    ├── test_executor.py
    └── test_result_assert.py
```

**分层原则**：`graph/nodes/` 只做编排和状态流转，具体能力在 `services/` 里；`api/` 不碰业务逻辑。

## 三、核心数据结构

> ⚠️ 本节四个结构中，**只有 3.4 的「断言不接收 SQL」这条硬约束以别的形式成立**（`is_result_empty(rows)` 签名里没有 SQL），其余字段名与形态都和当前代码不一致。落地实况见 §四 的 3.1～3.4c 各行。真正对外稳定、不可改的数据结构是 `outcome` 字典，字段字典在根目录 `NOTES.md:360-411`。

### 1. AgentState（TypedDict + Reducer）

```python
from typing import TypedDict, Annotated
from operator import add

class AgentState(TypedDict):
    # 输入
    question: str
    thread_id: str

    # 语义层结果
    metric: dict | None
    ambiguity: list[str] | None
    clarification_options: list[str] | None

    # SQL 链路
    generated_sql: str | None
    audit_verdict: dict | None       # {"passed": bool, "violations": [...]}
    query_result: list[dict] | None
    execution_error: str | None

    # 结果断言
    assertions: Annotated[list[dict], add]
    severity: str | None             # "ok" / "warning" / "blocking"

    # 输出
    final_answer: str | None
    evidence: dict | None

    # 事件流（追加式）
    events: Annotated[list[dict], add]
```

**关键设计**：`assertions` 和 `events` 用 `Annotated[list, add]` 做追加式 Reducer，多个节点写同一个 key 时不会被覆盖。其他字段默认"后写覆盖"，因为每个 key 只有一个节点负责写。

### 2. 语义层指标定义（metrics.json）

```json
{
  "version": 1,
  "metrics": [
    {
      "name": "sales_amount",
      "display_name": "销售额",
      "aliases": ["销售额", "营收", "销售收入"],
      "table": "fct_sales",
      "time_column": "stat_date",
      "calibers": [
        {
          "id": "tax_included",
          "label": "含税口径",
          "sql_condition": "1=1",
          "description": "订单金额含税"
        },
        {
          "id": "tax_excluded",
          "label": "不含税口径",
          "sql_condition": "tax_amount IS NOT NULL",
          "description": "仅统计已确认税额的订单"
        }
      ],
      "default_caliber": null,
      "non_negative": true
    }
  ]
}
```

**约束**：

- `aliases` 用于精确匹配，匹配时统一转小写；
- `calibers` 长度 > 1 且 `default_caliber` 为空 → 门控触发澄清；
- `non_negative` 是结果断言用的元数据，断言不读 SQL、只读这个定义和结果结构。

### 3. 查询链路记录

**决策**：trace 不用一张大表全存，而是"SQLite 索引 + JSONL 事件流"分离存储。SQLite 存可检索的摘要，JSONL 存完整事件流。

**SQLite 摘要表**：

```sql
CREATE TABLE query_runs (
    run_id          TEXT PRIMARY KEY,
    thread_id       TEXT NOT NULL,
    question        TEXT NOT NULL,
    metric_name     TEXT,
    caliber_id      TEXT,
    generated_sql   TEXT,
    row_count       INTEGER,
    assertion_status TEXT,
    final_status    TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX idx_query_runs_thread ON query_runs(thread_id, created_at);
```

**JSONL 事件结构**（追加写，不改写）：

```json
{
  "run_id": "...",
  "thread_id": "...",
  "seq": 3,
  "event": "static_audit",
  "timestamp": "2026-10-28T10:00:00Z",
  "payload": {
    "passed": false,
    "violations": ["multi_statement"],
    "sql_hash": "sha256:..."
  }
}
```

**理由**：JSONL 每条事件是结构化 JSON，自由文本放在 `payload.message` 里；append-only 保证不可篡改，满足审计基本要求。

### 4. 门控与断言的返回契约

**门控节点返回**（只返回它负责的 key）：

```python
{
    "ambiguity": ["sales_amount 有 2 种口径，用户未指定"],
    "clarification_options": ["含税口径", "不含税口径"],
    "events": [{"event": "clarification_triggered"}]
}
```

**断言节点返回**：

```python
{
    "assertions": [
        {"type": "empty_result", "passed": True, "message": "结果非空"},
        {"type": "negative_value", "passed": False, "message": "sales_amount 出现负值 -500"}
    ],
    "severity": "warning",
    "events": [...]
}
```

**硬约束**：断言节点不接收 `generated_sql` 字段，只接收 `query_result` 和 `metric` 定义。这是"零共享代码"的物理保证——它拿不到 SQL，就不可能重跑。

## 四、原决策 vs 实际落地（v0 + v1，2026-11-04 收口）

三种状态，逐条对照，全部按代码核实过（项目内 `grep -i` 查 `duckdb|fastapi|litellm|uvicorn|jsonl|thread_id|clarification` 零命中，唯一命中在 `.venv` 第三方包内）。

| # | 原决策（§一～§三） | 状态 | 实际落地 |
|---|---|---|---|
| 1.1 | LangGraph `StateGraph` 编排 | **收窄** | 原决策的三条约束全部保留（节点只写自己的 key、条件边是纯函数、不用 `create_agent`），但图只覆盖**一个岔路**：语义拒答。`graph/` 共 3 个文件、98 行：`SemanticGateState` 三键（`graph/state.py:8`）、两个节点（`nodes/gate_check.py:9` / `nodes/gate_refuse.py:11`）、一条条件边（`builder.py:24`），`lru_cache` 编译一次（`builder.py:31`）。**链路其余七步是普通函数调用**，顺序见 `pipeline.py` 的 `ask()`（`:63 → :67 → :80 → :81 → :84 → :89`）：拒答 → 取 SQL → 指标注入 → 时间边界 → guard → 执行，每步实现在 `pipeline_steps.py` 一函数一步。 |
| 1.1b | 配合 SQLite checkpointer 做断点恢复 | **未做** | `build_graph()` 是裸 `compile()`，无 checkpointer。理由：语义拒答是单次判定，没有断点恢复需求（见 `NOTES.md:46`）。原决策里的 `thread_id` 概念整个不存在。 |
| 1.2 | FastAPI + SSE（`/ask`、`/health`） | **未做** | 无 `api/` 目录、无 `src/data_agent/routers`。入口是 CLI：`cli.main(argv)`（`cli.py:89`），`python -m data_agent.cli "问题" [--json] [--debug]`。原决策的「routers 里不写 SQL、不调 LLM」这条分层原则，被「评测只调 `pipeline.ask()`，CLI 只做渲染」替代。 |
| 1.3 | DuckDB 演示分析库 | **替换** | SQLite（`data/demo.db`，`orders` + `customers` 两张表，`scripts/init_db.py:29-48`）。原方案的三条安全加固里有两条**不适用于 SQLite**，改用等价手段：`read_only=True` + `enable_external_access=false` + `lock_configuration=true` → 只读 URI `file:…?mode=ro` + `enable_load_extension(False)` + authorizer 白名单（`services/executor.py:open_readonly_connection`）；看门狗线程调 `connection.interrupt()` → `set_progress_handler(_on_progress, 1000)`（`executor.py:100`）。**唯一仍然成立的教训**：`read_only` 单独使用不安全——SQLite 侧对应的是 `load_extension`，所以它被显式关掉，并在静态校验（`forbidden_function`）和 authorizer 层各拦一次。 |
| 1.4 | LiteLLM 统一网关 | **替换** | `openai` SDK 直连 OpenAI 兼容端点（`llm_client.py:9` `from openai import OpenAI`）。切本地模型改 `OPENAI_BASE_URL` 环境变量，不改代码——达到原决策「不改业务代码即可换 provider」的目的，代价是没有 provider 名前缀（`ollama/llama3.2`）那种一行切换。`temperature=0` 保留。**「API Key 只从环境变量读、LLM 调用封装在独立 client」两条约束都成立**：`config.py` 读 `.env`，graph 节点与 pipeline 都不持有 SDK 实例。 |
| 1.5 | 三存储各司其职（SQLite 元数据 / DuckDB 演示 / JSONL trace） | **部分** | 演示与查询用 SQLite（原方案里 SQLite 只存元数据）；**指标定义不进 SQLite，是 Python 常量** `METRICS`（`semantic_tables.py:22`）；维度定义同理 `DIMENSIONS`（`semantic_tables.py:45`）；**JSONL 与 trace 索引整个没做**（见 3.3）。 |
| 1.6 | 语义层：JSON 文件 + 确定性别名匹配 | **一半成立** | 「确定性别名匹配、不做模糊匹配、不做向量检索」**完整保留**，这是拒答实现的判据基础（`README.md:252`）。「放在 JSON 文件里」没做到：登记表是 Python 字典，改动需改代码而非改数据文件。原决策说的「模糊匹配留到 v2 只作召回兜底」仍为待办。 |
| 2 | 目录结构（`src/api`、7 个 `graph/nodes`、`services/semantic.py`、`services/trace.py`、`data/semantic/metrics.json`） | **未照做** | 实际 `src/data_agent/`：`pipeline.py`（只留编排）、`pipeline_steps.py`（链路六步一函数一步）、`semantic_tables.py`（登记表与词表，纯数据）、`cli.py`、`config.py`、`prompts.py`、`graph/`（3 文件）、`services/`（扁平 8 个：`schema` / `llm_client` / `sql_guard` / `sql_text` / `metrics` / `time_boundary` / `semantic_check` / `executor`，其中 `sql_text` 是 SQL 文本扫描统一底座，guard/metrics/time_boundary 共用）。**「能力在 services、编排层只做流转」这条分层原则成立**，`semantic_check` 的判定逻辑就放在 services 里，所以单测不装 LangGraph 也能测（`NOTES.md:277`）。 |
| 3.1 | `AgentState`（TypedDict + `add` Reducer，14 个键） | **未做** | 只有 `SemanticGateState` 三键（`question` / `verdict` / `route`），**没有任何 Reducer**，因为没有多节点写同一个键。链路状态由 `outcome` 字典承载（字段字典见 `NOTES.md:360-411`），它就是评测契约。 |
| 3.2 | `metrics.json` 结构（`aliases` / `calibers[]` / `default_caliber` / `non_negative`） | **字段名与形态都不同** | `METRICS` 每条是 `name` / `synonyms` / `table` / `time_field` / `formula` / `required_filters`（`semantic_tables.py:22`，现登记销售额、订单数两条）。**没有 `calibers` 数组**——含税/不含税两种口径只写在 `formula` 的自由文本里，因此「`calibers` 长度 > 1 且 `default_caliber` 为空 → 门控触发澄清」这条约束**没有对应实现**（澄清未做，见下）。`non_negative` 也没登记，因为负值断言未做。表/列名同样对不上：原方案写 `fct_sales.stat_date`，实际是 `orders.order_date` / `orders.paid_at`。 |
| 3.3 | 查询链路记录（`query_runs` 摘要表 + JSONL 事件流） | **未做** | 不落盘任何东西。替代方案是**三层可对质证据**：① `outcome` 契约字段（`enforcement` / `time_fix` / `guard` / `error`）；② `--debug` 八步一次性打印到 stderr（`README.md:100-127`）；③ 评测报告每次采样原样保留这些字段（`docs/eval_report_final.json` 是归档快照，运行产物 `problems/eval_report.json` 已进 `.gitignore`）。原决策「trace 一旦写入不允许修改」因没有 trace 而无从违反。 |
| 3.4 | 门控返回 `ambiguity` / `clarification_options` / `events` | **替换** | `check_question()` 返回 `allowed` / `reason_code` / `residues` / `message` 等（`semantic_check.py:156`，形状见 `NOTES.md:252-260`）。**没有澄清选项**——v1 的承诺是「口径声明（不含澄清）」（`eval_cases.json` 的 `meta.stages.v1.promise`），用户没说清口径时按默认口径出数，但必须把默认写进 `caliber_note`（`prompts.py:18`）。 |
| 3.4b | 断言节点返回 `assertions` / `severity`，且**拿不到 `generated_sql`** | **原则保留，形态未做** | 没有 `result_assert` 节点、没有 `severity` 字段。但「检查逻辑不得重跑 SQL、不得复用计算函数」这条**硬约束以更强形式成立**：唯一的执行后检查是纯函数 `is_result_empty(rows)`（`executor.py:50`），只看结果形状，签名里根本没有 SQL。**未实现**：负值告警、NULL 比例告警（`README.md:251` 已列为已知边界）。 |
| 3.4c | 证据组装（口径 / 来源表 / SQL / 校验结论四件套） | **收窄** | 现在给出：SQL、口径声明 `caliber_note`、两道语义层改动痕迹、空结果标记、guard 结论。**缺**：来源表面向用户的显式标注（表名只在 `schema` 与 debug 行里）、以及「正常 / 有异常 / 无法可靠回答」的整体结论——拒答有文案但没有统一的校验结论标签。 |
| 4 | 实施顺序阶段一/二/三 | **顺序被打乱，覆盖面收窄** | 见下节 §五 的逐步对照。 |

## 五、实施顺序（原文保留 + v0/v1 实际收口）

**阶段一（最小管道）**：

1. `metrics.json` + `services/semantic.py` + `test_semantic.py`；
2. `graph/state.py` + `graph/builder.py`，先只接 `semantic_resolve → generate_sql → execute → answer` 四个节点；
3. DuckDB 安全执行器 + 静态审计；
4. `/ask` 跑通 5–10 个问题。

**阶段二（可信层）**：

1. 插入 `clarification_gate` 节点 + 条件边（歧义 → 澄清，唯一 → 继续）；
2. 插入 `result_assert` 节点，确保它拿不到 `generated_sql`；
3. 加 trace 写入（SQLite 摘要 + JSONL 事件）；
4. SSE 推送事件流。

**阶段三（收口）**：拒答逻辑、`/health`、README、演示脚本。

### 实际收口（2026-11-04）

| 原步骤 | 实际 |
|---|---|
| 一.1 语义层 | 做了，但是 `semantic_tables.py` 的 `METRICS` / `DIMENSIONS` 常量 + `services/semantic_check.py` 的判定；测试是 `test_metrics.py` + `test_semantic_check.py`。 |
| 一.2 四节点图 | **只做了拒答这一个岔路**（两个节点）。`generate_sql` / `execute` / `answer` 是 pipeline 里的函数调用，不是节点。 |
| 一.3 执行器 + 静态审计 | 做了，SQLite 版：`sql_guard.py` + `executor.py` 三层防线（`README.md:195-201`）。 |
| 一.4 `/ask` | **没做 HTTP**，做成了 CLI + `pipeline.ask()`；`today` / `injected_sql` / `debug` 三个可选入参为评测预留（`README.md:66-74`）。 |
| 二.1 澄清门控 | **提前替换**：v1 做的是「口径声明」（不澄清）+「语义拒答」（问题撑不住就拒），不是歧义反问。 |
| 二.2 结果断言节点 | **提前替换**：`is_result_empty` 空结果标记，负值 / NULL 比例未做。 |
| 二.3 trace 写入 | **未做**，推迟。 |
| 二.4 SSE | **未做**，推迟。 |
| 三 收口 | 拒答 ✅、README ✅、演示脚本 ✅（`scripts/init_db.py`）；`/health` ❌（因为没有 HTTP 层）。 |

**一句话**：v0/v1 把「可信」做在了**契约与痕迹**这一层（每个决定都能对质），原方案把「可信」做在**流程与展示**这一层（门控节点、SSE 步骤、trace 回放）。前者是评测靶子需要的，后者是产品需要的。下一步若要走向产品，被推迟的四项（Web 层、SSE、trace、澄清）要按本文 §一～§三 原样捡起。
