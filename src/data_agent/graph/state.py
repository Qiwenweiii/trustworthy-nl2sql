"""图状态：每个 key 只有一个节点负责写，节点只返回自己那部分状态。"""

from __future__ import annotations

from typing import TypedDict


class SemanticGateState(TypedDict):
    question: str
    verdict: dict | None  # 由 semantic_check 节点写
    route: str | None     # 由 semantic_refuse 节点写；放行路径不经过它，保持 None
