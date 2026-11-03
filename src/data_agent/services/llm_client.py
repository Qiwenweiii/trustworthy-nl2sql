"""OpenAI 兼容 LLM 调用：输入问题 + schema，输出抽取后的 SQL。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Callable

from openai import OpenAI

from ..config import Config
from ..prompts import SYSTEM_PROMPT

_FENCE_RE = re.compile(r"```(?:sql)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
REQUEST_TIMEOUT_S = 60.0


@dataclass(frozen=True)
class GeneratedSql:
    """模型输出：抽取后的 SQL（可能为 None）、原始文本与代码块外的口径声明。"""

    sql: str | None
    raw_response: str
    caliber_note: str | None = None


SqlGenerator = Callable[[str, str, date], GeneratedSql]


def extract_sql(text: str) -> str | None:
    """从模型回复中提取 SQL；NO_SQL 或空内容返回 None。"""
    text = (text or "").strip()
    if not text:
        return None
    fenced = _FENCE_RE.search(text)
    candidate = (fenced.group(1) if fenced else text).strip()
    if not candidate or candidate.upper().startswith("NO_SQL"):
        return None
    return candidate


def extract_caliber_note(text: str) -> str | None:
    """取 ```sql 代码块之外的文字作为口径声明；模型只回 SQL 时返回 None。"""
    text = text or ""
    outside_fence = _FENCE_RE.sub("", text)
    if outside_fence == text:
        # 没有代码块可切，SQL 与说明分不开，不能把 SQL 本身当成口径声明
        return None
    return outside_fence.strip() or None


def build_generator(cfg: Config) -> SqlGenerator:
    """构造一个绑定了配置与提示词的 SQL 生成函数；基准日期由每次调用传入。"""
    client = OpenAI(
        api_key=cfg.api_key, base_url=cfg.base_url, timeout=REQUEST_TIMEOUT_S
    )

    def generate(question: str, schema_text: str, today: date) -> GeneratedSql:
        """调用模型生成 SQL，并保留原始回复用于诊断。"""
        system_prompt = SYSTEM_PROMPT.replace("{today}", today.isoformat()).replace(
            "{schema}", schema_text
        )
        response = client.chat.completions.create(
            model=cfg.model,
            temperature=0,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": question},
            ],
        )
        # print(f"LLM 回复: {response.choices[0].message.content!r}")
        raw = (response.choices[0].message.content or "").strip()
        return GeneratedSql(
            sql=extract_sql(raw),
            raw_response=raw,
            caliber_note=extract_caliber_note(raw),
        )

    return generate
