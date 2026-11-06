"""评测 runner：读用例 -> 跑 pipeline.ask -> 判 -> 按阶段制聚合成报告。

用例内容全部来自 problems/eval_cases.json，本文件不内联任何期望值。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from datetime import date
from pathlib import Path
from typing import Callable

from data_agent.config import ConfigError, ensure_db_exists, load_config
from data_agent.pipeline import ask
from data_agent.services.llm_client import REQUEST_TIMEOUT_S, build_generator
from evaluation import judge
from evaluation.judge import CaseSpecError, VERDICT_ENVIRONMENT

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = PROJECT_ROOT / "problems" / "eval_cases.json"
DEFAULT_OUT = PROJECT_ROOT / "problems" / "eval_report.json"


def _available_months(db_path: Path) -> list[str] | None:
    """读演示库实际覆盖的月份，用于按用例声明做预检（不在代码里硬编码数据窗口）。"""
    try:
        with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
            rows = conn.execute(
                "SELECT DISTINCT substr(order_date, 1, 7) FROM orders ORDER BY 1"
            ).fetchall()
    except sqlite3.Error:
        return None
    return [row[0] for row in rows]


def _precheck_environment(case: dict, months: list[str] | None) -> str | None:
    """用例声明的数据前提不满足时返回原因，判为不可判定而不是缺陷。"""
    if months is None:
        return "无法读取演示库月份覆盖（orders.order_date），预检失败"
    missing = [month for month in case.get("requires_data_months", []) if month not in months]
    if missing:
        return f"演示库缺少用例依赖的月份: {missing}（实际覆盖 {months}）"
    extra = [month for month in case.get("forbids_data_months", []) if month in months]
    if extra:
        return f"演示库里出现了用例预期为空的月份: {extra}（实际覆盖 {months}）"
    return None


def _run_once(case: dict, *, generator, db_path: Path, max_rows: int, timeout_s: float, today: date) -> dict:
    """跑一次链路；意外异常与 llm_error 都转成不可判定，不算靶子缺陷。"""
    injected = case.get("injected_sql")
    try:
        return ask(
            case["question"],
            generator=None if injected else generator,
            db_path=db_path,
            max_rows=max_rows,
            query_timeout_s=timeout_s,
            today=today,
            injected_sql=injected,
        )
    except Exception as exc:  # noqa: BLE001 - runner 要把任何崩溃转成不可判定
        return {"status": "runner_crash", "error": {"type": type(exc).__name__, "message": str(exc)}}


def _total_runs(case: dict, repeats: int) -> int:
    """用例计划采样次数：注入用例恒 1，其余按 --repeats，用例级 repeats 可覆盖。"""
    return 1 if case.get("deterministic") else max(1, int(case.get("repeats", repeats)))


def plan_summary(cases: list[dict], *, repeats: int, months: list[str] | None) -> dict:
    """开跑前把“要发多少次模型调用”算清楚，用来打进度预估行。"""
    injected_runs = model_runs = skipped_cases = 0
    for case in cases:
        if case.get("injected_sql"):
            injected_runs += _total_runs(case, repeats)
        elif _precheck_environment(case, months):
            skipped_cases += 1
        else:
            model_runs += _total_runs(case, repeats)
    return {
        "cases": len(cases),
        "injected_runs": injected_runs,
        "model_runs": model_runs,
        "skipped_cases": skipped_cases,
        "planned_runs": injected_runs + model_runs,
    }


def run_cases(
    cases: list[dict],
    *,
    generator,
    db_path: Path,
    max_rows: int,
    timeout_s: float,
    today: date,
    repeats: int,
    months: list[str] | None,
    on_run: Callable[[dict], None] | None = None,
) -> list[dict]:
    """逐用例跑重复采样，收集失败断言并给出用例结论；on_run 每有一次结果就回调一次。"""
    records = []
    planned = sum(
        0
        if (not case.get("injected_sql") and _precheck_environment(case, months))
        else _total_runs(case, repeats)
        for case in cases
    )
    seq = 0

    def emit(case: dict, outcome: dict, run_index: int, total_runs: int, failed: int | None) -> None:
        """把一次采样的过程量交给进度回调；没传回调时只推进计数。"""
        nonlocal seq
        seq += 1
        if on_run is None:
            return
        on_run(
            {
                "kind": "run",
                "seq": seq,
                "planned": planned,
                "case_id": case["id"],
                "category": case["category"],
                "injected": bool(case.get("injected_sql")),
                "run": run_index,
                "total_runs": total_runs,
                "status": outcome.get("status"),
                "elapsed_ms": outcome.get("elapsed_ms"),
                "guard": outcome.get("guard"),
                "failed_assertions": failed,
            }
        )

    for case in cases:
        record = {
            "id": case["id"],
            "stage": case["stage"],
            "category": case["category"],
            "question": case["question"],
            "expected_status": case["expected_status"],
            "expected_behavior": case["expected_behavior"],
            # 挂账豁免随记录透传进报告：不带该块的用例恒为 null，报告契约键稳定
            "known_defect": case.get("known_defect"),
            "injected": bool(case.get("injected_sql")),
            "runs": [],
            "failures": [],
        }

        reason = _precheck_environment(case, months) if not record["injected"] else None
        if reason:
            record["verdict"] = VERDICT_ENVIRONMENT
            record["environment_reason"] = reason
            records.append(record)
            if on_run:
                on_run(
                    {
                        "kind": "skipped",
                        "case_id": case["id"],
                        "category": case["category"],
                        "planned": planned,
                        "reason": reason,
                    }
                )
            continue

        total_runs = _total_runs(case, repeats)
        seen_indexes: set[int] = set()
        for run_index in range(1, total_runs + 1):
            outcome = _run_once(
                case,
                generator=generator,
                db_path=db_path,
                max_rows=max_rows,
                timeout_s=timeout_s,
                today=today,
            )
            status = outcome.get("status")
            # 每次采样都留 guard 原样输出：通过的用例也要能回看是凭哪条规则过的；
            # enforcement 同样保留：SQL 里的订单范围条件是模型写的还是系统补的，报告要说得清；
            # time_fix 同样保留：边界写法是模型写的还是系统按字段类型改的，报告也要说得清；
            # error 同样保留：no_sql 是语义层机制拒答（type=semantic_refusal）还是模型自己拒的，
            # aggregate 判"不可靠通过"就看这个字段；
            # retry 同样保留（阶段 1 报错回炉）：回没回炉、修成什么样、最后救没救回来，
            # 台账要的"重试率"就从这条字段统计；没回炉的采样这里是 null
            run = {
                "run": run_index,
                "status": status,
                "sql": outcome.get("sql"),
                "guard": outcome.get("guard"),
                "enforcement": outcome.get("enforcement"),
                "time_fix": outcome.get("time_fix"),
                "error": outcome.get("error"),
                "retry": outcome.get("retry"),
            }
            if status in ("llm_error", "runner_crash"):
                record["runs"].append(run)
                emit(case, outcome, run_index, total_runs, None)
                continue

            evaluation = judge.evaluate_case(case, outcome)
            failures = [
                {**failure, "outcome_status": status}
                for failure in judge.failures_of(evaluation)
            ]
            run["failures"] = failures
            run["refusal_accepted"] = evaluation["refusal_accepted"]
            run["passed_assertions"] = sum(
                1 for item in evaluation["assertions"] if item["status"] == "pass"
            )
            # 拒答跳过的断言名要留下：aggregate 靠它识别"运气 pass"
            run["skipped_checks"] = judge.skipped_checks_of(evaluation)
            record["runs"].append(run)
            for failure in failures:
                if failure["index"] not in seen_indexes:
                    seen_indexes.add(failure["index"])
                    record["failures"].append(failure)
            emit(case, outcome, run_index, total_runs, len(failures))

        judged_runs = [run for run in record["runs"] if "failures" in run]
        record["decidable_runs"] = len(judged_runs)
        record["passing_runs"] = sum(1 for run in judged_runs if not run["failures"])
        if not judged_runs:
            record["verdict"] = VERDICT_ENVIRONMENT
            record["environment_reason"] = "所有重复都因 LLM 故障/runner 崩溃无法判定"
        else:
            record["verdict"] = judge.decide_verdict(judged_runs)
        records.append(record)

    return records


def _guard_brief(guard: dict | None) -> str:
    """一行说清这次校验是放行还是拦截，拦截时把 rule[token] 全列出来。"""
    if guard is None:
        return "未做静态校验（no_sql / llm_error / runner 崩溃）"
    rules = judge.violation_labels(guard)
    if guard.get("passed"):
        return "放行 passed=True，violations=（无）"
    return f"拦截 passed=False ← {', '.join(rules)}" if rules else "拦截 passed=False，但 violations 为空"


def _guard_lines(record: dict) -> list[str]:
    """逐用例的校验行：各次采样结论相同就合成一行，不同才按 run 展开。"""
    runs = record.get("runs") or []
    if not runs:
        return []
    briefs = [_guard_brief(run.get("guard")) for run in runs]
    distinct = list(dict.fromkeys(briefs))
    if len(distinct) > 1:
        return [f"校验[run{run['run']}]: {brief}" for run, brief in zip(runs, briefs)]
    suffix = f"（{len(briefs)} 次采样一致）" if len(briefs) > 1 else ""
    return [f"校验: {distinct[0]}{suffix}"]


def _progress_line(event: dict) -> str:
    """把一次采样（或一次预检跳过）压成一行进度文本。"""
    if event["kind"] == "skipped":
        return (
            f"[{event['case_id']}] [{event['category']}] 预检不通过 -> 不可判定："
            f"{event['reason']}"
        )
    elapsed = event.get("elapsed_ms")
    cost = f"{elapsed / 1000:.1f}s" if isinstance(elapsed, (int, float)) else "?"
    tail = (
        f" 失败断言 {event['failed_assertions']}"
        if isinstance(event.get("failed_assertions"), int)
        else ""
    )
    tag = "注入" if event["injected"] else "模型"
    return (
        f"[{event['seq']:>2}/{event['planned']}] {event['case_id']} "
        f"[{event['category']}] run{event['run']}/{event['total_runs']} {tag}"
        f" -> status={event['status']} 耗时 {cost}{tail} | {_guard_brief(event.get('guard'))}"
    )


def make_progress_writer(stream) -> Callable[[dict], None]:
    """进度回调：写到指定流（stderr），逐行 flush，管道里也能实时看到。"""

    def write(event: dict) -> None:
        print(_progress_line(event), file=stream, flush=True)

    return write


def _verdict_label(verdict: str) -> str:
    """把用例结论转成"符号 + 中文"标签，明细行扫一眼即可分清真过与红。"""
    return {
        judge.VERDICT_PASS: "✅ 通过",
        judge.VERDICT_FAIL: "❌ 失败",
        judge.VERDICT_ENVIRONMENT: "⚠️ 不可判定",
    }.get(verdict, verdict)


def _failure_group_lines(report: dict) -> list[str]:
    """失败断言按能力域分组的渲染块，阶段报告与全量报告共用。"""
    lines = [
        "-" * 72,
        "失败断言（按能力域分组，同一根因合并；证据每根因最多 4 条）",
        "-" * 72,
    ]
    if not report["failed_by_category"]:
        lines.append("（本轮没有失败用例）")
        lines.append("")
        return lines
    for bucket in report["failed_by_category"]:
        lines.append(f"● 类别 [{bucket['category']}] 命中用例: {', '.join(bucket['cases'])}")
        for group in bucket["root_causes"]:
            lines.append(
                f"    ■ {group['root_cause']}（能力域: {group['capability']}）"
                f" 命中用例: {', '.join(group['cases'])}"
            )
            for why in group["whys"]:
                lines.append(f"        为什么算问题: {why}")
            for evidence in group["evidence"][:4]:
                observed = evidence["observed"]
                status_note = (
                    "" if "status=" in observed else f"（status={evidence['outcome_status']}）"
                )
                lines.append(
                    f"        观测[{evidence['case']}] {evidence['check']} "
                    f"期望={json.dumps(evidence['expect'], ensure_ascii=False)} → {observed}{status_note}"
                )
            for landing in group["landings"]:
                lines.append(f"        建议落点: {landing}")
        lines.append("")
    return lines


def _unreliable_pass_lines(report: dict) -> list[str]:
    """全量报告的不可靠通过分区。措辞只声明"检测到的这一类"，不代表没标就可靠。"""
    items = report["unreliable_passes"]
    lines = [
        "-" * 72,
        "拒答跳过的不可靠通过（模型自发拒答、或断言被跳过；语义层机制拒答的 no_sql 不在此列）",
        "-" * 72,
        f"检测到 {len(items)} 条拒答跳过的不可靠通过",
    ]
    for item in items:
        detail = (
            f"跳过断言: {', '.join(item['skipped_checks'])}"
            if item["skipped_checks"]
            else "无断言被跳过，但 status_in 接受了拒答、靠模型自发拒答过关"
        )
        lines.append(f"  {item['id']} [{item['stage']}/{item['category']}] {detail}")
    lines.append("")
    return lines


def render_text(report: dict, *, cases_path: Path, db_path: Path, model: str, used_llm: bool, exit_code: int) -> str:
    """把聚合结果渲染成终端可读的分区报告；阶段模式与全量模式两套抬头。"""
    ctx = report["run_context"]
    overview = report["overview"]
    stages = ctx["stages"]
    lines = []
    lines.append("=" * 72)
    if ctx["mode"] == "all":
        lines.append("Data Agent 全量差距报告 —— 距离完整规范还差什么")
    else:
        stage = ctx["stage"]
        stage_status = (stages.get(stage) or {}).get("status", "未标注")
        lines.append(f"Data Agent 阶段评测报告 —— 阶段 {stage}（status={stage_status}）")
    lines.append(
        f"用例 {cases_path} | 锚定日期 {overview['anchor_today']} | 演示库 {db_path}"
    )
    lines.append(
        f"模型 {model or '（未配置）'} | 本轮{'调用 LLM' if used_llm else '未调用 LLM（纯注入用例）'}"
        f" | 非确定性用例重复 {overview['repeats']} 次"
    )

    if ctx["mode"] == "all":
        for bucket in report["by_stage"]:
            stage_status = (stages.get(bucket["stage"]) or {}).get("status", "未标注")
            lines.append(
                f"{bucket['stage']}（{stage_status}） 共 {bucket['total']} 条："
                f"通过 {bucket['pass']}，失败 {bucket['fail']}，不可判定 {bucket['environment']}"
            )
    lines.append(
        f"通过 {overview['pass']} / 失败 {overview['fail']} / 不可判定 {overview['environment']}"
        f"（共 {overview['cases']} 条）"
    )
    if overview.get("exempted"):
        lines.append(
            f"其中 {overview['exempted']} 条失败处于挂账豁免（known_defect：红照报，不拦 --all 退出码；账见逐用例明细）"
        )
    lines.append(f"检测到 {overview['unreliable_pass']} 条拒答跳过的不可靠通过")

    if ctx["mode"] == "all":
        done = ", ".join(judge.done_stage_ids(stages)) or "（无）"
        lines.append(
            f"退出码 {exit_code}（只看已完成阶段 [{done}] 的失败；pending 阶段失败仅报告不置错；"
            f"存在不可判定 -> 2）"
        )
    else:
        lines.append(f"退出码 {exit_code}（0 该阶段无失败 / 1 有失败 / 2 有不可判定）")
    lines.append("")

    lines.extend(_failure_group_lines(report))

    if ctx["mode"] == "all":
        lines.extend(_unreliable_pass_lines(report))

    lines.append("-" * 72)
    lines.append("逐用例明细")
    lines.append("-" * 72)
    for record in report["cases"]:
        ratio = f"{record.get('passing_runs', 0)}/{record.get('decidable_runs', 0)}"
        flag = "注入" if record["injected"] else "真模型"
        lines.append(
            f"{record['id']:<3} [{record['stage']}/{record['category']}] "
            f"{_verdict_label(record['verdict'])} "
            f"{flag} 通过重复 {ratio} | 期望 status={record['expected_status']}"
        )
        lines.append(f"    问题: {record['question']}")
        lines.append(f"    期望行为: {record['expected_behavior']}")
        for guard_line in _guard_lines(record):
            lines.append(f"    {guard_line}")
        if record.get("environment_reason"):
            lines.append(f"    不可判定原因: {record['environment_reason']}")
        for failure in record["failures"]:
            lines.append(
                f"    ✗ {failure['check']} 期望={json.dumps(failure['expect'], ensure_ascii=False)}"
                f" → {failure['observed']}｜根因 {failure['root_cause']}"
            )
        known_defect = record.get("known_defect")
        if known_defect and record["verdict"] == judge.VERDICT_FAIL:
            lines.append(
                f"    ⚠ 挂账豁免 {known_defect['id']}：根因 {known_defect['root_cause']}"
                f"｜修复归属 {known_defect['fix_stage']}｜解除条件 {known_defect['release_condition']}"
            )
        if record["injected"]:
            lines.append(f"    注入 SQL: {record['runs'][0]['sql'] if record.get('runs') else '（未执行）'}")
        lines.append("")

    return "\n".join(lines)


def compute_exit_code(report: dict) -> int:
    """退出码闸门：全量模式只看 done 阶段的失败，阶段模式看本阶段失败；
    带 known_defect 挂账块的失败用例在全量模式下豁免（红照报、不拦门），阶段模式不豁免。"""
    ctx = report["run_context"]
    if ctx["mode"] == "all":
        done = set(judge.done_stage_ids(ctx["stages"]))
        gating_fail = any(
            record["verdict"] == judge.VERDICT_FAIL
            and record["stage"] in done
            and not record.get("known_defect")
            for record in report["cases"]
        )
    else:
        gating_fail = report["overview"]["fail"] > 0

    if gating_fail:
        return 1
    if report["overview"]["environment"]:
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    """评测入口。"""
    parser = argparse.ArgumentParser(description="按 problems/eval_cases.json 做阶段制评测")
    parser.add_argument("--cases", default=str(DEFAULT_CASES), help="用例 JSON 路径")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="报告 JSON 输出路径")
    parser.add_argument("--stage", default=None, help="只跑该阶段的用例，如 --stage v0")
    parser.add_argument("--all", action="store_true", help="全量模式：跑所有阶段，出差距报告（验收时手动跑）")
    parser.add_argument("--ids", default="", help="只跑指定用例 id，逗号分隔")
    parser.add_argument("--repeats", type=int, default=3, help="非确定性用例重复次数")
    parser.add_argument("--today", default=None, help="锚定基准日期 YYYY-MM-DD，默认取用例 meta.anchor_today")
    parser.add_argument(
        "--progress",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="把每次采样的进度打到 stderr；默认交互终端开、重定向时关",
    )
    args = parser.parse_args(argv)

    # 必须显式二选一：不给默认值，防止手滑全量烧掉 27 次模型调用
    if args.all and args.stage:
        parser.error("--stage 与 --all 只能二选一")
    if not args.all and not args.stage:
        parser.error("必须显式指定 --stage <阶段名> 或 --all")

    try:
        meta, cases = judge.load_cases(Path(args.cases))
    except (CaseSpecError, OSError, json.JSONDecodeError) as exc:
        print(f"[用例文件不可用] {exc}", file=sys.stderr)
        return 2

    stages = meta.get("stages") or {}
    if args.stage:
        if args.stage not in stages:
            print(
                f"[阶段不存在] --stage {args.stage}；可用阶段: {list(stages)}",
                file=sys.stderr,
            )
            return 2
        cases = [case for case in cases if case["stage"] == args.stage]

    if args.ids:
        wanted = {item.strip() for item in args.ids.split(",") if item.strip()}
        unknown = wanted - {case["id"] for case in cases}
        if unknown:
            print(f"[用例文件不可用] 未知用例 id: {sorted(unknown)}", file=sys.stderr)
            return 2
        cases = [case for case in cases if case["id"] in wanted]

    if not cases:
        print("[选择集为空] --stage/--ids 过滤后没有用例可跑", file=sys.stderr)
        return 2

    use_llm = any(not case.get("injected_sql") for case in cases)
    try:
        config = load_config(require_llm=use_llm)
        ensure_db_exists(config)
    except ConfigError as exc:
        print(f"[CONFIG ERROR] {exc}", file=sys.stderr)
        return 2

    anchor_text = args.today or meta.get("anchor_today") or date.today().isoformat()
    try:
        anchor = date.fromisoformat(anchor_text)
    except ValueError:
        print(f"[CONFIG ERROR] --today 不是合法 YYYY-MM-DD: {anchor_text}", file=sys.stderr)
        return 2

    months = _available_months(config.db_path)
    generator = build_generator(config) if use_llm else None

    show_progress = args.progress if args.progress is not None else sys.stderr.isatty()
    on_run = make_progress_writer(sys.stderr) if show_progress else None
    if show_progress:
        plan = plan_summary(cases, repeats=args.repeats, months=months)
        ceiling = (
            f"；每次模型调用最长 {REQUEST_TIMEOUT_S:.0f}s，重试由 SDK 控制"
            if plan["model_runs"]
            else ""
        )
        scope = "全量模式" if args.all else f"阶段 {args.stage}"
        print(
            f"[进度] {scope}，共 {plan['cases']} 条用例：计划采样 {plan['planned_runs']} 次"
            f"（注入 {plan['injected_runs']} 次不调模型，模型 {plan['model_runs']} 次）"
            f"，预检跳过 {plan['skipped_cases']} 条{ceiling}",
            file=sys.stderr,
            flush=True,
        )

    records = run_cases(
        cases,
        generator=generator,
        db_path=config.db_path,
        max_rows=config.max_rows,
        timeout_s=config.query_timeout_s,
        today=anchor,
        repeats=args.repeats,
        months=months,
        on_run=on_run,
    )
    report = judge.aggregate(records, anchor_today=anchor.isoformat(), repeats=args.repeats)
    report["run_context"] = {
        "mode": "all" if args.all else "stage",
        "stage": args.stage,
        "stages": stages,
        "cases_path": str(args.cases),
        "db_path": str(config.db_path),
        "db_months": months,
        "model": config.model,
        "used_llm": use_llm,
    }

    # 退出码闸门只看"已完成阶段"：阶段制规则写在 meta.stages，v1 转 done 不用改代码
    exit_code = compute_exit_code(report)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        render_text(
            report,
            cases_path=Path(args.cases),
            db_path=config.db_path,
            model=config.model,
            used_llm=use_llm,
            exit_code=exit_code,
        )
    )
    print(f"报告 JSON: {out_path}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
