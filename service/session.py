"""游戏会话快照管理。"""
from __future__ import annotations

import json


class SessionManager:
    """保存可供一次问答使用的状态和拾取物快照。"""

    def __init__(self) -> None:
        self.latest_state: dict | None = None
        self.latest_pickups: list[dict] = []

    def update_state_payload(self, payload: str) -> dict | None:
        try:
            state = json.loads(payload) or None
        except json.JSONDecodeError:
            return None
        if not isinstance(state, dict):
            return None
        self.latest_state = dict(state)
        return dict(state)

    def update_pickups_payload(self, payload: str) -> list[dict] | None:
        try:
            items = json.loads(payload)
        except json.JSONDecodeError:
            return None
        if not isinstance(items, list):
            return None
        self.latest_pickups = [dict(item) for item in items if isinstance(item, dict)]
        return self.pickups_snapshot()

    def state_snapshot(self) -> dict | None:
        return dict(self.latest_state) if self.latest_state is not None else None

    def pickups_snapshot(self) -> list[dict]:
        return [dict(item) for item in self.latest_pickups]

    def clear(self) -> None:
        self.latest_state = None
        self.latest_pickups.clear()
