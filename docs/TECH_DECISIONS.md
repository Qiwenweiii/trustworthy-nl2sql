# 技术选型与数据结构决策

本文档记录项目关键技术选型和核心数据结构。这些是决策，不是讨论稿——按此实现，有异议先提。

## 一、技术选型

### 1. 编排层：LangGraph StateGraph（不用 create_agent）

**决策**：用 `TypedDict` 定义状态，节点函数只返回部分状态，通过声明式 Reducer 合并，条件边做路由。

**理由**：本项目的流程是确定性多阶段管道，门控和断言必须是硬节点，而不是"模型可选调用的工具"。StateGraph 让"不通过就不往下走"成为流程约束，而不是 Prompt 约束。

**约束**：

- 每个节点只写自己负责的 key，不返回整份 state；
- 条件边路由函数必须是纯函数，只读 state 返回路由 key；
- 用 `thread_id` 关联会话，配合 SQLite checkpointer 实现断点恢复。

### 2. Web 层：FastAPI + SSE

**决策**：FastAPI 提供 `/ask` 和 `/health`，SSE 推送步骤事件。

**约束**：路由层只做参数校验和事件转发，业务逻辑全部在 service / graph 层，routers 里不写 SQL、不调 LLM。

### 3. 演示数据库：DuckDB

**决策**：DuckDB 作为演示分析库，严格只读 + 配置锁定 + 看门狗超时。

**理由**：DuckDB 是列式存储，分析查询比 SQLite 快 10–100 倍，适合聚合类问数；进程内运行，不需要连接池或网络跳转。

**安全约束（硬性）**：

- `read_only=True` 单独使用不安全，必须同时设 `enable_external_access=false` 和 `lock_configuration=true`，防止通过 SQL 读写文件或访问网络；
- DuckDB 没有内置的 progress-handler 超时，必须用看门狗线程调用 `connection.interrupt()` 来中断超时查询；
- 返回行数上限在应用层截断，不依赖数据库。

### 4. LLM 接入：LiteLLM 作为统一网关

**决策**：通过 LiteLLM 的 OpenAI 兼容接口调用模型，默认云端模型，可通过环境变量切到本地 Ollama。

**理由**：LiteLLM 把不同 provider 统一成一个 OpenAI 兼容端点；本地 Ollama 接入用 `ollama/llama3.2` + `base_url=http://localhost:11434`，不需要改业务代码。

**约束**：API Key 只从环境变量读，绝不进代码库；LLM 调用封装在独立 client 里，graph 节点不直接持有 SDK 实例。

### 5. 数据层：SQLite + DuckDB + JSONL

**决策**：三个存储各司其职，不混用。

| 存储   | 存什么                                           | 理由                                        |
| ------ | ------------------------------------------------ | ------------------------------------------- |
| SQLite | 指标定义、口径选项、会话、checkpoint、trace 索引 | 单文件、零运维、支持事务，适合元数据        |
| DuckDB | 演示数据（事实表 + 维度表）                      | 只读分析查询，列式快                        |
| JSONL  | 每次查询的完整事件流（逐步追加）                 | append-only，天然可回放，不需要 schema 迁移 |

**约束**：DuckDB 连接必须用只读模式并做上述安全加固；trace 一旦写入，不允许修改历史记录。

### 6. 语义层：JSON 文件 + 确定性别名匹配

**决策**：指标定义放在 JSON 文件里，匹配用精确别名查找（小写归一化），不做模糊匹配、不做向量检索。

**理由**：语义层的价值在确定性，不在"智能"。精确别名查找可测试、可复现。模糊匹配留到 v2，且只作为召回兜底，不参与门控判断。

## 二、目录结构

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

## 四、实施顺序

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
