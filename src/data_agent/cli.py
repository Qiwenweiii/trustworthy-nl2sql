"""CLI 入口：默认人类可读输出，--json 输出结构化 outcome 供评测读取。"""

from __future__ import annotations

import argparse
import json
import sys

from .config import ConfigError, ensure_db_exists, load_config
from .pipeline import ask
from .services.llm_client import build_generator

_CELL_LIMIT = 40


def _cell(value, width: int) -> str:
    """把值转成定宽文本，超长截断并补省略号。"""
    text = "NULL" if value is None else str(value)
    if len(text) > width:
        text = text[: width - 1] + "…"
    return text.ljust(width)


def _render_result(result: dict) -> list[str]:
    """把 result 字段渲染成对齐的文本表格和统计行。"""
    columns = result["columns"]
    rows = result["rows"]
    widths = []
    for column in columns:
        longest = max([len(column)] + [len(_cell(row.get(column), _CELL_LIMIT).strip()) for row in rows])
        widths.append(min(max(longest, 4), _CELL_LIMIT))

    lines = ["结果:"]
    lines.append("  " + "  ".join(_cell(column, width) for column, width in zip(columns, widths)))
    for row in rows:
        lines.append(
            "  " + "  ".join(_cell(row.get(column), width) for column, width in zip(columns, widths))
        )
    lines.append(
        f"行数: {result['row_count']}"
        f" | 截断: {'是' if result['truncated'] else '否'}"
        f" (上限 {result['max_rows']})"
        f" | 查询耗时: {result['elapsed_ms']} ms"
    )
    return lines


def render(outcome: dict) -> str:
    """把 outcome 渲染成人类可读的多行文本。"""
    lines = [f"问题: {outcome['question']}", f"状态: {outcome['status']}"]

    if outcome["sql"]:
        lines.append("SQL:")
        lines.extend(f"  {line}" for line in outcome["sql"].splitlines())

    caliber = outcome.get("caliber_note")
    if caliber:
        lines.append("口径声明:")
        lines.extend(f"  {line}" for line in caliber.splitlines())

    enforcement = outcome.get("enforcement")
    if enforcement and enforcement.get("changed"):
        lines.append(f"指标强制（{enforcement['metric']}）:")
        for item in enforcement["actions"]:
            if item["type"] in ("added", "conflict_replaced"):
                lines.append(f"  - {item['detail']}")

    time_fix = outcome.get("time_fix")
    if time_fix and time_fix.get("changed"):
        lines.append("时间边界修正:")
        for item in time_fix["actions"]:
            if item["type"] == "boundary_rewritten":
                lines.append(f"  - {item['detail']}")

    if outcome["status"] == "blocked":
        violations = outcome["guard"]["violations"]
        lines.append(f"拦截规则 ({len(violations)} 条):")
        for item in violations:
            token = f" [{item['token']}]" if item.get("token") else ""
            lines.append(f"  - {item['rule']}: {item['detail']}{token}")
    elif outcome["status"] == "no_sql":
        error = outcome.get("error") or {}
        if error.get("type") == "semantic_refusal":
            # 两种 no_sql 要分得开：系统判定答不了（没调过模型），和模型自己回 NO_SQL
            lines.append("系统拒答（未调用模型、未生成 SQL）:")
            lines.append(f"  {error.get('message') or outcome['llm_response'] or '（空）'}")
        else:
            lines.append("模型没有给出 SQL。原始回复:")
            lines.append(f"  {outcome['llm_response'] or '（空）'}")
    elif outcome["status"] == "ok" and outcome["result"] is not None:
        result = outcome["result"]
        lines.extend(_render_result(result))
        if result.get("empty_result"):
            lines.append("提示: 当前条件下没有数据（空结果），这不是一个真实数值。")
    elif outcome["error"]:
        lines.append(f"错误 ({outcome['error']['type']}): {outcome['error']['message']}")

    lines.append(f"总耗时: {outcome['elapsed_ms']} ms")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：加载配置、检查演示库、执行查询并输出。"""
    parser = argparse.ArgumentParser(description="Data Agent v0：自然语言问数")
    parser.add_argument("question", nargs="+", help="要查询的业务问题")
    parser.add_argument("--json", action="store_true", help="只输出 JSON outcome")
    parser.add_argument(
        "--debug", action="store_true", help="把每一步的输入/输出打到 stderr"
    )
    args = parser.parse_args(argv)

    question = " ".join(args.question)

    try:
        config = load_config()
        ensure_db_exists(config)
    except ConfigError as exc:
        print(f"[CONFIG ERROR] {exc}", file=sys.stderr)
        return 2

    generator = build_generator(config)
    outcome = ask(
        question,
        generator=generator,
        db_path=config.db_path,
        max_rows=config.max_rows,
        query_timeout_s=config.query_timeout_s,
        debug=args.debug,
    )

    if args.json:
        print(json.dumps(outcome, ensure_ascii=False, indent=2))
    else:
        print(render(outcome))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
