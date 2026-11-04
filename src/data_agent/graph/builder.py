"""装配"语义检查 → 放行/拒答"这一个岔路。

图的边界只到这里：检查通过就从 END 出来交回 pipeline.ask() 继续走原来的
生成 SQL → 强制注入 → 时间边界校验 → 静态校验 → 只读执行；那几步不是图节点，
一行代码都没改。拒答才进 semantic_refuse 节点。

不接 checkpointer：本步是单次判定，没有断点恢复需求。
"""

from __future__ import annotations

from functools import lru_cache

from langgraph.graph import END, START, StateGraph

from .nodes.gate_check import semantic_check_node
from .nodes.gate_refuse import semantic_refuse_node
from .state import SemanticGateState

ROUTE_GENERATE = "generate"
ROUTE_REFUSE = "refuse"


def route_after_check(state: SemanticGateState) -> str:
    """条件边路由：纯函数，只读 state 里的判定结论，不产生副作用。"""
    verdict = state.get("verdict") or {}
    return ROUTE_GENERATE if verdict.get("allowed") else ROUTE_REFUSE


@lru_cache(maxsize=1)
def build_graph():
    """编译一次就复用：岔路结构固定，没有按请求重建的必要。"""
    builder = StateGraph(SemanticGateState)
    builder.add_node("semantic_check", semantic_check_node)
    builder.add_node("semantic_refuse", semantic_refuse_node)
    builder.add_edge(START, "semantic_check")
    builder.add_conditional_edges(
        "semantic_check",
        route_after_check,
        {ROUTE_REFUSE: "semantic_refuse", ROUTE_GENERATE: END},
    )
    builder.add_edge("semantic_refuse", END)
    return builder.compile()


def run_semantic_gate(question: str) -> dict:
    """对一个问题跑一次岔路，返回图最终状态：{"question","verdict","route"}。"""
    return build_graph().invoke({"question": question, "verdict": None, "route": None})
