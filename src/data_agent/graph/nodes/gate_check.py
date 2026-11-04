"""检查节点：把问题交给判定模块，只写 verdict 这一个 key。"""

from __future__ import annotations

from data_agent.graph.state import SemanticGateState
from data_agent.services.semantic_check import check_question


def semantic_check_node(state: SemanticGateState) -> dict:
    return {"verdict": check_question(state["question"])}
