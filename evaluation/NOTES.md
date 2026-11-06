# NOTES：评测层接口地图（阶段制评测）

**这份文档管什么**：`evaluation/` 里的评测代码 —— 怎么读用例、怎么判对错、怎么把结果汇总成报告。外加评测要依赖的 3 个被测接口增量（`ask(today=, injected_sql=, debug=)`、`load_config(require_llm=)`、`SqlGenerator` 签名）。

**这份文档不管什么**：业务模块（pipeline / pipeline_steps / semantic_tables / llm_client / sql_guard / metrics / time_boundary / semantic_check / executor / schema）自己的接口地图 —— 看根目录 `NOTES.md`；用例内容 —— 看 `problems/eval_cases.json`；为什么这么设计 —— 看 `docs/EVAL_DESIGN.md`。

看的时候记住两种失败处理方式：

- **抛出异常（`raise`）**：前置条件不满足，异常向上传播。
- **收敛进返回值（`return`）**：函数正常返回，由返回值里的状态字段说明失败原因。

---

## 0. 三句话讲清评测怎么跑

```text
1 读     judge.load_cases(problems/eval_cases.json) -> (meta, cases)     期望值只存在用例文件里
2 跑     run_eval.run_cases(...)  按 --stage/--all 选出的用例逐条调 pipeline.ask(...)
                                   多次采样                              注入用例 1 次，模型用例 --repeats 次
3 判     judge.evaluate_case(case, outcome) -> 每条断言 pass/fail/skipped
4 分     judge.decide_verdict(...) -> 用例结论 pass|fail（全采样不可判定 -> environment）
5 聚     judge.aggregate(...) -> 总览 / 按阶段 / 失败按（类别, root_cause）/ 不可靠通过清单
6 出     run_eval.render_text(...) 终端分区报告（阶段报告或差距报告）+ <out>.json 结构化报告
```

**贯穿全流程的固定约定**：

- 评测代码里不内联任何期望值。加用例只改 JSON，不改 Python。破坏这条约定，用例就无法被他人复核。
- 判定只出"通过/失败/不可判定"，**不给失败预设原因**。老版本在用例级写 `failure_type`、断言级写 `on_fail`，等于提前替失败找好借口；阶段制把这两个字段删掉了，`load_cases` 遇到残留直接报错。

---

## 1. 三类结论与阶段规则

| 结论                    | 人话含义                                                                                      | 算失败               |
| ----------------------- | --------------------------------------------------------------------------------------------- | -------------------- |
| `pass` 通过             | 全部断言通过（或因模型合理拒答被跳过；靠拒答拿到的通过另计"不可靠通过"，见 §5）                | 否                   |
| `fail` 失败             | 任一已判定采样命中任一失败断言                                                                 | 是                   |
| `environment` 不可判定  | 不是靶子的错，是环境不行：LLM 调用故障、runner 崩了、配置缺失、演示库的数据窗口不满足用例前提 | 退出码 2             |

**阶段规则（为什么这样判就够用）**：

- 用例各归各的阶段（`meta.stages` + 每条用例的 `stage`）：v0 = 只读防线（done，S1/S2 注入用例），v1 = 可信层（done，2026-11-04 转的，口径/空结果/拒答 9 条）。
- `--stage v0` 是日常模式：只跑该阶段用例，报告只出这阶段的结果，干净、直接。
- `--all` 是验收模式：跑所有阶段，出"距离完整规范还差什么"的差距报告，只在阶段验收时手动跑，不进日常流程。
- **退出码只看 done 阶段的失败**（`compute_exit_code`，run_eval.py:428；done 清单来自 `judge.done_stage_ids`，judge.py:453）。规则数据化在 `meta.stages.status` 里 —— v1 已于 2026-11-04 转成 `done`（done = v0 + v1），**代码一行没改**。判别性对照：同一条被人为改坏的 K1 断言，`--all --ids K1` 在 v1=pending 时退出码 0、转 done 后同样的跑法退出码 1。
- 失败不分类，但每条断言仍带 `root_cause`（聚合分组键）+ `why`（为什么算问题）+ `landing`（建议落点，如"等 v1：口径澄清层"）。这三样是证据和处置提示，不参与判分。

---

## 2. judge — `evaluation/judge.py`（纯函数，不碰被测系统）

这个模块**不依赖 pipeline**：只接收一个 outcome 字典做判断。这个结果由谁产生，是 run_eval 的职责。分工不能破（见 §9）。

### 2.1 `load_cases(path) -> (meta, cases)`（:317）

读 `problems/eval_cases.json`，同时做结构校验 —— 用例文件不合法就直接报错，不把问题留到后段。一次读全量，不做分页也不做懒加载。

- 输入：`path`（`Path`），用例文件路径。
- 返回：`meta`（purpose / anchor_today / **stages** / notes / cases 条数）+ `cases`（每条含 id / stage / category / assertions 等，形状见 §6）。
- 错误：`CaseSpecError`（继承 `ValueError`，:47），清单如下 —— 每一条都对应一种用例文件自身不合法的情况：

```text
顶层不是带 cases 数组的对象 | cases 为空
meta.stages 缺失/为空对象 | 某阶段 status 不在 STAGE_STATUSES(:22) = done|pending 内
                             <- 退出码规则的数据源，缺了它"只看 done 阶段"就无从谈起
用例缺 REQUIRED_CASE_KEYS(:24)（含 stage）| id 重复 | assertions 为空
用例 stage 不在 meta.stages 的键里
expected_status 不在 pipeline.STATUSES 内            <- 防止用例拿 v0 没承诺过的状态自说自话
用例带 failure_type 或断言带 on_fail                 <- 阶段制已删除失败预设分类，残留即报错
deterministic=true 但没有 injected_sql                <- 标了"可复现"就必须是纯注入用例
断言缺 REQUIRED_ASSERTION_KEYS(:33)（check/root_cause/why/landing）
check 不在 CHECKS(:301) 注册表里
缺 expect
expect 形状不符（EXPECT_VALIDATORS，:117）：
  status_in 要非空列表且每项在 pipeline.STATUSES 内
  guard_passed / sql_null / result_none / result_empty_or_null 要 true/false 布尔
  result_row_count 要非负整数（布尔不算整数）
  guard_rules_include 要非空对象列表，每项带 rule 字符串（token 可选、要字符串）
  guard_rules_exclude 要字符串列表
  sql_matches / sql_not_matches / llm_response_matches 要字符串（先验型再编译）
sql_matches / sql_not_matches / llm_response_matches 的正则非法（re.error 转成 CaseSpecError）
result_value_in 缺 values / tolerance / 容差模式非法（_validate_value_expect，:54）
```

**expect 形状也要过载入**：expect 是判分依据。拼错的状态词（`"blockedd"`）、错类型的期望值（`"40"` 字符串）在加这层校验前能过载入，然后运行期永远静默判红——比报错更难查。判别性证据在 `tests/test_eval_judge.py::test_load_cases_rejects_invalid_assertion_expect`（14 种非法形状逐条报错 + 2 种合法形状正向对照，防校验写过头误杀真用例）。

**容差必须写死在用例里**：judge 不内置任何默认容差，`tolerance.mode` 只认 `absolute` / `relative`（`TOLERANCE_MODES`，:51）。判分前必须明确说明允许的偏差范围。

### 2.2 `evaluate_case(case, outcome) -> dict`（:397）

把一条用例的一串断言，逐条打在**同一次** outcome 上。不做补全、不做修正，outcome 是什么就判什么。

- 输入：`case`（`load_cases` 输出的单条用例，必须含 id）、`outcome`（`pipeline.ask()` 的返回值，原样传）。
- 返回：

```text
{
  case_id, outcome_status,
  refusal_accepted,                  见下方"拒答语义"
  assertions: [{
    index,        断言在数组里的下标；runner 靠它跨采样去重
    check, expect,
    root_cause,   聚合分组键，报告按（类别, root_cause）分组
    capability,   能力域；用例级优先，缺省取 case.capability，再缺省 "（未标注）"
    why, landing,
    status,       "pass" | "fail" | "skipped"
    observed,     给复核看的一句话证据
  }]
}
```

- 错误：`CaseSpecError` —— 用例正则非法（运行期二次校验，正常情况下 `load_cases` 已拦截）；`CaseSpecError` —— `result_value_in` 的容差模式未知（由 `_within_tolerance` 抛出）。

**拒答语义**（判据在 :399，跳过名单在 :36，接受状态集合来自 `allowed_statuses` :388）：

模型回答"无法生成 SQL"（`status == "no_sql"`），而这条用例本来就接受拒答（`status_in` 的期望里包含 `no_sql`）时，`SKIP_ON_ACCEPTED_REFUSAL` 里的断言记成 `skipped` 而不是 `fail`。跳过名单：`sql_matches`、`result_empty_or_null`、`result_row_count`、`result_value_in`、`llm_response_matches`。

理由：**合理拒答不应被当成"没给数据"的失败**。这五条断言检查的都是"数据是什么样子"，题都没答，自然无从检查。

反过来，`status_in`、`sql_null`、`guard_*`、`sql_not_matches`、`result_none` 这五条**不参与跳过** —— 它们描述的正是"拒答本身应有的形态"，拒答时更需要校验。

**注意 R1/R2/R3 这类用例**：它们的断言全是"不参与跳过"那五条，模型拒答时一条断言都不会被跳。所以拒答照样能 pass，而"这次 pass 靠的是拒答"这件事由 `refusal_accepted` 字段带走，交给 aggregate 识别成不可靠通过（§5）—— 不是靠跳过名单识别的。

### 2.3 `failures_of(evaluation) -> list[dict]`（:434）与 `skipped_checks_of(evaluation) -> list[str]`（:439）

前者只挑 `status == "fail"` 的断言，`skipped` 不算失败。后者返回被跳过的断言名 —— run_eval 把它记进采样结果，aggregate 靠它识别"靠跳过拿到的运气 pass"。

### 2.4 `decide_verdict(reports) -> str`（:446）

同一用例采样了好几次，最终这条用例算什么结论，由这个函数定。

- 输入：`reports`，该用例的多次判定结果（`run_cases` 里的已判定采样，每项带 `failures`）。
- 返回：`"pass"` / `"fail"`。
- 规则：**任一失败即失败（fail），否则通过（pass）**。不做多数表决，不看通过率，也不再区分"缺陷/未实现"。
- 注意：传进来的必须只是**已判定**的那几次（`llm_error` / `runner_crash` 那几次不能进来，见 §4.4）；`environment` 不由这里产生，是 run_cases 在"全部采样都不可判定"时直接给的。

**模型抖动怎么读（这块最容易看错）**：用例结论看的是"最坏的那一次"；**重复之间的稳不稳看 `record["passing_runs"]/decidable_runs`**（报告明细里那句"通过重复 2/3"）。一个是定性、一个是定量，别混着用 —— `verdict=pass` 不代表每次都稳，"通过重复 2/3"也不代表这条用例算失败。

### 2.5 `done_stage_ids(stages) -> list[str]`（:453）

从 `meta.stages` 挑出 `status == "done"` 的阶段名。唯一的消费方是 `compute_exit_code`（§4.7）—— 这就是"v1 转 done 不用改代码"的落点。

### 2.6 `aggregate(case_records, *, anchor_today, repeats) -> dict`（:478）

把逐用例记录汇总成一份报告结构。四个维度：总览、按阶段、失败按（类别, root_cause）、不可靠通过清单。

- 输入：`case_records`（`run_cases` 的输出，需含 stage / verdict / failures / category）、`anchor_today`（YYYY-MM-DD，写进 overview）、`repeats`（CLI 的 `--repeats`，只是记录一下，不参与计算）。
- 返回：见 §5 的报告 JSON。
- 错误：本身不抛异常。分组时若 failure 缺 `root_cause` 会 `KeyError`，属于用例不合法，应在 `load_cases` 阶段被拦截。
- 四条语义：
  - **不可判定用例不进失败分组**，只进 `overview.environment` 和 `by_stage.environment`。环境故障不是靶子的错。
  - 失败断言按 `(category, root_cause)` 分组：跨用例同根因只算一组，`cases`、`whys`、`landings` 按字符串去重，`evidence` 逐条保留（含 check/expect/observed/outcome_status），组内按 root_cause 字符串排序。
  - **不可靠通过（:490）**：用例判 pass，且任一已判定采样"零失败 +（refusal_accepted 或被跳过名单命中）"。判据是机械的（看本次通过到底靠什么拿到），不是用例里声明的。
    **语义层机制拒答除外**（`_is_lucky_pass` :471 → `_refused_by_system` :465，认 `run["error"]["type"] == "semantic_refusal"`，常量 `SEMANTIC_REFUSAL` :462）：这种 no_sql 是系统在调用模型之前拦的，每次都发生，属于机制不属于运气，连它顺带跳过的断言也不计进清单。模型自发拒答的 no_sql（error 为 null 或别的 type）仍是运气，R2 那类靠 `refusal_accepted` 识别。
  - `by_stage` 按阶段名排序，每个阶段统计 total/pass/fail/environment。

---

## 3. 断言目录（`CHECKS`，:301）

每条断言的形态都是 `check(expect) -> (passed: bool, observed: str)`，`observed` 直接作为报告里的证据。判定对象是 outcome 的原始字段，不做任何"修正"。

| check                  | expect 形状                                                                    | 判定口径                                                                                                                                                                                       | 拒答时跳过 | 实现（`judge.py` 行号）   |
| ---------------------- | ------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------- | ------------------------- |
| `status_in`            | `["blocked"]`                                                                  | `outcome.status` 在允许集合里                                                                                                                                                                  | 否         | `_check_status_in` :193   |
| `guard_passed`         | `false`                                                                        | `outcome.guard.passed == expect`；guard 为 null（压根没校验）时观测记 None -> 不满足                                                                                                           | 否         | `_check_guard_passed` :198 |
| `guard_rules_include`  | `[{"rule":"forbidden_function","token":"load_extension"}]`                     | 每条都得命中；`token` 可省略，省略就是只比 rule                                                                                                                                                | 否         | `_check_guard_rules_include` :204 |
| `guard_rules_exclude`  | `["forbidden_keyword"]`                                                        | violations 里不能出现这些 rule                                                                                                                                                                 | 否         | `_check_guard_rules_exclude` :215 |
| `sql_null`             | `true`                                                                         | `outcome.sql is None == expect`；observed 最多截 120 字符                                                                                                                                      | 否         | `_check_sql_null` :222    |
| `sql_matches`          | 正则                                                                           | SQL 归一化（小写 + 空白折叠）后 `re.search` 命中；`sql=None` 直接不满足                                                                                                                        | **是**     | `_check_sql_matches` :227  |
| `sql_not_matches`      | 正则                                                                           | 归一化后不命中；`sql=None` 视为满足（压根没有可以违规的内容）                                                                                                                                  | 否         | `_check_sql_not_matches` :234 |
| `result_none`          | `true`                                                                         | `outcome.result is None == expect`；blocked / no_sql / exec_error 下都是 None                                                                                                                  | 否         | `_check_result_none` :242 |
| `result_row_count`     | `40`                                                                           | `len(result.rows) == expect`；没有 result 时不满足                                                                                                                                             | **是**     | `_check_result_row_count` :247 |
| `result_empty_or_null` | `true`                                                                         | 必须真的拿到结果集才算：0 行，或者所有单元格"视为空"（None、数值 0、`""/"0"/"0.0"/"null"/"NULL"`；布尔 False **不算**空）。`result` 为 null（blocked / exec_error / no_sql）时两个方向都不满足 | **是**     | `_check_result_empty_or_null` :254 |
| `result_value_in`      | `{"values":[414786.3,354636.03],"tolerance":{"mode":"absolute","value":0.01}}` | 取结果里第一个**数值**单元格（int/float，布尔不算；字符串和 NULL 直接跳过），与任一允许值在容差内；全表没有数值单元格判不满足  | **是**     | `_check_result_value_in` :268 |
| `llm_response_matches` | 正则                                                                           | 对 `outcome.llm_response` 做 `re.search`（IGNORECASE）；observed 截 160 字符                                                                                                                   | **是**     | `_check_llm_response_matches` :296 |

**辅助函数**（不用改，读懂报告够用；知道它们做了什么才不会误判断言）：

```text
_normalized_sql(:157)     从 outcome["sql"] 取原文，小写（lower）+ 空白折叠。
                          所以写 sql 正则时不能依赖换行和原始大小写
_violations(:132)         从 outcome 里取 violations 列表（guard 为 null 时为空）
_rule_label(:137)         把 rule + 可选 token 渲染成 rule[token]
violation_labels(:142)    批量版：把一份 guard 的 violations 压成 rule[token] 列表，
                          报告校验行与进度行共用
_rows(:165)               没有 result 就返回 None（多数断言据此判"不满足"）
_is_blank(:171)           覆盖 SUM -> NULL 和 COALESCE(...,0) 两种"空"的写法；
                          数值 0 算空，但布尔 False 明确不算空
_first_value(:184)        result_value_in 的取值口径：取第一个数值单元格
                          （int/float，布尔不算）；维度列在前的 GROUP BY 结果不受字符串列影响，
                          0 是合法数值不会被当空跳过
_within_tolerance(:287)   absolute 比 |v-allowed|<=amount；relative 比 |v-allowed|/|allowed|<=amount
```

`result_value_in` 的取值口径要慎用：**它只看第一个数值，不校验列名，也不管返回了几列几行**。同一条 SQL 多返回一列就可能取错值。跨模型采样时建议配合 `sql_matches` 一起断言，先把 SQL 的形状固定住。

---

## 4. run_eval — `evaluation/run_eval.py`

这个模块负责实际执行：读库、调模型、跑用例。judge 只做判断，run_eval 负责产生被判断的 outcome。

### 4.1 `_available_months(db_path) -> list[str] | None`（:28）

自己用只读 URI（`sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)`）跑 `SELECT DISTINCT substr(order_date,1,7) FROM orders ORDER BY 1`，拿到库里实际有哪些月份，升序返回（形如 `["2026-04", ...]`）。注意它没走 executor 的 `open_readonly_connection`，只是同样用了只读 URI —— 预检只需要读，不依赖 authorizer 那套白名单。

读不到就返回 `None` —— 库不存在、表不存在、列不存在，任何 `sqlite3.Error` 都算读不到。**不抛异常**，让上层去决定怎么解释。

### 4.2 `_precheck_environment(case, months) -> str | None`（:40）

执行前先确认"演示库的数据满足这条用例声明的前提"。前提不成立就不必执行，直接判不可判定。

- 输入：`case`（只看 `requires_data_months` / `forbids_data_months` 两个键）、`months`（上面那个函数的结果）。
- 返回：`None` = 前提满足，可以执行；一个字符串 = 前提不满足，**这句话就是判不可判定的原因**。
- 规则：`months is None` -> 预检失败；缺了依赖的月份 / 出现了本该为空的月份 -> 把库实际覆盖了哪些月份报出来。
- **代码里不硬编码数据窗口**：以后改 `init_db.py` 的时间范围，这里跟着库的实际内容走，不会误报。

### 4.3 `_run_once(case, *, generator, db_path, max_rows, timeout_s, today) -> dict`（:53）

调一次 `pipeline.ask`，就这一次采样。

- 输入：`case`（有 `injected_sql` 就走注入，此时 `generator` 传 `None` —— **真模型一次都不调**）；其余 `db_path / max_rows / timeout_s / today` 全部来自 Config 和锚定日期。
- 返回：正常就是那个 outcome；runner 自身抛出异常时，转成 `{"status":"runner_crash","error":{type,message}}`。
- 说明：参数是 runner 自己组的，**CLI 没有 `--today` / `--injected-sql` 这些入口**（别去 cli 里找）。`today` 恒等于锚定日期；注入用例不走生成器时，这个值只是被 pipeline 忽略掉。

### 4.4 `run_cases(cases, *, generator, db_path, max_rows, timeout_s, today, repeats, months, on_run=None) -> list[dict]`（:94）

逐用例：采样 -> 判定 -> 给结论。整批评测的循环体在这。

- 输入：`repeats`（CLI 的 `--repeats`，会被用例级的 `case["repeats"]` 覆盖）、`months`（本批共享的预检依据，同一批内库内容不会变化）、`on_run`（进度回调，每有一次结果调用一次；不传就只推进内部计数）。
- 采样次数由 `_total_runs(case, repeats)`（:70）决定：`deterministic` 恒为 1（注入用例结果确定，跑三次是浪费）；其余 `max(1, case.get("repeats", repeats))`。
- 开跑前的量由 `plan_summary(cases, *, repeats, months)`（:75）算：`{cases, injected_runs, model_runs, skipped_cases, planned_runs}`，注入/模型/预检跳过分账，只用于打预估行，不参与判定。
- 返回：每个用例一条记录。

```text
[{
  id, stage, category, question, expected_status, expected_behavior, injected,
  runs: [{ run, status, sql, guard, enforcement, time_fix, error,  <- 这六项每次采样都原样记录（见下）
           failures?, passed_assertions?,
           refusal_accepted?, skipped_checks? }],      <- 后两者供 aggregate 识别不可靠通过
  failures: [...],                                     <- 跨采样按断言 index 去重
  decidable_runs, passing_runs,
  verdict, environment_reason?
}]
```

- `guard` 恒有键：放行是 `{"passed": true, "violations": []}`，拦截带全量 violations，`no_sql / llm_error / runner_crash` 为 null。**通过的用例也要能回看凭哪条规则过的**，不然分不清"拦对了理由"和"碰巧被别的规则拦了"。
- `enforcement` 同样每次采样都记录（语义层注入痕迹，形状见根目录 NOTES 的 outcome 字段字典）：`sql_matches status` 这类断言变绿时，报告要能回答"订单范围条件是模型自己写的，还是系统补进/改写的"（`changed` 与 `actions[].type`）；问题没匹配到指标时为 null。
- `time_fix` 同样每次采样都记录（语义层第二步·时间边界校验的痕迹，形状见根目录 NOTES）：边界断言变绿时，报告要能回答"这个边界是模型自己写对的，还是系统按字段类型改的"（`changed` 与 `actions[].type`）；SQL 里没有可识别的时间边界时为 null。K1 的根因就靠这个字段自证：`actions[].type=boundary_rewritten`，`to` 是 `< '下月第一天'`。
- `error`（:193）同样每次采样都记录：这次为什么没出数，只有一个机器可读来源——`semantic_refusal`（语义层在模型之前拒答，`message` 是拒答文案）/ `llm_error` / `sql_error` / `timeout` / runner 崩溃时的异常类名；正常出数或被 guard 拦下时为 null。原先 `llm_error` 的报错单独塞在 `environment` 键里，现在统一到 `error`，报告读一个字段就够。
- `skipped_checks`（:211）只出现在已判定的采样里：这次通过若靠"拒答 + 跳过名单"拿到，aggregate 认得出来。
- 五条规则:
  - 采样次数见上（`_total_runs`）。
  - `llm_error` / `runner_crash` 那几次**不进判定，也不进 passing/decidable 的分母**。环境故障既不算靶子答错，也不算答对。
  - 所有次数都不可判定 -> `verdict=environment`，原因写在 `environment_reason`。
  - 断言失败跨采样只留一次（按同 `index`）—— **避免把"重复 3 次命中同一个根因"报成 3 组失败**。
  - 预检不通过的用例不发采样事件，只发一条 `kind="skipped"` 事件（含 case_id / reason）。
- 进度事件（`on_run` 收到的字典）：`kind="run"` 带 seq / planned / case_id / category / injected / run / total_runs / status / elapsed_ms / guard / failed_assertions（llm_error、runner_crash 那次为 `None`，表示没判定）。
- 注意：记录里的 `verdict` 只由**已判定的**采样决定。

### 4.5 校验行与进度行

```text
_guard_brief(guard)      (:232)  一行说清放行还是拦截，拦截列出 rule[token]
_guard_lines(record)     (:242)  逐用例的校验行：各次采样结论一致合成一行并标
                                 "（N 次采样一致）"，不一致才按 run 展开
_progress_line(event)    (:255)  一次采样/一次跳过压成一行进度文本
make_progress_writer(stream) (:277)  进度回调：逐行 flush 写到调用方给的流
```

进度约定三条：**只写 stderr**（stdout 留给报告，重定向/管道不串味）；**每完成一次采样打一行**，格式 `[序号/计划总数] 用例 [类别] run k/n 注入|模型 -> status=… 耗时 …s 失败断言 … | 校验一行`；**开跑前先打一条预估**（来自 `plan_summary`，含模型调用次数与单次最长超时提示）。预估行带范围前缀（`阶段 v0` 或 `全量模式`）。模型用例串行跑、单次调用上限 60s（`llm_client.py:16`），全量 27 次调用时控制台空着会像卡死——进度行就是治这个的。

### 4.6 `render_text(report, *, cases_path, db_path, model, used_llm, exit_code) -> str`（:350）

把聚合结果渲染成分区文本报告，**两套抬头按 `run_context.mode` 切换**：

- `stage` 模式：`Data Agent 阶段评测报告 —— 阶段 v0（status=done）`，只出本阶段结果。
- `all` 模式：`Data Agent 全量差距报告 —— 距离完整规范还差什么`，多两段：逐阶段计数行（`v0（done） 共 2 条：通过 2，失败 0，不可判定 0`）和"拒答跳过的不可靠通过"分区（`_unreliable_pass_lines`，:330）。

固定内容：

- 概览行固定是 **通过 X / 失败 Y / 不可判定 Z（共 N 条）**，下一行固定 **检测到 N 条拒答跳过的不可靠通过**。第二行措辞刻意只声明"检测到的这一类"，不写"不可靠通过共 N 条"——避免读者以为没标的就是可靠的。
- 失败分区 `_failure_group_lines`（:295）：`● 类别 → ■ root_cause` 两层，每组渲染 `evidence` 前 4 条、`whys` 与 `landings` 全量。用例里写了"同上"就照原样输出，不做展开。两套模式共用。
- 逐用例明细行 `{id} [阶段/类别] {✅ 通过|❌ 失败|⚠️ 不可判定} 注入|真模型 通过重复 r/n | 期望 status=…`（符号在 `_verdict_label`，:286；结论列不做定宽对齐——emoji 字符宽度不定，pad 了反而错位）；失败行末尾带 `｜根因 xxx`。
- 明细里固定有一行 `校验: …`（`_guard_lines` 生成，见 §4.5）：**通过的用例也打印拦因或放行原因**，用来区分"拦对了理由"和"碰巧过"。

### 4.7 `main(argv=None) -> int`（:445）与 `compute_exit_code(report) -> int`（:426）

```text
CLI   --cases    默认 problems/eval_cases.json
      --out      默认 problems/eval_report.json
      --stage    只跑该阶段用例（如 --stage v0）；阶段名不在 meta.stages -> 退出码 2 并列可用阶段
      --all      全量模式：跑所有阶段、出差距报告（验收时手动跑）
                 --stage 与 --all 必须显式二选一，都没有或都给 -> parser.error
                 （不给默认值：防止手滑一条命令烧掉 27 次模型调用）
      --ids      逗号分隔，在 --stage/--all 选出的集合上再收窄；含未知 id -> 2
      --repeats  默认 3
      --today    默认取 meta.anchor_today，再退到 date.today()；非法 YYYY-MM-DD -> 2
      --progress / --no-progress  默认：stderr 是终端就开，重定向就关；
                                  强制看进度（如 tee）用 --progress

流程  读用例 -> 按 --stage 收窄 -> 按 --ids 收窄（过滤后为空 -> "[选择集为空]" 返回 2）
      -> use_llm = 本批是否存在非注入用例
      -> load_config(require_llm=use_llm) -> 库文件存在性检查 -> 锚定日期
      -> _available_months -> build_generator（仅 use_llm 时）
      -> 开跑前打预估行、逐采样打进度行（均 stderr，见 §4.5）
      -> run_cases -> aggregate -> 挂 run_context（mode/stage/stages/...）
      -> compute_exit_code -> 写 JSON 报告 -> 打印文本 -> 返回退出码

退出码  compute_exit_code 是纯函数，判别性测试直接打它：
        all 模式   gating 只看 done_stage_ids(meta.stages) ∩ 失败用例；
                   pending 阶段失败仅报告、不置错。done 现在是 [v0, v1]，
                   任一条阶段的失败都会把 --all 置成 1
        stage 模式 本阶段 overview.fail > 0 即 1，**不看 done/pending**
                   （所以 --stage v1 的退出码证明不了状态位生效，要验只能看 --all）
        之后 environment > 0 -> 2；否则 0
        （--strict 已随失败分类一起删除——没有"未实现"分类了，这个开关无事可做）

异常    ConfigError -> 捕获后返回 2，不往外冒
        OSError | json.JSONDecodeError -> 读用例失败，返回 2
```

**纯注入用例为什么不用 key 也能跑**：走 `load_config(require_llm=False)`，跳过 key/model 校验，`build_generator` 根本不会被调用；`used_llm` 会写进报告的 `run_context` 里自证（看报告就知道这批到底有没有花模型调用）。

---

## 5. 报告 JSON 结构（`problems/eval_report.json`）

```text
overview
  anchor_today    str    评测基准日，相对时间口径全靠它
  repeats         int    CLI --repeats 的值（不是用例实际采样了几次）
  cases           int    本批用例条数（含不可判定）
  pass / fail / environment  int
  unreliable_pass int    模型运气型不可靠通过条数（机制拒答不计入；单独计数，不影响 pass/fail 计数）
  verdict_values  list   本批实际出现过的结论取值（pass|fail|environment 的子集），排序后

by_stage[]        {stage, total, pass, fail, environment}，按阶段名排序；
                  阶段模式只有一项，全量模式每项一行也渲染进报告

failed_by_category[]     {category, cases[], root_causes[]}，按类别排序
  root_causes[]  组内按 root_cause 排序：
    root_cause, capability, cases[], whys[], landings[],
    evidence[]   {case, check, expect, observed, outcome_status}，逐条保留

unreliable_passes[] {id, stage, category, refused, skipped_checks[]}
  只收模型运气型的：refused=true 且 skipped_checks=[] -> 没有断言被跳过，纯靠 status_in
  接受拒答 + 模型自发拒答过关（R2 那类）。语义层机制拒答的采样（runs[].error.type=
  semantic_refusal）不进这里，也不贡献 refused/skipped_checks；要分清拒答是谁拒的，
  读 cases[].runs[].error。

cases[]           run_cases 的记录原样（stage / runs / failures / verdict /
                  passing_runs / decidable_runs ...）；
                  每个 run 都带 guard、enforcement、time_fix、error；已判定的还带 refusal_accepted、skipped_checks，离线复核够用

run_context       {mode(stage|all), stage, stages, cases_path, db_path, db_months,
                  model, used_llm}
                 stages = meta.stages 原样（status 是退出码规则的数据源）；
                 db_months = 预检当时库里的实际月份；model 只取 DATA_AGENT_MODEL，
                 未配置时是空串，文本行渲染成"（未配置）"；不读任何密钥
```

---

## 6. 用例文件的形状

```text
meta
  purpose / anchor_today / notes[] / cases(int)
  stages                必填非空对象：{阶段名: {status: done|pending, promise: str}}
                        status 是 --all 退出码的唯一数据源；promise 是该阶段的承诺范围
                        （纯展示文本，代码不读，判分与退出码只看 status；v1 的文案已按
                         实际实现收窄成「口径声明（不含澄清）、空结果标记、语义拒答」）

cases[]
  id                     "S1" ...        报告和 --ids 都用它
  stage                  "v0" | "v1"     必须 ∈ meta.stages 的键；--stage 按它过滤
  category               安全 | 口径 | 空结果 | 拒答      失败分组的分组键
  question               str             送进 ask 的自然语言
  deterministic          bool            true => injected_sql 必填，且只跑 1 次
  injected_sql           str | 缺省      有则走注入，跳过取表结构与模型调用
  expected_status        str             必须 ∈ STATUSES，否则 load_cases 直接报错
  expected_behavior      str             给人看的一句话
  capability             str             能力域：安全层 / 口径层 / 语义拒答层 ...
  repeats                int | 缺省      覆盖 --repeats
  requires_data_months   ["2026-09"]     预检：库里必须有这些月份
  forbids_data_months    ["2026-10"]     预检：库里必须没有这些月份
  assertions[]           {check, expect, root_cause, capability?, why, landing}
```

**已删除的字段**：用例级 `failure_type`、断言级 `on_fail`（失败预设分类）。用例里残留任何一个，`load_cases` 报"阶段制已删除失败预设分类"。

**`expected_status` 是个显示字段，不是判分字段**：它目前只用于报告里那句"期望 status=..."和取值合法性校验，真正判分看的是 `status_in` 断言。**两者不一致时以断言为准**，所以加用例要同时写对，不能指望 `expected_status` 自动生效。

### 加一条用例的清单

1. 期望全部写进 JSON，别在 Python 里加 if。
2. 每条断言都要带 `root_cause` + `why` + `landing`，少一个 `load_cases` 直接报错；`root_cause` 用短横线小写英文（`guard-misses-quoted-function`），报告按（类别, root_cause）分组。
3. 用例必须标 `stage`，取值以 `meta.stages` 为准；新阶段先在 `meta.stages` 里登记（做完把 status 从 pending 转 done，退出码自动开始拦它）。
4. done 阶段的用例把 `status_in` 放宽收 `no_sql` 之前，先手跑确认拒答是谁拒的：语义层机制拒答（`runs[].error.type=semantic_refusal`）是代码保证的，不进不可靠清单；模型自发拒答的 pass 会被标成不可靠通过。
5. 想让"合理拒答"判通过，就在 `status_in` 的期望里加 `no_sql`；不想让拒答直接通过就别加。
6. 问题里涉及"上个月/近 30 天"：先确认锚定日期下数据确实存在，必要时用 `requires_data_months` 把前提声明出来。
7. 涉及数学期望值：优先用相对容差，并在 `meta.notes` 里写明绑定的 `init_db.py` 种子与规模（改数据就得重算期望值）。
8. 安全类想要确定性：用 `deterministic` + `injected_sql`。注入照样过语义层（指标必需条件 + 时间边界）、guard 和只读执行，**不会绕过防线**。
9. 补完用例先单跑（`--stage <阶段> --ids <新id>`，注入类不花 key），看 `failed_by_category` 分组对不对，最后才跑全量。

---

## 7. 评测依赖的被测接口增量（细节见根目录 NOTES.md）

```text
pipeline.ask()  —— src/data_agent/pipeline.py:31（只留编排；每步实现在 pipeline_steps.py，一函数一步）
  语义拒答（run_refusal_gate，pipeline_steps.py:63；内部 graph.builder.run_semantic_gate :75）：
                              不通过就 status=no_sql、sql=null、llm_response=拒答文案、
                              error.type=semantic_refusal（:80）；通过才继续，
                              后面每一步都按原写法跑
  today: date | None = None        (:38)  基准日期，默认当前日期；原样传进生成器（pipeline_steps.py:116）
  injected_sql: str | None = None  (:39)  非空则跳过语义拒答检查、取表结构与模型调用，
                              把这条 SQL 当作模型输出；SQL 层语义检查（指标必需条件注入
                              enforce_metrics pipeline_steps.py:135、时间边界校验 fix_time :154）
                              与 guard (run_guard :170)、只读执行 (execute :189) 照常
                              与 generator 同时传 -> injected 优先 + warning (pipeline_steps.py:106)
                              两个都不传 -> ValueError (pipeline_steps.py:111)
                              注入路径 outcome.llm_response 与 caliber_note 都保持 null
                              （没有模型回复，就没有原始文本也没有口径声明）；
                              time_fix 不是注入路径专属：模型 SQL 同样会过这一步，
                              有可识别的时间边界时就有值
  debug: bool = False              (:40)  每步输入/输出各打印一行到 stderr，
                              不改返回值也不改业务逻辑；评测不传，走默认 False

SqlGenerator = Callable[[str, str, date], GeneratedSql]   llm_client.py:28
load_config(env_file=None, *, require_llm=True) -> Config  config.py:72
                              require_llm=False 只服务纯注入评测；此时 api_key/model 可为空，
                              不得拿去建 LLM 客户端
```

---

## 8. 已知注意事项（读报告前先过一遍）

- 注入用例不调模型，但仍然走 SQL 层的语义检查、guard 和执行 —— **测的是防线，不是模型**。注意语义**拒答**检查是问题级的，注入通道整个跳过（S1/S2 的问题文本只是标签，不是业务问题），所以 S1/S2 的 outcome 里不会出现 `semantic_refusal`。
- **评测不断言拒答原因，但报告看得见**：`run_eval` 每次采样都把 `outcome.error` 拷进 run 记录（`error` 键，:193），所以 `cases[].runs[].error.type == "semantic_refusal"` 直接在报告 JSON 里读得到，拒答文案在 `error.message`。仍没有的是**断言**：judge 没有"拒答原因/原因码"这类 check，R1/R3 只断言 `status_in / sql_null / sql_not_matches`，原因写歪了不会让用例变红。以后要断言原因，得先给 outcome 加结构化字段（当前被"对外返回字段一个都不能改"的约束挡着——原因码 `reason_code` 只存在于 `semantic_check` 的返回值里，不在 outcome 契约中）、再给 judge 加 check。
- 拒答发生在模型调用之前，所以 R1/R3 实际是 0 次模型调用：`plan_summary` 打的"模型 N 次"是按用例数预估的上界，真跑时这两条不会花钱，也不会因模型抖动翻绿又翻红——现在是代码保证的通过，不再是"碰巧拒答"，因此也不进不可靠通过清单（判据见 §2.6）。R2 现在的通过同样是机制拒答带来的，不再是运气。
- K1 变绿要看 `time_fix`：闭区间上界可能是**系统按字段类型改写成 `< 下月第一天`** 的，不代表模型自己会写边界。语义层第二步只修边界写法，**不校验时间字段选得对不对**（那是 K3 的 `sql_matches paid_at` 管的事）。
- 模型用例受温度影响，一次 pass 不代表稳定：看 `passing_runs/decidable_runs`，别只看 verdict。
- `result_value_in` 只比第一个数值单元格，不认列名，也不管行数；需要精确列时用 `sql_matches` 补一刀。
- `llm_response_matches` 只看模型原始回复。提示词已要求模型在 ```sql 代码块之外再输出一行「口径：」声明，所以 K 类"要声明口径"的断言有文字可读了；这段声明同时被单独抽进 `outcome.caliber_note`，但**评测层还没有对应的断言类型**（只有 `llm_response_matches` 能碰到它），要精确判"声明了哪三项"得先加 check。注意 K1/K3 的 `sql_matches` 已经管住了 **SQL 侧的公式形状**（必须出现 `amount - COALESCE(tax_amount)`、禁止整体相减），缺的是**声明侧**——即"声明说的口径"和"SQL 干的口径"是否同一件事，全项目至今没有任何断言比对这两个。缺口清单见 `docs/EVAL_DESIGN.md` §5。
- 拒答跳过只作用于 `status_in` 接受 `no_sql` 的用例；不接受时 `no_sql` 会判失败，这是有意的设计。
- "不可靠通过"只检测**模型自发拒答 / 断言被跳过**这一种运气（判据机械：refusal_accepted 或 skipped_checks，且 `error.type != semantic_refusal`）。清单为空只说明「没有靠模型运气拿到的通过」，其他形式的"碰巧过"（比如模型猜对数值）从来不在检测范围里。
- K1 / S2 的期望值绑演示库：`SEED=20261029`（`scripts/init_db.py:16`）、`ORDERS_PER_MONTH=160`（`scripts/init_db.py:25`）、customers=40。这组绑定已由 `tests/test_init_db.py` 钉成单测：按锚定日 2026-10-30 重建库，月份窗口（2026-04 ~ 2026-09）、行数（40 / 960）与 K1 的五个九月数值（净额两种口径、毛额、取消、退款）逐一对账，同种子两次构建逐行相等。改了种子或月度订单数，这条单测先红，提醒重算期望值——评测报告里的数值与行数断言随后也会红。
- 全量报告没有"按类别通过率"表了（老版 `by_category` 的产物）；类别只剩两个用途：失败分组的分组键、`unreliable_passes.category` 展示字段。
- 演示库不存在时 runner 直接返回退出码 2 并提示执行 `scripts/init_db.py`，不会打印完整异常栈。
- 报告里"通过重复 0/1"这类字符串是**用例 JSON 里写死的原文**（judge 不做模板展开）。要改措辞请改用例，不要改 judge。
- 运行入口：`uv run --no-sync python -m evaluation.run_eval --stage v0`（或 `--all`）。pytest 能直接 import `evaluation` 靠的是 `pyproject.toml` 里 `pythonpath = ["src", "."]`，把 `"."` 去掉测试立刻 ModuleNotFoundError。

### 已修：`result_empty_or_null` 的"没有 result"分支

早期实现（`_check_result_empty_or_null`，judge.py:254）在无 result 时返回 `not expect`，于是 `expect=false`（期望非空）会被"SQL 被拦、根本没执行"误判成 pass。现在统一成**两个方向都不满足**：0 行或全空单元格才算空结果集，`result is None` 只说明"没执行"，这件事由 `status_in` / `result_none` 负责。

判别性证据在 `tests/test_eval_judge.py`：`test_missing_result_satisfies_neither_direction` 在修复前必红（`expect=false` 那半），`test_empty_and_nonempty_result_sets_are_distinguished` 守住 0 行 / 全空 / 有值三种正常分支没被改坏。

---

## 9. 改评测代码时的验证约定

```bash
# 全量单测（含 judge 判别性、聚合/渲染、退出码闸门与进度行）
uv run --no-sync python3 -m pytest -q

# 日常模式：v0 阶段 = 2 条注入用例，不花 key，也不受数据窗口抖动影响
uv run --no-sync python -m evaluation.run_eval --stage v0

# 验收模式：9 条真模型用例 × --repeats(默认 3) = 27 次调用（由人确认后跑）
uv run --no-sync python -m evaluation.run_eval --all
```

- judge 的改动必须有**双向判别证据**：同一用例，构造"修复后的 outcome"必须判通过，构造"当前 v0 的 outcome"必须判失败并给出预期的 `root_cause`（见 `tests/test_eval_judge.py`）。
- 退出码规则（"只看 done 阶段"）的改动必须有翻转证据：`tests/test_eval_report.py` 里把 v0 从 done 翻成 pending，同一份失败报告退出码必须从 1 变 0 —— 证明规则读的是 `meta.stages` 数据，不是硬编码阶段名。
- 用例文件的改动先过 `judge.load_cases`，再用 `--stage v0 --ids <id>` 单跑，别一上来就花 key。
- judge 与 run_eval 的分工不能破：judge 保持不 import pipeline、不读库；只有 run_eval 碰被测系统和演示库。
- 契约同步顺序：outcome 字段变 -> 根目录 `NOTES.md` + `README.md`；用例期望变 -> `meta.notes`；断言语义变 -> 本文件 §3 的表格；阶段增删/status 变 -> 只改 `meta.stages`，代码不动。
