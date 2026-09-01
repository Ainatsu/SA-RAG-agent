"""LangGraph 问答图的显式状态。"""
from __future__ import annotations

from typing import TypedDict


class GraphState(TypedDict, total=False):
    question: str
    player_state: dict | None
    pickups: list[dict]
    voice_mode: bool
    rewritten_terms: list[str]
    retrieved_chunks: list[dict]
    pickup_intent: tuple[str, str] | None
    pickup_context: str | None
    waypoint: str | None
    task_name: str | None
    task_status: str | None
    task_context: str | None
    task_decision: str | None
    prompt: str
    answer_chunks: list[str]
    error: str | None
