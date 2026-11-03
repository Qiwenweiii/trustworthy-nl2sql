# NOTES：接口地图（v0）

这份文档逐个函数说明三件事：**接受什么输入、返回什么结果、在什么情况下报什么错**。

看的时候记住两个约定：

- **抛出异常（`raise`）**：前置条件不满足，异常会向上冒。基本只有环境配置问题才会这样，例如没给生成器、库文件不存在。
- **收敛为 `status`**：业务上失败，但函数正常返回结果字典，由 `status` 字段说明失败原因。评测读的就是这个字段。

一句话区分：**环境问题抛异常；查询本身没成功则正常返回，原因写在 `status` 里**。

全文按模块走，每个模块先说人话，再给能核验的细节（函数签名、行号、字段名一个没删）。

---

## pipeline — `src/data_agent/pipeline.py`

`ask(question, *, generator=None, db_path, max_rows=200, query_timeout_s=5.0, today=None, injected_sql=None, debug=False) -> dict`

整条链路的唯一入口，评测也只调它。输入一个问题，它依次完成：读取库的表结构 -> 让大模型写成 SQL -> 对这条 SQL 做安全校验 -> 用只读连接执行 -> 把结果打包成一个字典返回。

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

**两条内部路线**（`ask` 里的分支）

- 给了 `injected_sql`：**不查表结构、不调模型**，直接使用传入的 SQL。但安全校验（guard）和只读执行一步都不少 —— **注入不是绕过校验的通道**。这时如果连 `generator` 也一起传入，`generator` 会被忽略，同时打一条 warning 日志 `generator ignored due to injected_sql`；结果里的 `llm_response` 保持 `null`（因为不存在模型回复）。
- 没给 `injected_sql`：先 `describe_schema(db_path)` 读取表结构，再调 `generator(question, schema_text, 日期)`。这一步的模型调用失败（网络、鉴权等任何异常）不向外传播，收敛为 `status="llm_error"`。

**在什么情况下抛出异常（环境问题，不会变成 status）**

- `ValueError`：`generator` 与 `injected_sql` 都未提供。
- `sqlite3.OperationalError`：库文件不存在，读取表结构这一步先报错。
- `RuntimeError`：库里没有业务表。

**什么时候正常返回、只改 `status`（业务失败）**

`ok` / `blocked` / `no_sql` / `exec_error` / `timeout` / `llm_error`，六选一，含义见 README。

**debug 打开时的输出格式**（`trace()`，只写 stderr，`--json` 从 stdout 读到的 JSON 不受影响）

```text
[ask] question: ...
[ask] schema: ...（走注入时这行是"跳过（injected_sql）"）
[ask] sql: ...（模型没给 SQL 时是"无"）
[ask] guard: passed=... violations=N 规则名(细节) ...
[ask] exec: rows=... elapsed_ms=...（失败时给错误信息）
[ask] outcome: status=...（只在提前收尾时多这一行）
```

---

## llm_client — `services/llm_client.py`

`build_generator(cfg) -> SqlGenerator`—— 拿配置造出一个"能生成 SQL 的动作"，返回的是个闭包 `generate`。

这里有两件容易看不懂的事，说清楚：

1. **基准日期不在造的时候定死，而是每次调用的时候现传。** 这样同一个生成器可以伺候多个锚定日期，评测想换日期不用重新造。
2. **"闭包"不是多余的包装。** pipeline 只认 `SqlGenerator` 这一个签名（`Callable[[str, str, date], GeneratedSql]`）。正因为它认签名不认实现，评测才能整体换成一个桩函数，单独把"安全校验 + 执行"这段链路跑起来 —— 这是 `injected_sql` 之外的第二条注入路。

- 输入：`cfg`（Config），只用里面三个字段 `api_key` / `base_url` / `model`。
- 返回：`generate` 这个闭包。
- 错误：抛出 `openai.*` 异常，例如 api key 非法，构造阶段即抛出，不做吞掉处理。

`generate(question, schema_text, today) -> GeneratedSql`—— 真调一次模型对话接口（chat，`temperature=0`，就是让它别发挥，尽量每次一样），然后把回复里的 SQL 抠出来。这个函数只被 pipeline 调用。

- 输入：`question`（用户的问题）、`schema_text`（上面 `describe_schema` 的输出）、`today`（填进系统提示词里的 `{today}`，决定"上个月"是哪个月）。
- 返回：`GeneratedSql`，两个字段 —— `sql`（字符串或 `None`）、`raw_response`（模型原始回复）。
- 错误：模型/网络异常**一律不吞**，向上传播，由 pipeline 收敛为 `status="llm_error"`。

`extract_sql(text) -> str | None`—— 从模型的一堆话里把 SQL 挑出来。纯函数，不碰外部世界。

- 输入：`text`，模型的原始回复。
- 返回：优先取 markdown 代码块内的内容（模型习惯用三个反引号包裹）；没有代码块则把整段文本当作 SQL 返回。返回 `None` 只有两种情况：文本为空，或以 `NO_SQL` 开头（模型明确表示无法回答）。
- 错误：不抛异常。

---

## sql_guard — `services/sql_guard.py`

`check_sql(sql) -> GuardResult`—— 判断"这条 SQL 是不是单条、只读的 SELECT"，并把不合格的原因一条条列出来。

`as_dict()`是准备写入 outcome 的版本，只有 `{"passed", "violations"}` 两个键，**不含 `sql`**。

**它的判定方法是这次改动里最不直观的一块，值得说明白**：扫一遍 SQL，产出两份文本，各管各的。

- **结构视图**：把引号包起来的标识符折成一个占位符 `ident`。这份用来查关键字 —— 所以 `SELECT 'delete'` 里的 `delete` 不会被当成删除操作误杀。
- **调用视图**：保留标识符的真实内容。这份用来查函数调用 —— 所以 `SELECT "load_extension"(...)` 这种想用引号规避校验的写法照样算一次函数调用。

两份文本各判各的，**不做跨文本的位置对应**（不要尝试推断"结构视图第 N 个字符对应原文哪个位置"，代码里没有这种对应关系）。

- 输入：`sql`，模型产出的 SQL，允许空串。
- 返回：`GuardResult(passed, violations, sql)`
  - `passed`：放不放行。
  - `violations`：每条一个字典，`rule`（规则名）、`detail`（人话说明）、`token`（命中的关键字/函数名，有的规则没有这个键）。
  - `sql`：**去掉末尾分号后的规范化语句**，真正送去执行的是它，不是你传进来的原文。
- 错误：不抛异常。非法输入也只返回 `passed=False`。

`strip_trailing_semicolons(sql) -> str`—— 把尾巴上的空白和分号抹掉。

为什么单拎一个函数出来：`check_sql` 用它产出送执行的 `sql`，executor 在包 `LIMIT` 之前也调它。**两处共用同一份"什么叫做完尾"的语义**，不会出现一处认为干净、另一处认为没写完。

- 输入：任意字符串。返回：去掉尾部 `;` 和空白后的语句（等价于"去尾分号再 `strip`"）。**字符串字面量、注释里的分号不动**，只处理末尾。
- 错误：不抛异常。

---

## executor — `services/executor.py`

`run_query(db_path, sql, *, max_rows=200, timeout_s=5.0) -> QueryResult`—— 三件事：只读执行、行数截断、超时中断。

- 输入：`db_path`（用只读 URI 打开，写操作无效）、`sql`（**已通过安全校验的语句；安全校验不是它的职责**，它不判断这条 SQL 是否危险）、`max_rows`（在外层再套一层 `LIMIT`）、`timeout_s`（超时即中断）。
- 开头先查库文件在不在，不在直接 `FileNotFoundError`。
- 返回：`QueryResult(columns, rows, truncated, max_rows, elapsed_ms)`；`as_dict()`在上面这几个之外多给一个 `row_count = len(rows)`。
- 错误：
  - `QueryTimeout` —— 超时。pipeline 接住，变成 `status="timeout"`。
  - `sqlite3.Error` —— 表不存在、写操作被拒等。pipeline 接住，变成 `status="exec_error"`。
  - `FileNotFoundError` —— 库不存在。**pipeline 不收敛成 status，直接向上传播**。正常调用模型的链路走不到这里（读取表结构那步先报错）；只有走 `injected_sql` 跳过表结构时才可能从那里抛出。

`_on_progress() -> int`—— SQLite 的"进度回调"（progress handler），本质是个心跳探针：数据库每执行一小段就回头问一次"该停了吗"。它比对时间戳，到点了返回 1 表示"中断"，否则返回 0 表示"继续"。

- 输入：无参数，读取闭包内的两个变量 `deadline` / `timed_out`。
- 返回：0 继续 / 1 请求中断。
- 错误：不抛异常。

`open_readonly_connection(db_path) -> sqlite3.Connection`—— 造一个只读连接：只读 URI + 关掉 `load_extension` + 挂上 authorizer 白名单（只放行读类动作，写、PRAGMA、ATTACH、事务一律拒绝）。

评测想单独验证"写操作必然失败"，直接用这个函数就行，不用绕整条链路。

- 输入：`db_path`。
- 返回：已配好安全设置的连接对象。
- 错误：`sqlite3.OperationalError`（库文件不存在）；`sqlite3.Error`（用该连接执行写、PRAGMA 等非白名单操作）。

---

## schema — `services/schema.py`

`describe_schema(db_path) -> str`—— 把库里有哪些表、哪些列、什么类型、中文备注拼成一段文本，这段文本会原样写入给模型的提示词（模型只看这段文字来写 SQL，它看不到库）。

- 输入：`db_path`。
- 返回：一段多行文本，形如 `"表 orders:\n  - amount REAL  -- 订单金额（元，含税）"`。
- 错误：`sqlite3.OperationalError`（库文件不存在）；`RuntimeError`（库里没有业务表）。
- 备注从哪来：查 `COLUMN_NOTES` 这张表，键长这样 `"orders.amount"`。
- 旧版本文档在这里写过一个 `note` 键，是笔误：`evaluate_case` 的断言结果里没有 `note` 这个字段（只有 `observed` 承担面向人的说明），本版按实际代码删掉了这条。

---

## 配置 / CLI / 初始化

`load_config(env_file=None, *, require_llm=True) -> Config`—— 读项目根的 `.env`，系统环境变量优先级更高。

- 输入：`env_file`（默认项目根 `.env`）、`require_llm`。
- `require_llm=False` 是为"只走 `injected_sql` 的评测"预留的路径：**跳过 key 和模型名的必填校验**。此时这两个字段可能是空字符串，**不得拿它去构造 LLM 客户端**。
- 返回：`Config`，包含 `api_key` / `base_url` / `model` / `db_path` / `max_rows` / `query_timeout_s`。
- 错误：`ConfigError` —— `require_llm=True` 且缺 `OPENAI_API_KEY` / `DATA_AGENT_MODEL`；或者数值项非法、`<= 0`（`DATA_AGENT_MAX_ROWS`、`DATA_AGENT_QUERY_TIMEOUT`）。

`ensure_db_exists(config) -> None`（config.py:32）—— 演示库不存在就抛 `ConfigError`。CLI 和评测 runner 用同一句文案，对应 README 里的退出码 2。

- 输入：`config`，只读取它的 `db_path`。
- 返回：`None`。库存在时不做任何事。
- 错误：`ConfigError`。
- 为什么这个检查不放进 `load_config`：**让"解析配置"这件事不碰磁盘**，两件事分开，配置本身是否合法跟库在不在没关系。

`cli.main(argv=None) -> int`（cli.py:74）—— 命令行入口。默认打印面向人的排版结果，`--json` 打印结构化 outcome，`--debug` 开启上述逐步打印。

- 输入：`argv`，位置参数是问题（可以拆成多个词），可选 `--json`、`--debug`。
- 返回：0 = 本次查询已处理（**包括被拦下的 `blocked`**，处理成功不等于查到数据）；2 = 配置不满足或演示库不存在。
- 错误：一般不抛。库里没有业务表时，`describe_schema` 的 `RuntimeError` 会直接向上传播。

`init_db(db_path, force=False, today=None) -> dict` —— 建演示库并灌进样例数据。

- 输入：`db_path`（输出位置）、`force`（目标已存在时是否重建）、`today`（数据往前倒推的基准日，可固定日期以便复现）。
- 返回：`{"db_path", "customers", "orders", "months"}`。
- 错误：`FileExistsError` —— 目标已存在且 `force=False`。

---

## outcome 字段字典（`ask` 的返回值和 `--json` 是同一份）

字段名和枚举取值**就是评测的契约**。改了代码必须同步 README 和本文件。

```text
question      str             原始问题
status        str             ok | blocked | no_sql | exec_error | timeout | llm_error
sql           str | null      送执行的 SQL；blocked 时是被拦下的原文；no_sql/llm_error 为 null
guard         dict | null     {passed, violations}；no_sql/llm_error 时压根没校验 -> null
  rule        str             empty_sql | malformed_sql | multiple_statements
                              | not_select | forbidden_keyword | forbidden_function
  detail      str             中文说明
  token       str             命中的关键字/函数名，部分规则没有这个键
result        dict | null     只有 status=ok 时非空
  columns     list[str]       列名
  rows        list[dict]      每行 {列名: 值}，SQL 里的 NULL 变成 None
  row_count   int             截断后的实际行数，不等于库里总行数
  truncated   bool            true = 撞到行数上限，后面还有行没返回
  max_rows    int             这次生效的上限
  elapsed_ms  float           数据库自己跑了多久
error         dict | null     {type, message}；type = sql_error | timeout | llm_error
llm_response  str | null      模型原始回复；llm_error 时是 null；走 injected_sql 时也恒为 null
elapsed_ms    float           端到端耗时（不含进程启动）
```

注入这条路**不给 outcome 添新字段**：`injected_sql` 只体现在 `sql` 上，契约本身不变。

评测层（读用例 / 判 outcome / 聚合报告）的接口地图在 `evaluation/NOTES.md`。
