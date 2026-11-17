# NOTES：接口地图（按函数速查）

这份文档逐个函数说明三件事：**接受什么输入、返回什么结果、在什么情况下报什么错**。

查法：先用「快捷索引」按函数名定位到文件与行号，再在本文对应模块的三级标题下看输入/返回/报错。每个函数固定四段：**签名 -> 输入 -> 返回 -> 报错**，长说明收在「说明」里，细节（函数签名、行号、字段名）一个没删。

看的时候记住两个约定：

- **抛出异常（`raise`）**：前置条件不满足，异常会向上冒。基本只有环境配置问题才会这样，例如没给生成器、库文件不存在。
- **收敛为 `status`**：业务上失败，但函数正常返回结果字典，由 `status` 字段说明失败原因。评测读的就是这个字段。

一句话区分：**环境问题抛异常；查询本身没成功则正常返回，原因写在 `status` 里**。

---

## 快捷索引

### pipeline — `src/data_agent/pipeline.py` / `src/data_agent/pipeline_steps.py`

| 函数 | 位置 | 一句话 |
| --- | --- | --- |
| `ask` | pipeline.py:33 | 全链路唯一入口：问题进，outcome 字典出 |
| `new_outcome` | pipeline_steps.py:29 | 造 outcome 骨架字典 |
| `stamp` | pipeline_steps.py:47 | 盖端到端耗时 `elapsed_ms` |
| `make_tracer` | pipeline_steps.py:53 | debug 逐步打印器（只写 stderr） |
| `run_refusal_gate` | pipeline_steps.py:65 | 语义拒答闸（第 1 步） |
| `resolve_sql` | pipeline_steps.py:92 | 拿到本轮 SQL：模型产出或注入（第 2 步） |
| `enforce_metrics` | pipeline_steps.py:137 | 指标条件强制注入（第 3 步） |
| `fix_time` | pipeline_steps.py:156 | 时间边界校验（第 4 步） |
| `run_guard` | pipeline_steps.py:172 | 静态安全校验（第 5 步） |
| `execute` | pipeline_steps.py:191 | 只读执行 + 判空（第 6 步） |
| `retry_after_error` | pipeline_steps.py:230 | 执行报错回炉修一次（第 7 步） |

### llm_client — `src/data_agent/services/llm_client.py`

| 函数 | 位置 | 一句话 |
| --- | --- | --- |
| `build_generator` | :53 | 拿配置造生成器（返回闭包 `generate`） |
| `generate` | build_generator 内部闭包 | 真调一次模型，返回 `GeneratedSql` |
| `extract_sql` | :31 | 从模型回复里抠 SQL（纯函数） |
| `extract_caliber_note` | :43 | 抠代码块之外的口径声明（纯函数） |

### sql_text — `src/data_agent/services/sql_text.py`

| 函数 | 位置 | 一句话 |
| --- | --- | --- |
| `mask_sql` | :92 | 等长空格遮罩引号/注释区 |
| `protected_spans` | :108 | 受保护区间清单 |
| `scan_views` | :113 | 结构/调用两份折叠视图 |
| `in_spans` | :150 | 位置是否落在区间清单内 |
| `is_bare_column` | :178 | 裸列判定（函数包裹的不算） |
| `referenced_tables` | :255 | FROM/JOIN 后的 `(表名, 别名)` 清单 |
| `split_query_blocks` | :269 | 把 SQL 切成查询块（顶层/派生表/CTE/子查询） |
| `SqlScanError` | :34 | 读不懂 SQL 的统一异常 |

### sql_guard — `src/data_agent/services/sql_guard.py`

| 函数 | 位置 | 一句话 |
| --- | --- | --- |
| `check_sql` | :106 | 判断是否单条只读 SELECT，返回 `GuardResult` |
| `GuardResult.as_dict` | :52 类内 | 写进 outcome 的两键版本 |
| `strip_trailing_semicolons` | :72 | 去尾分号与空白 |

### metrics — `src/data_agent/services/metrics.py`

| 函数 | 位置 | 一句话 |
| --- | --- | --- |
| `match_metric_in` | :33 | 全项目唯一指标匹配实现 |
| `match_metric` | :49 | `match_metric_in(question, METRICS)` 薄封装 |
| `enforce_metric_filters` | :54 | 必需条件强制注入，返回 `(new_sql, enforcement)` |

### time_boundary — `src/data_agent/services/time_boundary.py`

| 函数 | 位置 | 一句话 |
| --- | --- | --- |
| `fix_time_boundaries` | :60 | 时间边界校验改写，返回 `(new_sql, time_fix)` |

### semantic_check — `src/data_agent/services/semantic_check.py`

| 函数 | 位置 | 一句话 |
| --- | --- | --- |
| `check_question` | :157 | 语义拒答判定（纯函数），返回判定字典 |
| `supported_dimensions` | :109 | 某指标支持的维度清单 |

### graph — `src/data_agent/graph/`

| 函数 | 位置 | 一句话 |
| --- | --- | --- |
| `SemanticGateState` | state.py:8 | 图状态：三个键 |
| `semantic_check_node` | nodes/gate_check.py:9 | 只写 `verdict` |
| `semantic_refuse_node` | nodes/gate_refuse.py:11 | 只写 `route="refuse"` |
| `route_after_check` | builder.py:24 | 条件边路由（纯函数） |
| `build_graph` | builder.py:31 | 编译图，`lru_cache` 复用 |
| `run_semantic_gate` | builder.py:46 | 给 `ask()` 用的唯一出口 |

### executor — `src/data_agent/services/executor.py`

| 函数 | 位置 | 一句话 |
| --- | --- | --- |
| `run_query` | :80 | 只读执行 + 行数截断 + 超时中断 |
| `is_result_empty` | :50 | 判结果行是否"没数据"（纯函数） |
| `open_readonly_connection` | :67 | 只读连接 + authorizer 白名单 |
| `QueryTimeout` | :23 | 超时异常 |

### schema / 配置 / CLI / 初始化

| 函数 | 位置 | 一句话 |
| --- | --- | --- |
| `describe_schema` | services/schema.py:12 | 表结构拼成提示词文本 |
| `load_config` | config.py:72 | 读 `.env` 与环境变量 |
| `ensure_db_exists` | config.py:32 | 演示库不存在抛 `ConfigError` |
| `cli.main` | cli.py:102 | 命令行入口，返回退出码 |
| `init_db` | scripts/init_db.py:112 | 建演示库灌样例数据 |

---

## pipeline — `src/data_agent/pipeline.py`

`src/data_agent/pipeline.py`（115 行）只留按顺序调用与提前收尾；每一步怎么写 outcome、怎么打 debug 痕迹在 `pipeline_steps.py`，一函数一步（见上表）。搬家重构（2026-11-05）只挪了位置，语句与行为与瘦身前一致；回炉是阶段 1 新增的第十步。阶段 3 M1a 给 `ask()` 加了可选 `tracer` 入参（`trace = tracer or make_tracer(debug)`，不传时行为不变；判别性测试 `tests/test_pipeline_tracer.py`）。

### `ask`

**签名**：`ask(question, *, generator=None, db_path, max_rows=200, query_timeout_s=5.0, today=None, injected_sql=None, debug=False, tracer=None) -> dict`（pipeline.py:33）

整条链路的唯一入口，评测也只调它。输入一个问题，它依次完成：先做一次语义层拒答判定（撑不住的问题直接拒答、不进模型，见 `run_refusal_gate` 与「graph」模块）-> 读取库的表结构 -> 让大模型写成 SQL -> 按指标定义强制补齐订单范围条件（语义层，见 metrics 模块）-> 按库里字段的真实类型校验时间边界（语义层，见 time_boundary 模块）-> 对这条 SQL 做安全校验 -> 用只读连接执行 ->（执行报数据库错误时）把报错原文带回给模型修一次，修好的 SQL 重走注入/时间修正/guard/执行（阶段 1 报错回炉，见 `retry_after_error`）-> 把结果打包成一个字典返回。

**输入**

| 参数              | 类型         | 人话                                                         |
| ----------------- | ------------ | ------------------------------------------------------------ |
| `question`        | str          | 用户问题                                                     |
| `generator`       | SqlGenerator | "把问题变成 SQL"这个动作本身。评测可以传入桩函数（stub），不碰真实模型 |
| `db_path`         | Path         | 演示库文件在哪                                               |
| `max_rows`        | int          | 最多返回多少行，默认 200，超了会截断                         |
| `query_timeout_s` | float        | 数据库跑多久算超时，默认 5 秒                                |
| `today`           | date \| None | "今天"是哪天。模型要靠它算"上个月"。不传就取 `date.today()`（真实今天）；评测会固定一个日期以便复现 |
| `injected_sql`    | str \| None  | 直接传入一条 SQL，视作模型的产出                             |
| `debug`           | bool         | 打开后按「一步一行」把每步输入与输出打印到 stderr，用于定位问题发生在哪一步；不影响返回值 |

**返回**：一个 `outcome` 字典，字段含义见文末「outcome 字段字典」。

**报错**

- 抛出异常（环境问题，不会变成 status）：`ValueError`——`generator` 与 `injected_sql` 都未提供；`sqlite3.OperationalError`——库文件不存在，读取表结构这一步先报错；`RuntimeError`——库里没有业务表。
- 正常返回、只改 `status`（业务失败）：`ok` / `blocked` / `no_sql` / `exec_error` / `timeout` / `llm_error`，六选一，含义见 README。注意 `no_sql` 现在有两个来源，靠 `error` 分辨：语义层拒答带 `error.type="semantic_refusal"`（模型一次都没调），模型自己回 NO_SQL 时 `error` 仍是 `null`。

**说明·两条内部路线**（`ask` 里的分支）

- 给了 `injected_sql`：**不查表结构、不调模型**，直接使用传入的 SQL。但指标条件注入、时间边界校验、安全校验（guard）和只读执行一步都不少 —— **注入不是绕过校验的通道**。这时如果连 `generator` 也一起传入，`generator` 会被忽略，同时打一条 warning 日志 `generator ignored due to injected_sql`；结果里的 `llm_response` 保持 `null`（因为不存在模型回复）。**唯一例外**：执行出来是空结果时，`llm_response` 会被填成那句「当前条件下没有数据」提示。
- 没给 `injected_sql`：**先跑语义拒答检查**（`run_refusal_gate`），放行后才 `describe_schema(db_path)` 读取表结构，再调 `generator(question, schema_text, 日期)`。这一步的模型调用失败（网络、鉴权等任何异常）不向外传播，收敛为 `status="llm_error"`。

**说明·语义拒答（生成 SQL 之前的一步，语义层第三步·链路上唯一的岔路）**

问题先交给 `graph.builder.run_semantic_gate(question)`，它跑一张只有两个节点、一条条件边的 LangGraph 图：`semantic_check` 节点调纯函数 `check_question(question)` 出判定，条件边按 `allowed` 路由——放行就直接 `END` 交回 `ask()`，拒答才进 `semantic_refuse` 节点。选在这里用图，是因为这是链路上第一次出现真正的岔路；前面几步和后面的生成/注入/时间修正/guard/执行都还是"所有 SQL 都过"的线性写法，一行没动，也不接 checkpointer（单次判定没有断点恢复需求）。

拒答时 `ask()` 立刻收尾：`status="no_sql"`、`sql`/`guard`/`enforcement`/`time_fix`/`result`/`caliber_note` 全为 `null`，拒答文案写进 `llm_response`，缺口原因写进 `error`（`{"type": "semantic_refusal", "message": ...}`）。**没有新增任何 outcome 字段**，评测代码零改动；评测报告每次采样都把 `error` 原样记进 `cases[].runs[].error`，所以拒答原因在报告里看得见，`aggregate` 也用它把"系统机制拒答"从"模型自发拒答"里分出去（见 `evaluation/NOTES.md`）。走 `injected_sql` 时这一步整个跳过——那条通道的问题文本只是标签（S1 的"写一条 SQL，用 load_extension…"根本不是业务问题），拦它会误伤注入用例；SQL 层的两道语义检查不受影响，注入依旧没有绕过校验的特权。判定细节见 `check_question`。

**说明·指标必需条件强制注入（guard 之前的一步，语义层第一步）**

SQL 一旦存在（模型产出或 `injected_sql`，两条路都过），先交给 `metrics.enforce_metric_filters(question, sql)`：问题匹配到指标定义（销售额/订单数）时，把必需条件（订单范围 `status = 'paid'`）强制落到 SQL 上——缺了就用代码补进 WHERE，模型写了冲突取值（如 `status = 'refunded'`）以指标定义为准改写。痕迹写进 `outcome["enforcement"]`，`changed=true` 表示这条 SQL 被系统改过。改写后的 SQL 再进 guard，**注入不享有绕过校验的特权**。问题没匹配到指标时不处理，`enforcement` 为 `null`。细节见 `enforce_metric_filters`。

**说明·时间边界校验（指标注入之后、guard 之前的一步，语义层第二步·小步一）**

订单范围补完，SQL 接着交给 `time_boundary.fix_time_boundaries(sql, db_path=...)`：找出 `BETWEEN` 闭区间与 `<=` 两类**上界**谓词，若该列在库里是日期时间类型（带时分秒），把仅日期的上界改成次日 00:00 的排他上界——`paid_at BETWEEN '2026-09-01' AND '2026-09-30'` 变成 `paid_at >= '2026-09-01' AND paid_at < '2026-10-01'`。K1 第 3 次失败就是这个写法漏掉了 9 月 30 日当天的订单。三条边界要说清：

- **只改边界写法，不替换时间字段**：指标定义里登记的 `time_field` 不参与这道检查，模型写 `paid_at` 修正后还是 `paid_at`（K3 点名按支付时间，语义不变）。
- DATE 字段、上界已带时分（`'2026-09-30 23:59:59'`）、被函数包住的列（`DATE(paid_at) BETWEEN ...`）、严格小于 `<` 本来就是排他上界——都不动。
- 判不出来就一律不动，且**不抛异常**：列在 SQL 引用的表里都不存在、同名列跨表无法确定归属、取值形态不认识、全是 NULL、库读不到、上界不是合法日期，都只记一条 `skipped` 动作。SQL 里压根没有时间边界谓词时连库都不打开，`time_fix` 为 `null`。

痕迹写进 `outcome["time_fix"]`（形状与 `enforcement` 一致：`changed` + `actions[]`），修正后的 SQL 再进 guard。细节见 `fix_time_boundaries`。注意：走 `injected_sql` 时不读 schema 文本，但这条校验要判列类型，仍会用只读连接抽样取值。

**说明·执行后判空（`status="ok"` 分支里的一步）**

SQL 跑成功之后，用 `is_result_empty(result.rows)` 按**本次结果的形状**判断是不是"没数据"：0 行，或每行所有取值都是 NULL。判为空时把「当前条件下没有数据」追加到 `llm_response` 末尾（原来为 `null` 时就直接写这句）。三条约定：

- **不重跑 SQL 去验证**，也不另发一条 `count(*)` 交叉核对——空结果就是空结果。
- `count(*) = 0` 这类**合法的数值不算空**（只认 `None`），否则会把真实的"0 单"答案误标成没数据。
- 这一步不新增 `status` 取值，`ok` 仍是 `ok`；标记写在 `result.empty_result`，见文末字段字典。

### `new_outcome`

**签名**：`new_outcome(question: str) -> dict`（pipeline_steps.py:29）

**输入**：`question` 用户问题原文。

**返回**：outcome 骨架字典——全部字段在这里一次摆齐（`status` 默认 `"ok"`，各痕迹默认 `null`），后续每一步只填自己那块。

**报错**：不抛异常。

### `stamp`

**签名**：`stamp(outcome: dict, started: float) -> dict`（pipeline_steps.py:47）

**输入**：`outcome` 结果字典、`started` 单调时钟起点（`time.monotonic()`）。

**返回**：同一字典，补上 `elapsed_ms`（端到端耗时，不含进程启动）。链路上任何一步提前收尾都走这里。

**报错**：不抛异常。

### `make_tracer`

**签名**：`make_tracer(debug: bool)`（pipeline_steps.py:53）

**返回**：`trace(label, value)` 打印器——`debug=False` 时是空操作；`True` 时把某一步的输入/输出压成一行写到 stderr。**只写 stderr**，`--json` 从 stdout 读到的 JSON 不受影响；不影响返回值。

**报错**：不抛异常。

**说明**：debug 打开时的输出格式：

```text
[ask] question: ...
[ask] refuse: 通过 metric=销售额 dims=（无）（拒答时是 reason=<原因码> terms=<缺的词> metric=... | <完整拒答文案>；走注入时是"跳过（injected_sql）"）
[ask] schema: ...（走注入时这行是"跳过（injected_sql）"）
[ask] sql: ...（模型没给 SQL 时是"无"）
[ask] metric: 指标=销售额 changed=True 缺少必需条件，系统补入 status = 'paid'（未匹配指标时是"未匹配指标，不注入"；SQL 为空时整行不打）
[ask] time: changed=True paid_at 带时分，闭区间上界 '2026-09-30' 会漏掉当天 00:00 之后的数据，改写为 ...（没有可识别的时间边界时是"未识别到时间边界，不修正"；SQL 为空时整行不打）
[ask] guard: passed=... violations=N 规则名(细节) ...
[ask] exec: rows=... elapsed_ms=...（失败时给错误信息）
[ask] exec: empty_result=True | 当前条件下没有数据（只在判为空时补这一行）
[ask] outcome: status=...（只在提前收尾时多这一行）
```

### `run_refusal_gate`

**签名**：`run_refusal_gate(question: str, outcome: dict, trace, *, injected_sql: str | None) -> bool`（pipeline_steps.py:65）

**输入**：问题原文、outcome 骨架、trace 打印器、`injected_sql`（用于判断是否跳过本步）。

**返回**：`True` = 已拒答——`status="no_sql"`、拒答文案、缺口类型已写进 outcome，pipeline 直接收尾；`False` = 放行。拒答发生在模型调用之前——不调模型、不出 SQL，后面的注入/时间修正/guard/执行都不进。

**报错**：不抛异常。

**说明**：`injected_sql` 通道整个跳过（trace 打"跳过"）——那是"SQL 由外部给定、问题文本只是标签"的离线复放通道，问题层检查跳过，SQL 层的两道语义检查照常。判定细节见 `check_question`。

### `resolve_sql`

**签名**：`resolve_sql(question, outcome, trace, *, generator, db_path, today, injected_sql) -> str | None`（pipeline_steps.py:92）

**输入**：问题原文、outcome、trace，关键词参数 `generator` / `db_path` / `today` / `injected_sql`。

**返回**：本轮要处理的 SQL；`None` 表示这一步已经收好 outcome（`llm_error` 或 `no_sql`），pipeline 直接收尾。注入通道直接取 `injected_sql`；模型通道先 `describe_schema` 再调 `generator(question, schema_text, run_date)`，把 `raw_response`/`caliber_note` 写进 outcome。

**报错**：`generator` 与 `injected_sql` 都没给时抛 `ValueError`；模型/网络异常在这一步被接住，收敛为 `status="llm_error"`，不向外抛。

### `enforce_metrics`

**签名**：`enforce_metrics(question: str, sql: str, outcome: dict, trace) -> str`（pipeline_steps.py:137）

**输入**：问题原文 + 当前 SQL。

**返回**：注入处理后的 SQL（调 `metrics.enforce_metric_filters`），同步更新 `outcome["sql"]`，痕迹写 `outcome["enforcement"]`。所有 SQL（含 `injected_sql`）都过这道检查，没有分支。

**报错**：不抛异常。

### `fix_time`

**签名**：`fix_time(sql: str, db_path: Path, outcome: dict, trace) -> str`（pipeline_steps.py:156）

**输入**：指标注入后的 SQL + 演示库路径。

**返回**：时间边界修正后的 SQL（调 `time_boundary.fix_time_boundaries`），同步更新 `outcome["sql"]`，痕迹写 `outcome["time_fix"]`。

**报错**：不抛异常。

### `run_guard`

**签名**：`run_guard(sql: str, outcome: dict, trace) -> str | None`（pipeline_steps.py:172）

**输入**：待校验 SQL。

**返回**：放行时返回 guard 规范化后的 SQL（剥尾部空白与分号，送执行的就是它）；拦截时返回 `None` 并已写 `status="blocked"`。结果记 `outcome["guard"]`。

**报错**：不抛异常（非法 SQL 也只返回 `passed=False`）。

### `execute`

**签名**：`execute(db_path, sql, outcome, trace, *, max_rows, timeout_s) -> None`（pipeline_steps.py:191）

**输入**：库路径、放行的 SQL、`max_rows`、`timeout_s`。

**返回**：`None`。只读执行 + 空结果标记：`status` 按结果落成 `ok` / `timeout` / `exec_error`，结果字典写 `outcome["result"]`，判空提示追加进 `outcome["llm_response"]`。

**报错**：`QueryTimeout` / `sqlite3.Error` 都在这一步接住落成 status，不向外抛。

### `retry_after_error`

**签名**：`retry_after_error(question, outcome, trace, *, generator, db_path, today, failed_sql, db_error, max_rows, timeout_s) -> None`（pipeline_steps.py:230）

**输入**：第一次的执行现场（`failed_sql`、`db_error` 原文）+ 重走链路所需参数（`generator`、`db_path`、`today`、`max_rows`、`timeout_s`）。

**返回**：`None`。把闯祸 SQL 和报错原文拼回问题，请模型修一次（只此一次）；修好的 SQL 没有豁免权，指标注入、时间修正、静态校验、只读执行全部重走，终态落在 outcome 原有字段；第一次现场保存在 `outcome["retry"]`。回炉调用模型本身失败（llm_error）或模型拒绝修改（NO_SQL）时不叠加新错误，终态保持第一次的 `exec_error`，`retry["result"]` 记录回炉的死因。

**报错**：不抛异常。

---

## semantic_tables — `src/data_agent/semantic_tables.py`

语义层的登记表与词表：**纯数据，没有一行逻辑**。想回答"系统认识哪些指标/维度/列/说法"只看这个文件；消费方是 metrics（强制注入判据）、semantic_check（拒答判据）、schema（提示词列说明）。加指标、加维度、补词只改这里。

- `ORDER_SCOPE_PAID`（:15）/ `METRICS`（:22）：指标登记表——名称、同义词、数据表、时间字段（只登记不校验）、口径公式（只登记不校验）、必需过滤条件（订单范围）。
- `DIMENSIONS`（:45）：唯一维度登记表——名称、别名、绑定的列、枚举取值、`applies_to`。
- `COLUMN_NOTES`（:85）：渲染进提示词的中文列说明，键形如 `"orders.amount"`。
- `CALIBER_ALIASES`（:105）/ `ENTITY_NOUNS`（:109）/ `GRAMMAR_WORDS`（:111）/ `TIME_PHRASES`（:120）/ `TIME_PATTERNS`（:126）/ `TIME_HINT_CHARS`（:135）/ `DIMENSION_HINT_WORDS`（:141）/ `SKIPPABLE_CHARS`（:146）：拒答判定"涂色留残片"用的词表与字符集。
- `REASON_CODES`（:146）：`check_question` 结论里 `reason_code` 的全部取值。

---

## llm_client — `src/data_agent/services/llm_client.py`

### `build_generator`

**签名**：`build_generator(cfg: Config) -> SqlGenerator`（llm_client.py:53）

拿配置造出一个"能生成 SQL 的动作"，返回的是个闭包 `generate`。

- 输入：`cfg`（Config），只用里面三个字段 `api_key` / `base_url` / `model`。
- 返回：`generate` 这个闭包。
- 错误：抛出 `openai.*` 异常，例如 api key 非法，构造阶段即抛出，不做吞掉处理。

**说明**：这里有两件容易看不懂的事，说清楚：

1. **基准日期不在造的时候定死，而是每次调用的时候现传。** 这样同一个生成器可以伺候多个锚定日期，评测想换日期不用重新造。
2. **"闭包"不是多余的包装。** pipeline 只认 `SqlGenerator` 这一个签名（`Callable[[str, str, date], GeneratedSql]`）。正因为它认签名不认实现，评测才能整体换成一个桩函数，单独把"安全校验 + 执行"这段链路跑起来 —— 这是 `injected_sql` 之外的第二条注入路。

### `generate`

**签名**：`generate(question, schema_text, today) -> GeneratedSql`（`build_generator` 返回的闭包）

真调一次模型对话接口（chat，`temperature=0`，就是让它别发挥，尽量每次一样），然后把回复里的 SQL 抠出来。这个函数只被 pipeline 调用。

- 输入：`question`（用户的问题）、`schema_text`（上面 `describe_schema` 的输出）、`today`（填进系统提示词里的 `{today}`，决定"上个月"是哪个月）。
- 返回：`GeneratedSql`，三个字段 —— `sql`（字符串或 `None`）、`raw_response`（模型原始回复）、`caliber_note`（代码块之外的口径声明，没有则 `None`）。
- 错误：模型/网络异常**一律不吞**，向上传播，由 pipeline 收敛为 `status="llm_error"`。

### `extract_sql`

**签名**：`extract_sql(text: str) -> str | None`（llm_client.py:31）

从模型的一堆话里把 SQL 挑出来。纯函数，不碰外部世界。

- 输入：`text`，模型的原始回复。
- 返回：优先取 markdown 代码块内的内容（模型习惯用三个反引号包裹）；没有代码块则把整段文本当作 SQL 返回。返回 `None` 只有两种情况：文本为空，或以 `NO_SQL` 开头（模型明确表示无法回答）。
- 错误：不抛异常。

### `extract_caliber_note`

**签名**：`extract_caliber_note(text: str) -> str | None`（llm_client.py:43）

把代码块**之外**的文字当作口径声明取出来，与 `extract_sql` 互为补集。纯函数。

- 输入：`text`，模型的原始回复。
- 返回：有代码块时，取剥掉代码块剩下的文字（剥完为空就是模型没声明 -> `None`）；**没有代码块时直接返回 `None`**，因为这时 SQL 和说明分不开，不能把 SQL 本身当口径交出去。
- 为什么缺声明要返回 `None` 而不是空串：评测与 CLI 要能区分“模型没说”和“说了但说空了”。
- 错误：不抛异常。

---

## sql_text — `src/data_agent/services/sql_text.py`

SQL 文本扫描统一底座：一次分段，三份投影。二期 2a 把原先 metrics / time_boundary / sql_guard 各自养的扫描循环合并到这里（`_scan_segments` :53 认出字符串、引号标识符、方括号标识符、行注释、块注释）；二期 2c 又把 time_boundary 里的通用文本工具下沉过来；阶段 4 加了 `split_query_blocks`（切查询块，KD-001 修复的地基）。

### `SqlScanError`

**签名**：`class SqlScanError(ValueError)`（sql_text.py:34）

**统一口径（用户 2026-11-05 确认）**：引号/方括号/块注释未闭合（阶段 4 起还包括括号不配对）= 整句判读不懂，各投影与 `split_query_blocks` 一致抛错；消费方接到一律不动 SQL（metrics 记 `skipped`、time_boundary 记 `skipped`、guard 收敛成 `malformed_sql`）。合并前三份实现各有各的口径（不遮 / 后面整段保护 / 抛错），现在只剩这一条。

### `mask_sql`

**签名**：`mask_sql(sql: str) -> str`（sql_text.py:92）

**输入**：任意 SQL 文本。**返回**：与原文**等长**的空格遮罩（字符串/引号标识符/注释区换成空格，引号内空白保留），metrics 定位子句关键字用。**报错**：未闭合抛 `SqlScanError`。

### `protected_spans`

**签名**：`protected_spans(sql: str) -> list[tuple[int, int]]`（sql_text.py:108）

**返回**：受保护区间清单（字符串字面量、引号标识符、注释在原文中的 `(start, end)`），time_boundary 跳过假谓词用。**报错**：未闭合抛 `SqlScanError`。

### `scan_views`

**签名**：`scan_views(sql: str) -> tuple[str, str]`（sql_text.py:113）

**返回**：`(structure, calls)` 结构/调用两份折叠视图（不保长度、位置不与原文对应），sql_guard 词法检查用。**报错**：未闭合抛 `SqlScanError`。

### `in_spans`

**签名**：`in_spans(spans: list[tuple[int, int]], pos: int) -> bool`（sql_text.py:150）

**输入**：区间清单 + 一个原文位置。**返回**：该位置是否落在任一区间内。与具体规则无关的谓词定位工具，time_boundary / metrics 消费。**报错**：不抛异常。

### `is_bare_column`

**签名**：`is_bare_column(sql: str, pos: int) -> bool`（sql_text.py:178）

**返回**：该位置的列是不是"裸列"——被函数包裹的列不算（词表 `_GROUPING_WORDS`：括号前是 SQL 关键字算分组括号，仍算裸列）。time_boundary 消费。**报错**：不抛异常。

### `referenced_tables`

**签名**：`referenced_tables(sql: str) -> list[tuple[str, str | None]]`（sql_text.py:255）

**返回**：FROM/JOIN 后的 `(表名, 别名)` 清单（别名可能为 `None`；负向预查词表 `_NOT_ALIAS` 防把关键字认成别名），time_boundary 用它把列归位到表。**报错**：未闭合抛 `SqlScanError`。

### `split_query_blocks`

**签名**：`split_query_blocks(sql: str) -> list[dict]`（sql_text.py:269）

**输入**：SQL 原文。

**返回**：查询块清单（文档序：外层在前、内层在后），位置全部对原文成立。块 = 一个 SELECT 作用域：顶层语句一块；每对括号内首个关键字是 SELECT 就再切一块（派生表、CTE 支、`IN (SELECT ...)` 都算）；函数括号、条件括号不是块。每块字段：

| 键 | 含义 |
| --- | --- |
| `start` / `end` | 块正文区间（顶层为整句；子块不含外层括号） |
| `depth` | 块内代码的括号深度（顶层 0，一层子查询 1） |
| `tables` | 本块 FROM/JOIN **直接**引用的表名集合（小写，不含子查询别名引用） |
| `own_spans` | 本块正文扣掉所有子块（含其括号）后的自有区间清单 |
| `is_top` | 是否顶层块 |

**报错**：括号不配对与引号未闭合同口径——抛 `SqlScanError`，调用方"读不懂不动手"。

**说明**：阶段 4（KD-001 修复主刀）新增；消费方是 `metrics._ensure_filter`——口径条件的判定与插入点限定在直接引用指标表的那个块内。

---

## sql_guard — `src/data_agent/services/sql_guard.py`

### `check_sql`

**签名**：`check_sql(sql: str) -> GuardResult`（sql_guard.py:106）

判断"这条 SQL 是不是单条、只读的 SELECT"，并把不合格的原因一条条列出来。

**说明·判定方法**（这次改动里最不直观的一块，值得说明白）：扫一遍 SQL（`sql_text.scan_views`），产出两份文本，各管各的。

- **结构视图**：把引号包起来的标识符折成一个占位符 `ident`。这份用来查关键字 —— 所以 `SELECT 'delete'` 里的 `delete` 不会被当成删除操作误杀。
- **调用视图**：保留标识符的真实内容。这份用来查函数调用 —— 所以 `SELECT "load_extension"(...)` 这种想用引号规避校验的写法照样算一次函数调用。

两份文本各判各的，**不做跨文本的位置对应**（不要尝试推断"结构视图第 N 个字符对应原文哪个位置"，代码里没有这种对应关系）。

- 输入：`sql`，模型产出的 SQL，允许空串。
- 返回：`GuardResult(passed, violations, sql)`
  - `passed`：放不放行。
  - `violations`：每条一个字典，`rule`（规则名）、`detail`（人话说明）、`token`（命中的关键字/函数名，有的规则没有这个键）。
  - `sql`：**去掉末尾分号后的规范化语句**，真正送去执行的是它，不是你传进来的原文。
- 错误：不抛异常。非法输入也只返回 `passed=False`。引号/注释未闭合走 `sql_text.SqlScanError`，在这里收敛成 `malformed_sql` violation。

### `GuardResult.as_dict`

**签名**：`as_dict() -> dict`（`GuardResult` 类，sql_guard.py:52）

**返回**：准备写入 outcome 的版本，只有 `{"passed", "violations"}` 两个键，**不含 `sql`**。**报错**：不抛异常。

### `strip_trailing_semicolons`

**签名**：`strip_trailing_semicolons(sql: str) -> str`（sql_guard.py:72）

把尾巴上的空白和分号抹掉。

为什么单拎一个函数出来：`check_sql` 用它产出送执行的 `sql`，executor 在包 `LIMIT` 之前也调它。**两处共用同一份"什么叫做完尾"的语义**，不会出现一处认为干净、另一处认为没写完。

- 输入：任意字符串。返回：去掉尾部 `;` 和空白后的语句（等价于"去尾分号再 `strip`"）。**字符串字面量、注释里的分号不动**，只处理末尾。
- 错误：不抛异常。

---

## metrics — `src/data_agent/services/metrics.py`

语义层第一步：**指标定义 + 订单范围强制注入**。纯文本处理，不碰数据库。存在的理由：同一个问题模型有时写 `status='paid'` 有时不写（K1 三次只过一次），必需条件不能再靠模型自觉。

### `METRICS`

登记表在 `semantic_tables.py:22`，本模块 import 后消费—— 指标登记表，现有销售额、订单数两条。每条记录：`name`、`synonyms`、`table`（数据表）、`time_field`（时间字段，本步只登记不校验）、`formula`（口径公式，本步只登记不校验）、`required_filters`（必需过滤条件，现只有订单范围 `status = 'paid'`，取值 `ORDER_SCOPE_PAID`，`semantic_tables.py:15`）。

### `match_metric_in`

**签名**：`match_metric_in(text: str, metrics: dict) -> dict | None`（metrics.py:33）

按指标名/同义词在文本里找指标（大小写不敏感），多个命中取最长词。没匹配到返回 `None`。**全项目唯一的指标匹配实现**（二期 2b 去重）：本模块的 `match_metric(question)` 只是 `match_metric_in(question, METRICS)` 的薄封装（metrics.py:49），`semantic_check` 的拒答判定也 import 这同一个函数（`test_semantic_check::test_metric_matcher_is_a_single_implementation` 钉住）。**报错**：不抛异常。

### `enforce_metric_filters`

**签名**：`enforce_metric_filters(question: str, sql: str) -> tuple[str, dict | None]`（metrics.py:54）

SQL 生成后、guard 前的强制检查。

- 输入：问题原文 + 模型产出（或 `injected_sql`）的 SQL。
- 返回：处理后的 SQL 与记录字典；问题没匹配到指标（或 SQL 为空）时返回原文和 `None`。
- 记录形状：`{"metric", "required_filters", "changed", "actions": [...]}`。
- `actions[].type` 四种：
  - `added` —— 直接引用指标表的查询块里本来没有必需条件，系统补进**那个块自己的** WHERE（有 WHERE 时原条件整体加括号再 AND，防 `OR` 被抢优先级；没有 WHERE 就插在块内 GROUP BY/HAVING/ORDER BY/LIMIT 之前；补进非顶层块时 detail 追加位置说明）。
  - `conflict_replaced` —— 块内谓词写了冲突取值（`status='refunded'`、`status IN ('paid','refunded')`、`<>`/`!=`、`NOT IN` 等排除式），**以指标定义为准就地改写**，记录带 `from`/`to`；列别名保留（`o.status` 改写后还是 `o.status`）。
  - `already_present` —— 必需条件已在（`status='paid'` 或 `status IN ('paid')`），不动 SQL。
  - `skipped` —— 没注入并写明原因：SQL 未引用该指标的数据表；多个查询块直接引用指标表、条件位置不唯一；无法把表引用归位到查询块；文本形态找不到安全插入点；或整句读不懂（引号/方括号/块注释未闭合、括号不配对，`sql_text.SqlScanError`——此时原 SQL 一字不动返回）。
- `changed=true` 只可能是 `added` / `conflict_replaced` —— 评测与调试据此分辨"条件是谁写进 SQL 的"。
- **实现是块感知 + 文本级手术（阶段 4 / KD-001 修复主刀）**：`split_query_blocks` 切查询块，目标块 = `tables` 直接含指标表的块，**唯一才动手**；谓词只有落在目标块 `own_spans` 内才参与 `already_present`/冲突判定，块外错误位置谓词不计数、不改动（那是模型的产物，交执行报错与回炉处理——注入不删模型谓词是阶段 4 裁决）。补写经 `_append_where_condition(sql, fragment, block)`（metrics.py:209）落进目标块自己的 WHERE；顶层形态下与旧文本级行为逐字等价。定位用 `sql_text.mask_sql`（等长掩码）；谓词正则直接扫原文，极端情况（字面量里恰好出现 `status='x'`）可能误判，演示范围接受。**无法识别形态时宁可不动，不做危险猜测。**
- 错误：不抛异常（`SqlScanError` 在内部接住，落成 `skipped`）。

---

## time_boundary — `src/data_agent/services/time_boundary.py`

语义层第二步·小步一：**时间边界校验**。存在的理由：K1 第 3 次失败不是字段选错，是 DATETIME 字段用了 DATE 的闭区间写法，`BETWEEN '2026-09-01' AND '2026-09-30'` 把 9 月 30 日当天带时分的订单整段漏掉。与 metrics 的分工：metrics 管"条件缺不缺"，本模块管"边界写法对不对"，两道痕迹分别记在 `enforcement` 与 `time_fix`。

### `fix_time_boundaries`

**签名**：`fix_time_boundaries(sql: str, *, db_path: Path) -> tuple[str, dict | None]`（time_boundary.py:60）

- 输入：SQL（指标注入**之后**的版本，所以看到 WHERE 里已经有系统补的 `status = 'paid'`）+ 演示库路径（只用来判列类型，用 `file:...?mode=ro` 只读连接）。
- 返回：`(处理后的 SQL, 记录字典)`。SQL 里没有可识别的时间边界谓词时返回 `(原文, None)`，**这时不打开数据库**。
- 记录形状：`{"changed", "actions": [...]}`；`changed=true` 只可能是 `boundary_rewritten`。
- `actions[].type` 三种：
  - `boundary_rewritten` —— DATETIME 字段的仅日期闭区间上界改成半开区间，带 `from`/`to`；`BETWEEN` 展开成 `col >= '下界' AND col < '次日'`，`col <= '月末'` 改成 `col < '次日'`。别名前缀保留（`o.paid_at` 改写后还是 `o.paid_at`）。
  - `already_correct` —— 写法本来就对，没动：列判出是 DATE 类型、或上界字面量已带时分（`'2026-09-30 23:59:59'`）。
  - `skipped` —— 没修正并写明原因：列在 SQL 引用的表里都不存在、同名列跨表无法确定归属、别名对不上任何表、取值形态不认识、全是 NULL、库读不到（`sqlite3.Error` 兜住）、上界不是合法日期（`date.fromisoformat` 报错），或整句读不懂（引号/方括号/块注释未闭合，`sql_text.SqlScanError`——判读不懂时连库都不开）。
- 不抛异常：数据库不可读、列/表找不到都只落成 `skipped`。改写发生在 guard 之前，修正后的 SQL 照常过静态校验与只读执行。

**说明·内部怎么判"这个字段是不是 DATETIME"**（`_resolve_kind` :190、`_kind_of` :217）：

1. 先定位列属于哪张表：`sql_text.referenced_tables`（sql_text.py:255）抓 FROM/JOIN 后的 `(表名, 别名)`；谓词写了 `o.paid_at` 就按别名对表，没写别名就在 SQL 引用的表里找有这一列的——**多张表都有同名列时不猜**，直接 `skipped`。
2. 列的声明类型含 `DATETIME`/`TIMESTAMP`/`TIME` -> datetime；以 `DATE` 开头 -> date（顺序不能颠倒，`DATETIME` 里也含 `DATE`）。
3. SQLite 存日期时间的常态是声明成 `TEXT`，声明给不出信息时**抽样真实取值**（`_sample_values` :258，最多 `_SAMPLE_LIMIT` = 200 条非空值）：带 `HH:MM` 的是 datetime，只有 `YYYY-MM-DD` 的是 date，出现别的形态或非文本取值 -> unknown -> `skipped`。
4. 类型判定依据写进动作的 `detail`（"抽样 200 条 orders.paid_at 取值，带时分"），报告里能看到结论是怎么来的。

**说明·谓词识别的三个"不动"**（`_iter_predicates` :100）：

- 落在字符串字面量、引号标识符、注释里的假谓词不算（`sql_text.protected_spans`，sql_text.py:108 给出保护区间；扫描抛 `SqlScanError` 即整句判读不懂，直接 `skipped`）。
- 被函数包住的列不算（`sql_text.is_bare_column`，sql_text.py:178）：`DATE(paid_at) BETWEEN ...` 已经是正确写法，不能二次改写；`WHERE (paid_at BETWEEN ...)` 里的左括号是分组括号，仍算裸列——靠括号前是不是 SQL 关键字区分（`_GROUPING_WORDS`）。
- 只认闭区间上界：`BETWEEN` 的上界和 `<=`。`<` 本来就排他、`>=` 是下界，都不在射程内，系统不代替模型改口径。

一处代价要说明：谓词正则直接扫原文（同 metrics 的取舍），极端情况（字面量里恰好出现 `paid_at BETWEEN '...' AND '...'`）可能误判，演示范围接受。表名/列名取自 SQL 文本，只用于拼只读查询的标识符，并按双引号转义（`_quote` :275）。

**说明·方向性**：这道修正只会"把范围放宽到用户本来想要的整月"，不会缩窄。即使把 DATE 列误判成 datetime（比如 `FROM a, b` 逗号连接没被识别、取错了表），`col >= '2026-09-01' AND col < '2026-10-01'` 对纯日期取值与 BETWEEN 完全等价，结果不变。

**说明·待立项**：本模块目前仍是文本级扫描、不认识查询块（与 KD-001 修复前的 metrics 同病灶）；块感知改造已登记待立项，见文末「阶段承诺」。

---

## semantic_check — `src/data_agent/services/semantic_check.py`

### `check_question`

**签名**：`check_question(question: str, *, metrics=None, dimensions=None) -> dict`（semantic_check.py:157）

生成 SQL 之前的语义拒答判定。**纯函数**：只读问题文本和两份静态登记表，不调模型、不连数据库，所以能单测（`tests/test_semantic_check.py` 直接拿 `problems/eval_cases.json` 的 9 条非注入用例问题文本核对）。

**输入**：`question` 用户问题；`metrics` / `dimensions` 只有测试会传（构造"某维度只对某指标可用"这类替身登记表），生产路径用 `semantic_tables.py` 里的 `METRICS`（:22）与 `DIMENSIONS`（:45）。

**返回**：判定字典，键固定这些——

| 键 | 含义 |
| --- | --- |
| `allowed` | bool，True 才继续走生成 |
| `reason_code` | `None` 或 `empty_question` / `missing_metric` / `missing_dimension` / `dimension_not_supported` / `unknown_qualifier` |
| `metric` | 匹配到的指标名（可能为 `None`，与 `residues` 一起决定原因码） |
| `matched_dimensions` / `unsupported_dimensions` | 命中的维度、以及命中但 `applies_to` 不含当前指标的维度 |
| `residues` | `[{term, kind}]`，没被词表解释掉的缺口；`kind` = `dimension`（含"地区/省份/产品线"这类拆分指示词）或 `qualifier` |
| `dimension_values` / `time_terms` / `ignored_fragments` | 三条放行规则各自放过了什么，留痕给 debug 用，不参与拒答 |
| `message` | 拒答文案，点名缺哪个词 + 列出当前支持的指标与维度；放行时为 `None` |

**报错**：**不抛异常**——判不出缺口就放行；唯一例外是空问题，返回 `allowed=False` + `empty_question`。

**说明·判据是反向覆盖，不是正向匹配**：词表把问题"涂色"（最长优先），没被涂到的连续片段就是缺口。R3「上个月新客销售额」能正常匹配到指标「销售额」，所以只看"有没有匹配到指标"永远抓不到它——缺口在残片里。词表来源（都在 `semantic_tables.py`）：指标别名（`METRICS`，:22）、口径词 `CALIBER_ALIASES`（:105，来自销售额定义里已有的含税/不含税两种算法）、维度别名与枚举取值（`DIMENSIONS`，:45）、时间表达 `TIME_PHRASES`（:120）/`TIME_PATTERNS`（:126）、业务对象与语法词 `ENTITY_NOUNS`（:109）/`GRAMMAR_WORDS`（:111）。指标匹配（`match_metric_in`）不自建实现，import 自 `services/metrics.py`——拒答与强制注入共用同一份判据（二期 2b 去重，见 metrics 模块小节）。

**说明·三条放行规则**（`_residue_runs` :78 之后逐段判，宁可放过不误杀）：残片等于某个已登记的枚举取值放过；残片与维度别名紧邻（`_touches_dimension` :99，"抖音渠道"里的"抖音"）当取值放过——取值合不合法属于下一步的"维度取值校验"，本步不管；整段残片的每个字都落在 `TIME_HINT_CHARS`（semantic_tables.py:135）里就当时间表达放过（`前三个月`、`过去半年`、`今年以来`）。收紧到"整段都像时间"而不是"沾一个时间字符就放"，否则「月度环比」会一起漏过去。单字残片（口语尾巴"怎么样"剩下的"样"）只进 `ignored_fragments`，不做拒答依据。

### `supported_dimensions`

**签名**：`supported_dimensions(metric_name: str | None, dimensions: dict | None = None) -> list[str]`（semantic_check.py:109）

某指标支持的维度清单，从 `DIMENSIONS` 按 `applies_to` 反查。**维度只有一张登记表**，`METRICS` 里不另存维度清单，所以加维度只改一处、不可能两处不一致；`tests/test_semantic_check.py` 还双向核对"登记的列必须存在于 `COLUMN_NOTES`""能当维度的列必须已登记"。**报错**：不抛异常。

---

## graph — `src/data_agent/graph/`

只装"检查 → 放行/拒答"这一个岔路，别的一步都不在这里。

### `SemanticGateState`

**签名**：`class SemanticGateState(TypedDict)`（state.py:8）

图状态：`question` / `verdict` / `route` 三个键，每个键只有一个节点写。

### `semantic_check_node`

**签名**：`semantic_check_node(state: SemanticGateState) -> dict`（nodes/gate_check.py:9）

只写 `verdict`（调 `services/semantic_check.check_question` 的判定结果），不路由、不写文案。

### `semantic_refuse_node`

**签名**：`semantic_refuse_node(state: SemanticGateState) -> dict`（nodes/gate_refuse.py:11）

只写 `route="refuse"`，不重复拼文案，也不产 SQL。

### `route_after_check`

**签名**：`route_after_check(state: SemanticGateState) -> str`（builder.py:24）

条件边路由，**纯函数**，只读 `state["verdict"]["allowed"]` 返回 `generate` / `refuse`。

### `build_graph`

**签名**：`build_graph()`（builder.py:31，`lru_cache` 编译一次复用）

返回编译好的 LangGraph 图：`START -> semantic_check ->（条件边）refuse 节点 / END`，`semantic_refuse -> END`。没有 checkpointer（单次判定没有断点恢复需求）。

### `run_semantic_gate`

**签名**：`run_semantic_gate(question: str) -> dict`（builder.py:46）

给 `pipeline.ask()` 用的唯一出口。

**输入**：`question` 用户问题。**返回**：图的最终状态（判定读 `verdict["allowed"]`）。**报错**：不抛异常（判定本身不抛，见 `check_question`）。

**说明**：判定逻辑不在图里，在 `services/semantic_check.py`，所以单测不装 LangGraph 也能测判定本身。

---

## executor — `src/data_agent/services/executor.py`

### `run_query`

**签名**：`run_query(db_path, sql, *, max_rows=200, timeout_s=5.0) -> QueryResult`（executor.py:80）

三件事：只读执行、行数截断、超时中断。

- 输入：`db_path`（用只读 URI 打开，写操作无效）、`sql`（**已通过安全校验的语句；安全校验不是它的职责**，它不判断这条 SQL 是否危险）、`max_rows`（在外层再套一层 `LIMIT`）、`timeout_s`（超时即中断）。
- 开头先查库文件在不在，不在直接 `FileNotFoundError`。
- 返回：`QueryResult(columns, rows, truncated, max_rows, elapsed_ms)`；`as_dict()`在上面这几个之外多给两个派生键：`row_count = len(rows)` 与 `empty_result = is_result_empty(rows)`。注意 `empty_result` **不是** `QueryResult` 的字段，是序列化时算出来的。
- 错误：
  - `QueryTimeout` —— 超时。pipeline 接住，变成 `status="timeout"`。
  - `sqlite3.Error` —— 表不存在、写操作被拒等。pipeline 接住，变成 `status="exec_error"`。
  - `FileNotFoundError` —— 库不存在。**pipeline 不收敛成 status，直接向上传播**。正常调用模型的链路走不到这里（读取表结构那步先报错）；只有走 `injected_sql` 跳过表结构时才可能从那里抛出。

### `is_result_empty`

**签名**：`is_result_empty(rows: list[dict]) -> bool`（executor.py:50）

判断这批结果行是不是"没数据"。纯函数，不碰数据库，被 `as_dict()` 和 pipeline 共用同一份判空语义。

- 输入：`rows`，已转成 `{列名: 值}` 的行列表（即 `QueryResult.rows`）。
- 返回：`True` = 空结果，两种形态——0 行，或每行的每个取值都是 `None`（SQL 的 NULL，聚合查询没匹配到行的典型样子）。其余一律 `False`。
- **口径要说清**：只认 `None`。`0`、`0.0`、空串都算"查出来的值"，不判空——否则 `count(*) = 0` 这种正确答案会被误标成没数据。评测层的 `result_empty_or_null` 断言口径更宽（把 0 也当空），两者不是一套，别改错地方。
- 错误：不抛异常。

### `_on_progress`

**签名**：`_on_progress() -> int`（`run_query` 内部闭包）

SQLite 的"进度回调"（progress handler），本质是个心跳探针：数据库每执行一小段就回头问一次"该停了吗"。它比对时间戳，到点了返回 1 表示"中断"，否则返回 0 表示"继续"。

- 输入：无参数，读取闭包内的两个变量 `deadline` / `timed_out`。
- 返回：0 继续 / 1 请求中断。
- 错误：不抛异常。

### `open_readonly_connection`

**签名**：`open_readonly_connection(db_path: Path) -> sqlite3.Connection`（executor.py:67）

造一个只读连接：只读 URI + 关掉 `load_extension` + 挂上 authorizer 白名单（只放行读类动作，写、PRAGMA、ATTACH、事务一律拒绝）。

评测想单独验证"写操作必然失败"，直接用这个函数就行，不用绕整条链路。

- 输入：`db_path`。
- 返回：已配好安全设置的连接对象。
- 错误：`sqlite3.OperationalError`（库文件不存在）；`sqlite3.Error`（用该连接执行写、PRAGMA 等非白名单操作）。

---

## schema — `src/data_agent/services/schema.py`

### `describe_schema`

**签名**：`describe_schema(db_path: Path) -> str`（schema.py:12）

把库里有哪些表、哪些列、什么类型、中文备注拼成一段文本，这段文本会原样写入给模型的提示词（模型只看这段文字来写 SQL，它看不到库）。

- 输入：`db_path`。
- 返回：一段多行文本，形如 `"表 orders:\n  - amount REAL  -- 订单金额（元，含税）"`。
- 错误：`sqlite3.OperationalError`（库文件不存在）；`RuntimeError`（库里没有业务表）。
- 备注从哪来：查 `COLUMN_NOTES`（`semantic_tables.py:85`），键形如 `"orders.amount"`。

---

## 配置 / CLI / 初始化

### `load_config`

**签名**：`load_config(env_file=None, *, require_llm=True) -> Config`（config.py:72）

读项目根的 `.env`，系统环境变量优先级更高。

- 输入：`env_file`（默认项目根 `.env`）、`require_llm`。
- 返回：`Config`，包含 `api_key` / `base_url` / `model` / `db_path` / `max_rows` / `query_timeout_s`。
- 错误：`ConfigError` —— `require_llm=True` 且缺 `OPENAI_API_KEY` / `DATA_AGENT_MODEL`；或者数值项非法、`<= 0`（`DATA_AGENT_MAX_ROWS`、`DATA_AGENT_QUERY_TIMEOUT`）。
- **说明**：`require_llm=False` 是为"只走 `injected_sql` 的评测"预留的路径：**跳过 key 和模型名的必填校验**。此时这两个字段可能是空字符串，**不得拿它去构造 LLM 客户端**。

### `ensure_db_exists`

**签名**：`ensure_db_exists(config: Config) -> None`（config.py:32）

演示库不存在就抛 `ConfigError`。CLI 和评测 runner 用同一句文案，对应 README 里的退出码 2。

- 输入：`config`，只读取它的 `db_path`。
- 返回：`None`。库存在时不做任何事。
- 错误：`ConfigError`。
- 为什么这个检查不放进 `load_config`：**让"解析配置"这件事不碰磁盘**，两件事分开，配置本身是否合法跟库在不在没关系。

### `cli.main`

**签名**：`cli.main(argv: list[str] | None = None) -> int`（cli.py:102）

命令行入口。默认打印面向人的排版结果（`render`，cli.py:48），`--json` 打印结构化 outcome，`--debug` 开启上述逐步打印。

- 输入：`argv`，位置参数是问题（可以拆成多个词），可选 `--json`、`--debug`。
- 返回：0 = 本次查询已处理（**包括被拦下的 `blocked`**，处理成功不等于查到数据）；2 = 配置不满足或演示库不存在。
- 口径声明在 SQL 区块之后另起一段「口径声明:」，只打印 `caliber_note` 的原文；`caliber_note` 为 `null`（模型没声明、或走注入）时整段不打，不留空壳。
- 语义层改过 SQL（`enforcement.changed=true`）时，再打一段「指标强制（指标名）:」，逐条列出 `added` / `conflict_replaced` 的动作说明；没改动（`already_present` / `skipped` / 未匹配指标）不打空区块。
- 时间边界被系统改写过（`time_fix.changed=true`）时，再打一段「时间边界修正:」，逐条列出 `boundary_rewritten` 的说明；没改动（`already_correct` / `skipped` / 没有时间条件）不打空区块。
- 空结果时，人类可读输出在表格与统计行之后多打一行「提示: 当前条件下没有数据（空结果），这不是一个真实数值。」；`--json` 不需要专门改，`result.empty_result` 与 `llm_response` 里的提示会随 outcome 一起序列化出去。
- 错误：一般不抛。库里没有业务表时，`describe_schema` 的 `RuntimeError` 会直接向上传播。

### `init_db`

**签名**：`init_db(db_path: Path, force: bool = False, today: date | None = None) -> dict`（scripts/init_db.py:112）

建演示库并灌进样例数据。

- 输入：`db_path`（输出位置）、`force`（目标已存在时是否重建）、`today`（数据往前倒推的基准日，可固定日期以便复现）。
- 返回：`{"db_path", "customers", "orders", "months"}`。
- 错误：`FileExistsError` —— 目标已存在且 `force=False`。

---

## outcome 字段字典（`ask` 的返回值和 `--json` 是同一份）

字段名和枚举取值**就是评测的契约**。改了代码必须同步 README 和本文件。

```text
question      str             原始问题
status        str             ok | blocked | no_sql | exec_error | timeout | llm_error
sql           str | null      送执行的 SQL；blocked 时是被拦下的原文；no_sql（含语义层拒答）/llm_error 为 null
guard         dict | null     {passed, violations}；no_sql/llm_error 时压根没校验 -> null
  rule        str             empty_sql | malformed_sql | multiple_statements
                              | not_select | forbidden_keyword | forbidden_function
  detail      str             中文说明
  token       str             命中的关键字/函数名，部分规则没有这个键
enforcement   dict | null     语义层订单范围强制注入的记录 {metric, required_filters,
                              changed, actions[]}；问题没匹配到指标（或 SQL 为空/拒答）时是 null。
                              actions[].type = added（缺了被系统补上）| conflict_replaced
                              （模型写了冲突取值，以指标定义为准改写，带 from/to）| already_present
                              （模型写了必需条件，没动）| skipped（没注入，原因写在 detail）。
                              changed=true 表示送去执行的 SQL 被系统改过，不是模型的原文
time_fix      dict | null     语义层时间边界校验的记录 {changed, actions[]}；SQL 里没有可识别的
                              时间边界谓词时是 null（这时连库都不打开）。
                              actions[].type = boundary_rewritten（DATETIME 字段的仅日期闭区间
                              上界改成半开区间，带 from/to，例如 paid_at BETWEEN '2026-09-01'
                              AND '2026-09-30' -> paid_at >= '2026-09-01' AND paid_at < '2026-10-01'）
                              | already_correct（列是 DATE 类型、或上界已带时分：写法本来就对，没动）
                              | skipped（列找不到 / 跨表无法确定归属 / 类型判不出 / 库读不到 /
                              上界不是合法日期，原因写在 detail）。
                              只改边界写法，时间字段仍是模型选的那个；changed=true 表示送去执行
                              的 SQL 被系统改过边界，不是模型原文
result        dict | null     只有 status=ok 时非空
  columns     list[str]       列名
  rows        list[dict]      每行 {列名: 值}，SQL 里的 NULL 变成 None
  row_count   int             截断后的实际行数，不等于库里总行数
  empty_result bool           true = 没数据（0 行，或每行所有取值都是 None）；只按本次
                              结果形状判定，不重跑 SQL。count(*)=0 这类合法数值是 false
  truncated   bool            true = 撞到行数上限，后面还有行没返回
  max_rows    int             这次生效的上限
  elapsed_ms  float           数据库自己跑了多久
error         dict | null     {type, message}；type = semantic_refusal | sql_error | timeout | llm_error
                              semantic_refusal = 语义层拒答（status 仍是 no_sql，但模型一次都没调），
                              message 就是拒答文案，说清缺的是指标还是维度、缺哪个词
llm_response  str | null      模型原始回复；语义层拒答时这里是系统写的拒答说明（与 error.message 同文）；
                              llm_error 时是 null；走 injected_sql 时为 null，
                              但空结果例外（下两条）
                              · empty_result=true 时，这里会追加一行「当前条件下没有数据」
                              · 走 injected_sql 且空结果时，整个字段就是这句提示
caliber_note  str | null      模型在 ```sql 代码块之外给的一句口径声明（金额口径含税/不含税、
                              按哪个时间字段、paid/refunded/cancelled 算不算），由
                              extract_caliber_note 抽取；模型只回了 SQL、回 NO_SQL、
                              或走 injected_sql 时都是 null。llm_error 时同样是 null
retry         dict | null     报错回炉（阶段 1）的痕迹。触发条件：LLM 路径 + 第一次执行报
                              数据库错误（exec_error）。没触发（一次成功 / 拒答 / timeout /
                              injected_sql 通道）时是 null。
                              形状 {attempted: true, failed_sql, db_error, regenerated_sql, result}：
                              failed_sql/db_error = 第一次的现场原文；regenerated_sql = 修复返回的
                              SQL（模型拒绝修改 NO_SQL 或回炉调用挂了时 null）；
                              result = ok（救回）| exec_error（修了还是错）| blocked（修好的 SQL
                              没过安检，此时顶层 error 清空，报错原文只在 retry 里）| timeout |
                              no_sql | llm_error。
                              最多回炉一次；修好的 SQL 重走注入/时间修正/guard/执行，顶层
                              sql/guard/enforcement/time_fix/result/llm_response/caliber_note
                              反映的都是最后一次处理，第一次现场全在 retry 里
elapsed_ms    float           端到端耗时（不含进程启动）
```

注入这条路**不因注入本身给 outcome 添字段**：`injected_sql` 只体现在 `sql` 上。值差异只有两处：空结果提示会写进 `llm_response`（模型没回复就没有该字段内容，那字段本来为 null），`caliber_note` 同理恒为 null——没有模型回复就没有声明。另注意：注入的 SQL 同样要过语义层两道检查，缺订单范围条件照样会被补上并记进 `enforcement`、DATETIME 字段的闭区间上界照样会被改写并记进 `time_fix`——那是这两个字段的常规行为，不是注入路径的新字段。`time_fix` 是语义层第二步带来的常规字段（模型路径和注入路径都有），与注入无关。

评测层（读用例 / 判 outcome / 聚合报告）的接口地图在 `evaluation/NOTES.md`。

---

## 阶段承诺（退出码拦什么）

- **v1 于 2026-11-04 转 done，此后 v1 用例失败视为回归，需修复或显式降级并记录原因。**（转的时候 9 条 v1 用例 `--repeats 3` 全过；改状态前后各跑过一次 `--all --ids K1`：同一条被人为改坏的 K1 断言，v1=pending 时退出码 0、v1=done 时退出码 1。）
- **v2 于 2026-11-06 转 done（毕业）**：毕业两轮 `--stage v2 --repeats 3` 各 39/40，唯一红 D15 走用例级 `known_defect` 挂账豁免（KD-001：文本级口径注入定位不到跨子查询条件，修复归属阶段 4 AST；红照报、只免 `--all` 退出码，解除条件=AST 落地后某轮 3/3 全绿即删块）。翻转自证：同一条被人为改坏的 D23 断言（`result_row_count` 8→99），pending 时退出码 0、done 时退出码 1。豁免细则见 `evaluation/NOTES.md` §4.7。
- **KD-001 于 2026-11-07 销账（阶段 4）**：块感知注入落地（`split_query_blocks` 切查询块，口径条件判定与补写限定在直接引用指标表的块内），验收轮 `--stage v2 --repeats 3` 40/40 全绿（样本级 120/120、快照 `docs/eval_report_v2_stage4_accept.json`），D15 挂账块已删、恢复普通门禁用例——**当前全文件零挂账**，`known_defect` 机制保留备用。阶段 4 账见 `docs/STAGE4_AST_PLAN.md` 与复盘 005；time_boundary 的同病灶（文本级不认识子查询）登记待立项。
- done/pending **只在 `--all` 模式参与退出码判定**；`--stage <阶段>` 模式是本阶段有失败就 1，跟状态无关、也跟豁免无关（成绩单与门禁分离）。现在 done = v0 + v1 + v2，`problems/eval_cases.json` 的 `meta.stages` 是唯一数据源，改状态不用动代码。
- `promise` 是展示文案，**以实际实现为准**：代码不读它，判分和退出码只看 `status`。v1 的文案已收窄成「口径声明（不含澄清）、空结果标记、语义拒答」——澄清没做、结果校验只做到空结果标记，所以不写进承诺；「SQL 生成质量」也没写，v0/v1 都不保证准确率。
