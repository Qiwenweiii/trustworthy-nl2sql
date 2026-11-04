"""拒答节点：岔路的另一端，只把"走了拒答"这件事写回状态。

拒答文案由判定模块生成（缺口在检查时就已经算清），这里不重复拼文本，也不生成 SQL。
"""

from __future__ import annotations

from data_agent.graph.state import SemanticGateState


def semantic_refuse_node(state: SemanticGateState) -> dict:
    return {"route": "refuse"}
