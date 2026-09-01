"""LangGraph 问答图及其唯一的流式适配入口。"""
from __future__ import annotations

from collections.abc import Iterator

from langgraph.graph import END, START, StateGraph

from agent_nodes import make_nodes
from agent_state import GraphState


class AgentRuntime:
    def __init__(self, pipeline) -> None:
        nodes = make_nodes(pipeline.retriever, pipeline.llm,
                           top_k=pipeline.top_k, rewrite=pipeline.rewrite)
        builder = StateGraph(GraphState)
        for name, node in nodes.items():
            builder.add_node(name, node)
        builder.add_edge(START, "rewrite_query")
        builder.add_edge("rewrite_query", "retrieve")
        builder.add_edge("retrieve", "pickup_context")
        builder.add_edge("pickup_context", "task_assist")
        builder.add_edge("task_assist", "build_prompt")
        builder.add_edge("build_prompt", "generate")
        builder.add_edge("generate", END)
        self.graph = builder.compile()

    def stream(self, question: str, player_state: dict | None = None,
               voice: bool = False, pickups: list[dict] | None = None) -> Iterator[object]:
        """执行图并只向调用方暴露文本片段。"""
        initial: GraphState = {
            "question": question,
            "player_state": dict(player_state) if player_state else None,
            "pickups": [dict(item) for item in (pickups or [])],
            "voice_mode": voice,
        }
        for chunk in self.graph.stream(initial, stream_mode="custom"):
            if isinstance(chunk, (str, dict)) and chunk:
                yield chunk
