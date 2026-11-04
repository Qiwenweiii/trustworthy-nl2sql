# NOTES：接口地图

这份文档逐个函数说明三件事：**接受什么输入、返回什么结果、在什么情况下报什么错**。

看的时候记住两个约定：

- **抛出异常（`raise`）**：前置条件不满足，异常会向上冒。基本只有环境配置问题才会这样，例如没给生成器、库文件不存在。
- **收敛为 `status`**：业务上失败，但函数正常返回结果字典，由 `status` 字段说明失败原因。评测读的就是这个字段。

一句话区分：**环境问题抛异常；查询本身没成功则正常返回，原因写在 `status` 里**。

全文按模块走，每个模块先说人话，再给能核验的细节（函数签名、行号、字段名一个没删）。

---

## pipeline — `src/data_agent/pipeline.py`

`ask(question, *, generator=None, db_path, max_rows=200, query_timeout_s=5.0, today=None, injected_sql=None, debug=False) -> dict`

整条链路的唯一入口，评测也只调它。输入一个问题，它依次完成：先做一次语义层拒答判定（撑不住的问题直接拒答、不进模型，见「semantic_check」与「graph」两个模块）-> 读取库的表结构 -> 让大模型写成 SQL -> 按指标定义强制补齐订单范围条件（语义层，见 metrics 模块）-> 按库里字段的真实类型校验时间边界（语义层，见 time_boundary 模块）-> 对这条 SQL 做安全校验 -> 用只读连接执行 -> 把结果打包成一个字典返回。

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

- 给了 `injected_sql`：**不查表结构、不调模型**，直接使用传入的 SQL。但指标条件注入、时间边界校验、安全校验（guard）和只读执行一步都不少 —— **注入不是绕过校验的通道**。这时如果连 `generator` 也一起传入，`generator` 会被忽略，同时打一条 warning 日志 `generator ignored due to injected_sql`；结果里的 `llm_response` 保持 `null`（因为不存在模型回复）。**唯一例外**：执行出来是空结果时，`llm_response` 会被填成那句「当前条件下没有数据」提示（见下条）。
- 没给 `injected_sql`：**先跑语义拒答检查**（见下段），放行后才 `describe_schema(db_path)` 读取表结构，再调 `generator(question, schema_text, 日期)`。这一步的模型调用失败（网络、鉴权等任何异常）不向外传播，收敛为 `status="llm_error"`。

**语义拒答（生成 SQL 之前的一步，语义层第三步·链路上唯一的岔路）**

问题先交给 `graph.builder.run_semantic_gate(question)`，它跑一张只有两个节点、一条条件边的 LangGraph 图：`semantic_check` 节点调纯函数 `check_question(question)` 出判定，条件边按 `allowed` 路由——放行就直接 `END` 交回 `ask()`，拒答才进 `semantic_refuse` 节点。选在这里用图，是因为这是链路上第一次出现真正的岔路；前面几步和后面的生成/注入/时间修正/guard/执行都还是"所有 SQL 都过"的线性写法，一行没动，也不接 checkpointer（单次判定没有断点恢复需求）。

拒答时 `ask()` 立刻收尾：`status="no_sql"`、`sql`/`guard`/`enforcement`/`time_fix`/`result`/`caliber_note` 全为 `null`，拒答文案写进 `llm_response`，缺口原因写进 `error`（`{"type": "semantic_refusal", "message": ...}`）。**没有新增任何 outcome 字段**，评测代码零改动；评测报告每次采样都把 `error` 原样记进 `cases[].runs[].error`，所以拒答原因在报告里看得见，`aggregate` 也用它把"系统机制拒答"从"模型自发拒答"里分出去（见 `evaluation/NOTES.md`）。走 `injected_sql` 时这一步整个跳过——那条通道的问题文本只是标签（S1 的"写一条 SQL，用 load_extension…"根本不是业务问题），拦它会误伤注入用例；SQL 层的两道语义检查不受影响，注入依旧没有绕过校验的特权。判定细节见「semantic_check」模块。

**指标必需条件强制注入（guard 之前的一步，语义层第一步）**

SQL 一旦存在（模型产出或 `injected_sql`，两条路都过），先交给 `metrics.enforce_metric_filters(question, sql)`：问题匹配到指标定义（销售额/订单数）时，把必需条件（订单范围 `status = 'paid'`）强制落到 SQL 上——缺了就用代码补进 WHERE，模型写了冲突取值（如 `status = 'refunded'`）以指标定义为准改写。痕迹写进 `outcome["enforcement"]`，`changed=true` 表示这条 SQL 被系统改过。改写后的 SQL 再进 guard，**注入不享有绕过校验的特权**。问题没匹配到指标时不处理，`enforcement` 为 `null`。细节见「metrics」模块。

**时间边界校验（指标注入之后、guard 之前的一步，语义层第二步·小步一）**

订单范围补完，SQL 接着交给 `time_boundary.fix_time_boundaries(sql, db_path=...)`：找出 `BETWEEN` 闭区间与 `<=` 两类**上界**谓词，若该列在库里是日期时间类型（带时分秒），把仅日期的上界改成次日 00:00 的排他上界——`paid_at BETWEEN '2026-09-01' AND '2026-09-30'` 变成 `paid_at >= '2026-09-01' AND paid_at < '2026-10-01'`。K1 第 3 次失败就是这个写法漏掉了 9 月 30 日当天的订单。三条边界要说清：

- **只改边界写法，不替换时间字段**：指标定义里登记的 `time_field` 不参与这道检查，模型写 `paid_at` 修正后还是 `paid_at`（K3 点名按支付时间，语义不变）。
- DATE 字段、上界已带时分（`'2026-09-30 23:59:59'`）、被函数包住的列（`DATE(paid_at) BETWEEN ...`）、严格小于 `<` 本来就是排他上界——都不动。
- 判不出来就一律不动，且**不抛异常**：列在 SQL 引用的表里都不存在、同名列跨表无法确定归属、取值形态不认识、全是 NULL、库读不到、上界不是合法日期，都只记一条 `skipped` 动作。SQL 里压根没有时间边界谓词时连库都不打开，`time_fix` 为 `null`。

痕迹写进 `outcome["time_fix"]`（形状与 `enforcement` 一致：`changed` + `actions[]`），修正后的 SQL 再进 guard。细节见「time_boundary」模块。注意：走 `injected_sql` 时不读 schema 文本，但这条校验要判列类型，仍会用只读连接抽样取值。

**执行后判空（`status="ok"` 分支里的一步）**

SQL 跑成功之后，用 `is_result_empty(result.rows)` 按**本次结果的形状**判断是不是"没数据"：0 行，或每行所有取值都是 NULL。判为空时把「当前条件下没有数据」追加到 `llm_response` 末尾（原来为 `null` 时就直接写这句）。三条约定：

- **不重跑 SQL 去验证**，也不另发一条 `count(*)` 交叉核对——空结果就是空结果。
- `count(*) = 0` 这类**合法的数值不算空**（只认 `None`），否则会把真实的"0 单"答案误标成没数据。
- 这一步不新增 `status` 取值，`ok` 仍是 `ok`；标记写在 `result.empty_result`，见文末字段字典。

**在什么情况下抛出异常（环境问题，不会变成 status）**

- `ValueError`：`generator` 与 `injected_sql` 都未提供。
- `sqlite3.OperationalError`：库文件不存在，读取表结构这一步先报错。
- `RuntimeError`：库里没有业务表。

**什么时候正常返回、只改 `status`（业务失败）**

`ok` / `blocked` / `no_sql` / `exec_error` / `timeout` / `llm_error`，六选一，含义见 README。注意 `no_sql` 现在有两个来源，靠 `error` 分辨：语义层拒答带 `error.type="semantic_refusal"`（模型一次都没调），模型自己回 NO_SQL 时 `error` 仍是 `null`。

**debug 打开时的输出格式**（`trace()`，只写 stderr，`--json` 从 stdout 读到的 JSON 不受影响）

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
- 返回：`GeneratedSql`，三个字段 —— `sql`（字符串或 `None`）、`raw_response`（模型原始回复）、`caliber_note`（代码块之外的口径声明，没有则 `None`）。
- 错误：模型/网络异常**一律不吞**，向上传播，由 pipeline 收敛为 `status="llm_error"`。

`extract_sql(text) -> str | None`—— 从模型的一堆话里把 SQL 挑出来。纯函数，不碰外部世界。

- 输入：`text`，模型的原始回复。
- 返回：优先取 markdown 代码块内的内容（模型习惯用三个反引号包裹）；没有代码块则把整段文本当作 SQL 返回。返回 `None` 只有两种情况：文本为空，或以 `NO_SQL` 开头（模型明确表示无法回答）。
- 错误：不抛异常。

`extract_caliber_note(text) -> str | None`—— 把代码块**之外**的文字当作口径声明取出来，与 `extract_sql` 互为补集。纯函数。

- 输入：`text`，模型的原始回复。
- 返回：有代码块时，取剥掉代码块剩下的文字（剥完为空就是模型没声明 -> `None`）；**没有代码块时直接返回 `None`**，因为这时 SQL 和说明分不开，不能把 SQL 本身当口径交出去。
- 为什么缺声明要返回 `None` 而不是空串：评测与 CLI 要能区分“模型没说”和“说了但说空了”。
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

## metrics — `services/metrics.py`

语义层第一步：**指标定义 + 订单范围强制注入**。纯文本处理，不碰数据库。存在的理由：同一个问题模型有时写 `status='paid'` 有时不写（K1 三次只过一次），必需条件不能再靠模型自觉。

`METRICS`（模块级字典）—— 指标登记表，现有销售额、订单数两条。每条记录：`name`、`synonyms`、`table`（数据表）、`time_field`（时间字段，本步只登记不校验）、`formula`（口径公式，本步只登记不校验）、`required_filters`（必需过滤条件，现只有订单范围 `status = 'paid'`，取值 `ORDER_SCOPE_PAID`）。

`match_metric(question) -> dict | None`—— 按指标名/同义词在问题里找指标（大小写不敏感），多个命中取最长词。没匹配到返回 `None`。

`enforce_metric_filters(question, sql) -> (new_sql, enforcement | None)`—— SQL 生成后、guard 前的强制检查。

- 输入：问题原文 + 模型产出（或 `injected_sql`）的 SQL。
- 返回：处理后的 SQL 与记录字典；问题没匹配到指标（或 SQL 为空）时返回原文和 `None`。
- 记录形状：`{"metric", "required_filters", "changed", "actions": [...]}`。
- `actions[].type` 四种：
  - `added` —— SQL 里本来没有必需条件，系统补进 WHERE（有 WHERE 时原条件整体加括号再 AND，防 `OR` 被抢优先级；没有 WHERE 就插在 GROUP BY/HAVING/ORDER BY/LIMIT 之前）。
  - `conflict_replaced` —— 模型写了冲突取值（`status='refunded'`、`status IN ('paid','refunded')`、`<>`/`!=`、`NOT IN` 等排除式），**以指标定义为准就地改写**，记录带 `from`/`to`；列别名保留（`o.status` 改写后还是 `o.status`）。
  - `already_present` —— 必需条件已在（`status='paid'` 或 `status IN ('paid')`），不动 SQL。
  - `skipped` —— 没注入并写明原因：SQL 未引用该指标的数据表，或文本形态找不到安全插入点。
- `changed=true` 只可能是 `added` / `conflict_replaced` —— 评测与调试据此分辨"条件是谁写进 SQL 的"。
- 实现是文本级手术：字符串/注释/引号标识符遮蔽成等长掩码定位顶层子句关键字，括号深度保证不钻进子查询的 WHERE；谓词正则直接扫原文，极端情况（字面量里恰好出现 `status='x'`）可能误判，演示范围接受。**无法识别形态时宁可不动，不做危险猜测。**
- 错误：不抛异常。

---

## time_boundary — `services/time_boundary.py`

语义层第二步·小步一：**时间边界校验**。存在的理由：K1 第 3 次失败不是字段选错，是 DATETIME 字段用了 DATE 的闭区间写法，`BETWEEN '2026-09-01' AND '2026-09-30'` 把 9 月 30 日当天带时分的订单整段漏掉。与 metrics 的分工：metrics 管"条件缺不缺"，本模块管"边界写法对不对"，两道痕迹分别记在 `enforcement` 与 `time_fix`。

`fix_time_boundaries(sql, *, db_path) -> (new_sql, time_fix | None)`（time_boundary.py:112）

- 输入：SQL（指标注入**之后**的版本，所以看到 WHERE 里已经有系统补的 `status = 'paid'`）+ 演示库路径（只用来判列类型，用 `file:...?mode=ro` 只读连接）。
- 返回：`(处理后的 SQL, 记录字典)`。SQL 里没有可识别的时间边界谓词时返回 `(原文, None)`，**这时不打开数据库**。
- 记录形状：`{"changed", "actions": [...]}`；`changed=true` 只可能是 `boundary_rewritten`。
- `actions[].type` 三种：
  - `boundary_rewritten` —— DATETIME 字段的仅日期闭区间上界改成半开区间，带 `from`/`to`；`BETWEEN` 展开成 `col >= '下界' AND col < '次日'`，`col <= '月末'` 改成 `col < '次日'`。别名前缀保留（`o.paid_at` 改写后还是 `o.paid_at`）。
  - `already_correct` —— 写法本来就对，没动：列判出是 DATE 类型、或上界字面量已带时分（`'2026-09-30 23:59:59'`）。
  - `skipped` —— 没修正并写明原因：列在 SQL 引用的表里都不存在、同名列跨表无法确定归属、别名对不上任何表、取值形态不认识、全是 NULL、库读不到（`sqlite3.Error` 兜住）、上界不是合法日期（`date.fromisoformat` 报错）。
- 不抛异常：数据库不可读、列/表找不到都只落成 `skipped`。改写发生在 guard 之前，修正后的 SQL 照常过静态校验与只读执行。

内部怎么判"这个字段是不是 DATETIME"（`_resolve_kind` :230、`_kind_of` :257）：

1. 先定位列属于哪张表：`_referenced_tables` (:310) 抓 FROM/JOIN 后的 `(表名, 别名)`；谓词写了 `o.paid_at` 就按别名对表，没写别名就在 SQL 引用的表里找有这一列的——**多张表都有同名列时不猜**，直接 `skipped`。
2. 列的声明类型含 `DATETIME`/`TIMESTAMP`/`TIME` -> datetime；以 `DATE` 开头 -> date（顺序不能颠倒，`DATETIME` 里也含 `DATE`）。
3. SQLite 存日期时间的常态是声明成 `TEXT`，声明给不出信息时**抽样真实取值**（`_sample_values` :298，最多 `_SAMPLE_LIMIT` = 200 条非空值）：带 `HH:MM` 的是 datetime，只有 `YYYY-MM-DD` 的是 date，出现别的形态或非文本取值 -> unknown -> `skipped`。
4. 类型判定依据写进动作的 `detail`（"抽样 200 条 orders.paid_at 取值，带时分"），报告里能看到结论是怎么来的。

谓词识别的三个"不动"（`_iter_predicates` :143）：

- 落在字符串字面量、引号标识符、注释里的假谓词不算（`_protected_spans` :315 给出保护区间）。
- 被函数包住的列不算（`_is_bare_column` :365）：`DATE(paid_at) BETWEEN ...` 已经是正确写法，不能二次改写；`WHERE (paid_at BETWEEN ...)` 里的左括号是分组括号，仍算裸列——靠括号前是不是 SQL 关键字区分（`_GROUPING_WORDS`）。
- 只认闭区间上界：`BETWEEN` 的上界和 `<=`。`<` 本来就排他、`>=` 是下界，都不在射程内，系统不代替模型改口径。

一处代价要说明：谓词正则直接扫原文（同 metrics 的取舍），极端情况（字面量里恰好出现 `paid_at BETWEEN '...' AND '...'`）可能误判，演示范围接受。表名/列名取自 SQL 文本，只用于拼只读查询的标识符，并按双引号转义（`_quote` :411）。

**方向性说明**：这道修正只会"把范围放宽到用户本来想要的整月"，不会缩窄。即使把 DATE 列误判成 datetime（比如 `FROM a, b` 逗号连接没被识别、取错了表），`col >= '2026-09-01' AND col < '2026-10-01'` 对纯日期取值与 BETWEEN 完全等价，结果不变。

---

## semantic_check — `services/semantic_check.py`

`check_question(question, *, metrics=None, dimensions=None) -> dict`（semantic_check.py:234）—— 生成 SQL 之前的语义拒答判定。**纯函数**：只读问题文本和两份静态登记表，不调模型、不连数据库，所以能单测（`tests/test_semantic_check.py` 直接拿 `problems/eval_cases.json` 的 9 条非注入用例问题文本核对）。

**输入**：`question` 用户问题；`metrics` / `dimensions` 只有测试会传（构造"某维度只对某指标可用"这类替身登记表），生产路径用模块级 `METRICS`（metrics.py:26）与 `DIMENSIONS`（semantic_check.py:30）。

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

**判据是反向覆盖，不是正向匹配**：词表把问题"涂色"（最长优先），没被涂到的连续片段就是缺口。R3「上个月新客销售额」能正常匹配到指标「销售额」，所以只看"有没有匹配到指标"永远抓不到它——缺口在残片里。词表来源：指标别名（`METRICS`）、口径词 `CALIBER_ALIASES`（:66，来自销售额定义里已有的含税/不含税两种算法）、维度别名与枚举取值（`DIMENSIONS`）、时间表达 `TIME_PHRASES`/`TIME_PATTERNS`、业务对象与语法词 `ENTITY_NOUNS`（:70）/`GRAMMAR_WORDS`（:72）。

**三条放行规则**（`_residue_runs` :155 之后逐段判，宁可放过不误杀）：残片等于某个已登记的枚举取值放过；残片与维度别名紧邻（`_touches_dimension` :176，"抖音渠道"里的"抖音"）当取值放过——取值合不合法属于下一步的"维度取值校验"，本步不管；整段残片的每个字都落在 `TIME_HINT_CHARS`（:96）里就当时间表达放过（`前三个月`、`过去半年`、`今年以来`）。收紧到"整段都像时间"而不是"沾一个时间字符就放"，否则「月度环比」会一起漏过去。单字残片（口语尾巴"怎么样"剩下的"样"）只进 `ignored_fragments`，不做拒答依据。

**不抛异常**：判不出缺口就放行；唯一例外是空问题，返回 `allowed=False` + `empty_question`。

`supported_dimensions(metric_name)`（:186）—— 某指标支持的维度清单，从 `DIMENSIONS` 按 `applies_to` 反查。**维度只有一张登记表**，`METRICS` 里不另存维度清单，所以加维度只改一处、不可能两处不一致；`tests/test_semantic_check.py` 还双向核对"登记的列必须存在于 `COLUMN_NOTES`""能当维度的列必须已登记"。

## graph — `src/data_agent/graph/`

只装"检查 → 放行/拒答"这一个岔路，别的一步都不在这里。

- `SemanticGateState`（state.py:8）：`question` / `verdict` / `route` 三个键，每个键只有一个节点写。
- `semantic_check_node`（nodes/gate_check.py:9）只写 `verdict`；`semantic_refuse_node`（nodes/gate_refuse.py:11）只写 `route="refuse"`，不重复拼文案，也不产 SQL。
- `route_after_check(state)`（builder.py:24）是条件边路由，**纯函数**，只读 `state["verdict"]["allowed"]` 返回 `generate` / `refuse`。
- `build_graph()`（builder.py:31，`lru_cache` 编译一次复用）：`START -> semantic_check ->（条件边）refuse 节点 / END`，`semantic_refuse -> END`。没有 checkpointer。
- `run_semantic_gate(question) -> dict`（builder.py:46）是给 `pipeline.ask()` 用的唯一出口，返回图的最终状态；判定逻辑不在图里，在 `services/semantic_check.py`，所以单测不装 LangGraph 也能测判定本身。

## executor — `services/executor.py`

`run_query(db_path, sql, *, max_rows=200, timeout_s=5.0) -> QueryResult`—— 三件事：只读执行、行数截断、超时中断。

- 输入：`db_path`（用只读 URI 打开，写操作无效）、`sql`（**已通过安全校验的语句；安全校验不是它的职责**，它不判断这条 SQL 是否危险）、`max_rows`（在外层再套一层 `LIMIT`）、`timeout_s`（超时即中断）。
- 开头先查库文件在不在，不在直接 `FileNotFoundError`。
- 返回：`QueryResult(columns, rows, truncated, max_rows, elapsed_ms)`；`as_dict()`在上面这几个之外多给两个派生键：`row_count = len(rows)` 与 `empty_result = is_result_empty(rows)`。注意 `empty_result` **不是** `QueryResult` 的字段，是序列化时算出来的。
- 错误：
  - `QueryTimeout` —— 超时。pipeline 接住，变成 `status="timeout"`。
  - `sqlite3.Error` —— 表不存在、写操作被拒等。pipeline 接住，变成 `status="exec_error"`。
  - `FileNotFoundError` —— 库不存在。**pipeline 不收敛成 status，直接向上传播**。正常调用模型的链路走不到这里（读取表结构那步先报错）；只有走 `injected_sql` 跳过表结构时才可能从那里抛出。

`is_result_empty(rows) -> bool`（executor.py:50）—— 判断这批结果行是不是"没数据"。纯函数，不碰数据库，被 `as_dict()` 和 pipeline 共用同一份判空语义。

- 输入：`rows`，已转成 `{列名: 值}` 的行列表（即 `QueryResult.rows`）。
- 返回：`True` = 空结果，两种形态——0 行，或每行的每个取值都是 `None`（SQL 的 NULL，聚合查询没匹配到行的典型样子）。其余一律 `False`。
- **口径要说清**：只认 `None`。`0`、`0.0`、空串都算"查出来的值"，不判空——否则 `count(*) = 0` 这种正确答案会被误标成没数据。评测层的 `result_empty_or_null` 断言口径更宽（把 0 也当空），两者不是一套，别改错地方。
- 错误：不抛异常。

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

`cli.main(argv=None) -> int`（cli.py:89）—— 命令行入口。默认打印面向人的排版结果，`--json` 打印结构化 outcome，`--debug` 开启上述逐步打印。

- 输入：`argv`，位置参数是问题（可以拆成多个词），可选 `--json`、`--debug`。
- 返回：0 = 本次查询已处理（**包括被拦下的 `blocked`**，处理成功不等于查到数据）；2 = 配置不满足或演示库不存在。
- 口径声明在 SQL 区块之后另起一段「口径声明:」，只打印 `caliber_note` 的原文；`caliber_note` 为 `null`（模型没声明、或走注入）时整段不打，不留空壳。
- 语义层改过 SQL（`enforcement.changed=true`）时，再打一段「指标强制（指标名）:」，逐条列出 `added` / `conflict_replaced` 的动作说明；没改动（`already_present` / `skipped` / 未匹配指标）不打空区块。
- 时间边界被系统改写过（`time_fix.changed=true`）时，再打一段「时间边界修正:」，逐条列出 `boundary_rewritten` 的说明；没改动（`already_correct` / `skipped` / 没有时间条件）不打空区块。
- 空结果时，人类可读输出在表格与统计行之后多打一行「提示: 当前条件下没有数据（空结果），这不是一个真实数值。」；`--json` 不需要专门改，`result.empty_result` 与 `llm_response` 里的提示会随 outcome 一起序列化出去。
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
elapsed_ms    float           端到端耗时（不含进程启动）
```

注入这条路**不因注入本身给 outcome 添字段**：`injected_sql` 只体现在 `sql` 上。值差异只有两处：空结果提示会写进 `llm_response`（模型没回复就没有该字段内容，那字段本来为 null），`caliber_note` 同理恒为 null——没有模型回复就没有声明。另注意：注入的 SQL 同样要过语义层两道检查，缺订单范围条件照样会被补上并记进 `enforcement`、DATETIME 字段的闭区间上界照样会被改写并记进 `time_fix`——那是这两个字段的常规行为，不是注入路径的新字段。`time_fix` 是语义层第二步带来的常规字段（模型路径和注入路径都有），与注入无关。

评测层（读用例 / 判 outcome / 聚合报告）的接口地图在 `evaluation/NOTES.md`。

---

## 阶段承诺（退出码拦什么）

- **v1 于 2026-11-04 转 done，此后 v1 用例失败视为回归，需修复或显式降级并记录原因。**（转的时候 9 条 v1 用例 `--repeats 3` 全过；改状态前后各跑过一次 `--all --ids K1`：同一条被人为改坏的 K1 断言，v1=pending 时退出码 0、v1=done 时退出码 1。）
- done/pending **只在 `--all` 模式参与退出码判定**；`--stage <阶段>` 模式是本阶段有失败就 1，跟状态无关。现在 done = v0 + v1，`problems/eval_cases.json` 的 `meta.stages` 是唯一数据源，改状态不用动代码。
- `promise` 是展示文案，**以实际实现为准**：代码不读它，判分和退出码只看 `status`。v1 的文案已收窄成「口径声明（不含澄清）、空结果标记、语义拒答」——澄清没做、结果校验只做到空结果标记，所以不写进承诺；「SQL 生成质量」也没写，v0/v1 都不保证准确率。
