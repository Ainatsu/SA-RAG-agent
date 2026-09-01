"""问答图节点。节点只处理状态，不持有 socket、音频对象或线程锁。"""
from __future__ import annotations

import logging
from collections.abc import Callable

from langgraph.config import get_stream_writer

from agent_state import GraphState
from agent_tools import build_pickup_context, parse_nearest_intent
from rag.pipeline import (ANSWER_SYS, REWRITE_SYS, VOICE_ANSWER_SYS,
                          describe_state)
from rag.missions import mission_name

log = logging.getLogger("agent_nodes")
NO_HITS = "资料库里没找到相关内容，换个说法试试？"
TASK_HINTS = ("这关", "当前任务", "任务", "怎么过", "下一步", "目标")


def make_nodes(retriever, llm, top_k: int = 5,
               rewrite: bool = True) -> dict[str, Callable[[GraphState], dict]]:
    """用现有低层适配器构造节点，方便测试时替换检索器和 LLM。"""

    def rewrite_query_node(state: GraphState) -> dict:
        if not rewrite:
            return {"rewritten_terms": []}
        question = state["question"]
        prompt = f"问题：{question}\n"
        desc = describe_state(state.get("player_state"))
        if desc:
            prompt += f"玩家当前状态：{desc}\n"
        prompt += "输出："
        try:
            terms = llm.rewrite_query(prompt, REWRITE_SYS)
        except Exception as exc:
            log.warning("查询改写失败（退回纯 BM25）: %s", exc)
            terms = []
        return {"rewritten_terms": terms}

    def retrieve_node(state: GraphState) -> dict:
        hits = retriever.search(
            state["question"], top_k=top_k,
            extra_terms=state.get("rewritten_terms", []),
        )
        log.info("图检索命中 %d 块", len(hits))
        return {"retrieved_chunks": hits}

    def build_prompt_node(state: GraphState) -> dict:
        hits = state.get("retrieved_chunks", [])
        task_context = state.get("task_context")
        if not hits and not task_context:
            return {"prompt": "", "answer_chunks": [NO_HITS]}

        parts = [
            f"【资料{i}】{hit['title']} — {hit['section']}\n{hit['text']}"
            for i, hit in enumerate(hits, 1)
        ]
        user = f"玩家问题：{state['question']}\n\n"
        desc = describe_state(state.get("player_state"))
        if desc:
            user += f"玩家当前游戏内状态：{desc}\n\n"
        if state.get("task_name"):
            user += f"当前任务：{state['task_name']}\n"
            if state.get("task_status"):
                user += f"当前任务状态：{state['task_status']}\n"
            if task_context:
                user += f"当前任务资料：\n{task_context}\n\n"
        user += "以下是从 GTA Wiki 检索到的资料：\n\n"
        if state.get("pickup_context"):
            user += f"游戏内实时拾取物线索：{state['pickup_context']}\n\n"
        user += "\n\n".join(parts) + "\n\n请依据资料回答玩家问题。"
        if desc:
            user += "若状态与问题相关（如血量偏低、缺少弹药、属性不足），可在回答中一并提醒。"
        return {"prompt": user}

    def task_assist_node(state: GraphState) -> dict:
        player_state = state.get("player_state") or {}
        question = state["question"]
        if not player_state.get("on_mission") or not any(
                hint in question for hint in TASK_HINTS):
            return {"task_decision": "answer"}

        name = mission_name(player_state.get("mission_script"))
        if not name:
            return {"task_decision": "answer"}

        task_hits = retriever.search(name, top_k=3, extra_terms=[name])
        context = "\n\n".join(
            f"【任务资料{i}】{hit['title']} — {hit['section']}\n{hit['text']}"
            for i, hit in enumerate(task_hits, 1)
        )
        status = "任务进行中"
        if player_state.get("health", 100) <= 25:
            status += "；血量偏低，建议先找掩体或补给"
        return {"task_name": name, "task_status": status,
                "task_context": context or None,
                "task_decision": "answer" if context else "wait"}

    def pickup_context_node(state: GraphState) -> dict:
        intent = parse_nearest_intent(state["question"], state.get("pickups", []))
        result = build_pickup_context(intent, state.get("player_state"),
                                      state.get("pickups", []))
        if result is None:
            return {"pickup_intent": intent}
        clue, waypoint = result
        return {"pickup_intent": intent, "pickup_context": clue,
                "waypoint": waypoint}

    def generate_node(state: GraphState) -> dict:
        existing = state.get("answer_chunks", [])
        if existing:
            writer = get_stream_writer()
            for chunk in existing:
                writer(chunk)
            return {}

        system = VOICE_ANSWER_SYS if state.get("voice_mode", False) else ANSWER_SYS
        chunks: list[str] = []
        writer = get_stream_writer()
        if state.get("waypoint"):
            writer({"type": "waypoint", "payload": state["waypoint"]})
        for chunk in llm.stream_answer(state["prompt"], system,
                                       voice=state.get("voice_mode", False)):
            chunks.append(chunk)
            writer(chunk)
        return {"answer_chunks": chunks}

    return {
        "rewrite_query": rewrite_query_node,
        "retrieve": retrieve_node,
        "pickup_context": pickup_context_node,
        "task_assist": task_assist_node,
        "build_prompt": build_prompt_node,
        "generate": generate_node,
    }
