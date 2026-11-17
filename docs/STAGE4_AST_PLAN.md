# 阶段 4 工程方案 —— AST 条件定位改写（KD-001 清偿）

> 状态：**已落地收口（2026-11-07）**。M1 块感知注入（`858c5bb`）→ M2 小采样 3 次命中派生表现场 → M3 验收轮 40/40（样本级 120/120、exempted=0）后 KD-001 销账（`18de4da`：用例账删挂账、快照 `docs/eval_report_v2_stage4_accept.json` 入库）；调用账 123 ≤ 130，四裁决点全部兑现。原立项证据：D15 两轮各红 2/3 的现场（快照 `docs/eval_report_v2_round1_r3.json` / `eval_report_v2_round2_r3.json`）；解除条件（D15 验收 3/3）已由数据达成。
> 本方案的原则和前面几期一样：**先定契约，再动代码；只动病灶，不顺手装修**。
>
> 文中「复盘 00X」是各阶段收口时的过程档案（本地留存，不随仓库分发）。本文的可核验证据一律是 `docs/eval_report_*.json` 快照与对应 git 提交，两者能独立复现同样的结论。

## 1. 问题是什么（一句话版）

系统补写口径条件时**不认识"子查询"**：它把整条 SQL 当一段文本找位置，条件被补到（或"已经存在"被误判在）**看不见 orders 表的那一层**，SQL 一执行就报"没有这一列"。D15 两轮各红 2/3 就是这个，公开挂账 KD-001。

## 2. 现场与根因（代码锚点）

失败形态（R1 run3 / R2 run1 未救回现场，复盘 003 台账）：

```sql
SELECT channel, cnt FROM
  (SELECT channel, COUNT(*) AS cnt FROM orders GROUP BY channel) AS t
WHERE status = 'paid'          -- 模型把口径写到了外层——外层只有 t 的列，没有 status
ORDER BY cnt DESC LIMIT 1
```

三处文本级判读各自怎么失守：

| 环节 | 代码 | 行为 | 病症 |
| --- | --- | --- | --- |
| 谓词扫描 | `services/metrics.py:130` `_iter_predicates` | 全文正则找 `status ...`，不看括号深度 | 外层的错位置谓词被当成"已满足" → 记 `already_present` 不动手，真正该有条件的内层（FROM orders 那层）没人管 |
| 插入点定位 | `services/metrics.py:183` `_append_where_condition` | 只认**顶层深度 0** 的 WHERE/子句关键字 | 补写只会补到外层——恰好是报错那一层 |
| 回炉复核 | `pipeline_steps.py:230` `retry_after_error` | 修好的 SQL 重走 `enforce_metrics`（本意是好的：回炉无豁免权） | 模型听报错删掉外层谓词（合法修复），复核判"缺失"→ 又补回同一错位置 → 第二次 exec_error，两投皆红 |

对照救回现场（R1 run1）：模型自己把谓词挪进了子查询——**人能看出来该放哪层，文本级代码看不出来**，这就是要修的差距。

## 3. 目标与非目标

**目标**：
- 口径条件的**判定**（已满足/冲突/缺失）和**插入**都按查询块（query block）进行：谓词只有落在"直接引用指标表的那个块"里才算数；补写落进那个块；
- D15 转普通门禁用例（解除条件兑现），v2 验收轮 40/40；
- 全程不新增运行时依赖，outcome 12 键契约与 `enforcement.actions` 四型不动——前端与评测零改动。

**非目标**（防止范围膨胀）：
- 不做通用 SQL 改写引擎/优化器，不重排模型写的任何东西；
- 不删外层错误谓词——"删模型的谓词"超出注入的权限边界，交给报错回炉处理（见 §5.4）；
- 不动 `time_boundary.py`（同款文本级病，但 T 组现全绿、风险敞口小，登记待立项）；
- 不动口径公式校验、不动 guard、不做"含退款"口径变体（那是语义层登记表扩展，另一笔账）。

## 4. 技术路线（裁决点 1）

| 路线 | 做法 | 优点 | 代价 |
| --- | --- | --- | --- |
| **B. 自研块感知（推荐）** | 在现有 `sql_text.mask_sql` 底座上加"查询块切分"：按括号深度 + `SELECT` 头切出各查询块（顶层、每个括号子查询、每个 CTE 支），块内用现成的 `referenced_tables` 判谁直接引用 orders；谓词扫描与插入点都限定在该块 span 内 | 零新依赖；SQL 仍是原文手术，`conflict_replaced` 的 from→to 证据、guard、既有测试全部不漂移；读不懂的形态沿用"宁可 skip 不乱动"纪律 | 自研切块只覆盖已知形态（单 SELECT/派生表/CTE），未知形态保守跳过——但这正是 v0 以来一贯的失败姿态 |
| A. 引入 sqlglot | 真 AST：parse → 按 scope 操作 → 重新生成 SQL | 语义完备，形态通吃 | 新重依赖；**输出是再生成文本不是原文**，diff 证据、`already_present` 判定、prompt 反馈形态全要重核，前端展示跟着变——为一个 D15 掀整张契约桌 |
| C. B 为主 + sqlglot 只做测试 oracle | 实现同 B；dev 依赖加 sqlglot，单测里对切块结果做交叉校验 | 多一层尺子 | 只在想让"自研切块"的可信度上保险时才值 |

推荐 **B**。项目对"读不懂"的一贯立场（`sql_text.py` 头注释，2026-11-05 用户确认口径）在 B 里原样成立：切块判读不懂 = skipped 不动手。若选 C，代价是一个 optional dev 依赖 + 少量测试代码，不动运行时。

## 5. 设计（按 B 展开）

### 5.1 查询块切分（`sql_text.py` 增一个函数）

`split_query_blocks(sql) -> list[Block]`，`Block = {"start", "end", "tables": {表名→别名}}`：
- 掩码文本上按括号深度配对；每个"以 `SELECT` 开头的作用域"是一块：顶层一块、每对括号内一块、`WITH x AS (...)` 每个支一块；
- 块内 `FROM/JOIN` 直接引用的表/子查询别名用现成 `referenced_tables` 语义取；
- 任何配对失败（括号未闭合、SELECT 头认不出）抛 `SqlScanError`，调用方沿用现有 skipped 通道。

### 5.2 谓词判定改作用域内（`_iter_predicates` 改造）

对指标的 `required_filters`（列 `status`、表 `orders`）：
- 找到**直接引用 orders 的块集合**；
- 谓词命中的 start 落在这些块 span 内 → 按现有规则判 satisfied/冲突；落在其他块（外层、引用别名 t 的层）→ **不计数**（外层那个 `status='paid'` 不再能冒充"已满足"）；
- 目标块集合不唯一（多块直接引用 orders、自连接歧义）→ 整体 skipped，detail 写明原因。

### 5.3 插入点改块内 WHERE（`_append_where_condition` 泛化）

现有逻辑（AND 加括号防优先级抢戏、分号去尾规则）平移为"在指定块 span 内找顶层子句关键字"——括号深度基准从整条 SQL 换成块起点。多过滤条件同块时逐条并进同一 WHERE。

### 5.4 权限边界：外层错误谓词不删

块内补写后，外层那个 `status='paid'` 原样留着——执行照报 `no such column`，回炉把报错原文喂回去，模型删掉它；修好的 SQL 重走复核，这次 `enforce_metrics` 认得块结构：内层条件在（`already_present`），放行 → ok。D15 绿路径 = 首稿必回炉、第二次调用救回，**每题 2 次调用**，预算账按此算（§7）。
激进方案（算每层可见列集、主动删外层谓词）作为备选写在裁决点 3，不推荐：删比补的证据风险大一档。

### 5.5 契约与记录

`enforcement` 记录形状一字不动（四型 + `changed` + `detail`）；变化只在 `detail` 话术：added 补一句块位置（如"已补入查询 orders 表的子查询内"）。前端 `types.ts`/`EvidencePanel` 把 detail 当纯文本渲染，零改动；`/api/meta`、SSE 帧不动。

## 6. 测试方案（判别性纪律：先红后绿）

单测夹具（新建 `tests/test_metrics_blocks.py` + 扩充 `tests/test_metrics.py`）：

| 夹具 | 现场来源 | 旧实现（必须先红） | 新实现期望 |
| --- | --- | --- | --- |
| F1 派生表 + 外层 `status='paid'` | D15 R1 run3 原文形状 | `already_present`、SQL 不动 | `added` 补进内层块，`changed=True`，外层原文保留 |
| F2 回炉复核：谓词全无的派生表 SQL | R1 run1 修复后形状 | 补到**顶层 WHERE**（错层） | 补进内层块 |
| F3 CTE：`WITH t AS (... FROM orders ...) SELECT ... FROM t WHERE status='paid'` | 同病另一皮 | `already_present` 误判 | 补进 CTE 支块 |
| F4 单层 `SELECT ... FROM orders` 无条件 | 现网主流形态 | （绿） | 行为与旧逐字一致——回归哨兵 |
| F5 两块同时直接引用 orders | 歧义形态 | — | 保守 `skipped`，detail 报原因 |
| F6 括号/引号未闭合 | 判读不懂通道 | — | `SqlScanError` → skipped，SQL 原样 |

另：`test_pipeline_retry.py` 补一条端到端桩——F1 形状首跑 exec_error → 回炉删外层 → 复核 `already_present` → ok（演示 D15 完整绿路径，0 模型调用走注入通道）。

验收梯度（全确定性 → 真采样 → 验收轮，每级过才进下一级）：
1. 全量 pytest（352 → 352+N passed）+ 确定性命路 `--all --ids S1,S2,R1,R2,R3 --repeats 1` 退出码 0；
2. 小采样 `--stage v2 --ids D15 --repeats 1~3`（**3~9 次调用**，另批）：核对现场是否真按 §5.4 绿路径走（看 `runs[].retry` 与 enforcement 记录）；
3. 验收轮 `--stage v2 --repeats 3`（约 120 次调用，D15 若触发回炉每次 2 调用，账按 126 封顶报）；
4. 释放条件（用例账上原文）：任一验收轮 D15 **3/3 全绿** → 收口提交删除 `known_defect` 块、恢复普通门禁用例，D15 从此进 `--all` 拦截。

## 7. 调用预算（裁决点 4）

评测 360 授权已用尽（复盘 003 账），本阶段全部另批：小采样 3~9 次 + 验收轮 ≈120~126 次 ≈ **130 次封顶**。两批分开授权：修完先跑小采样看形态，形态确认再批验收轮——不批"一路跑到绿"。

## 8. 里程碑

| 里程碑 | 内容 | 文件白名单 | 收口证据 |
| --- | --- | --- | --- |
| M0 | 本方案稿四裁决点拍板 | `docs/STAGE4_AST_PLAN.md` | 提交 + 裁决记录 |
| M1 | 切块函数 + metrics 改造 + §6 全部单测 | `services/sql_text.py`、`services/metrics.py`、`tests/test_metrics*.py`、`tests/test_pipeline_retry.py` | 先红后绿记录 + 全量 passed |
| M2 | 小采样 D15 现场核对 | 无代码改动 | `runs[].retry`/enforcement 现场贴台账 |
| M3 | 验收轮 + KD-001 销账 + 文档同步 | `problems/eval_cases.json`（删块）、README、NOTES、`evaluation/NOTES.md`、`docs/EVAL_DESIGN.md`、快照 `docs/eval_report_*.json` | 40/40×repeats 3 快照 + 销账提交 |

风险与对策：切块误判括号形态（函数括号 vs 子查询括号）→ 认"括号后紧跟 FROM/SELECT 语境"，判不准就 skip（失败姿态无害）；temperature=0 锁不住 schema 变化引起的形态漂移（复盘 004 §2 教训）→ 验收以当轮现场为准，不为凑绿改用例；D15 验收轮仍可能红（模型这次不写派生表）→ 红照报、按 release_condition 等下一轮，不做无界重跑。

## 9. 待裁决（2026-11-07 用户"按你推荐的来"，四条均按推荐落定）

1. **技术路线**：B（自研块感知，零依赖，推荐）/ A（sqlglot，掀契约桌）/ C（B + sqlglot 仅做测试 oracle）？
   【裁决：B。零新依赖，SQL 保持原文手术，契约与证据展示零漂移。】
2. **time_boundary 同病灶是否顺带**：建议**不动**、登记待立项（T 组现全绿，无验收压力；两条腿同改会让 M1 判别性测试翻倍）。
   【裁决：不动，登记待立项——同款"文本级不认识子查询"的病，账在本方案 §3 非目标。】
3. **权限边界**：外层错误谓词"不删、交回炉"（推荐，D15 绿路径 = 每题 2 调用）vs "主动删"（0 次回炉但要做逐层列集运算，证据与风险都大一档）？
   【裁决：不删、交回炉。系统只保证"真正查 orders 表的块里有必需条件"，删模型谓词超出注入权限。】
4. **调用预算**：§7 的 130 次封顶、两批分开授权（小采样先行），是否成立？
   【裁决：成立。M2 小采样 3~9 次随开工授权；验收轮 ≈120~126 次待现场形态确认后另行请示。】
