"""事件检测与告警调度运行时。"""
from __future__ import annotations

import time
from collections.abc import Awaitable, Callable


class EventRuntime:
    def __init__(self, detector: Callable, announce: Callable[[str, str], Awaitable[None]],
                 cooldown: dict[str, float], busy: Callable[[], bool]) -> None:
        self.detector = detector
        self.announce = announce
        self.cooldown = cooldown
        self.busy = busy
        self.previous: dict | None = None
        self.last_alert_time: dict[str, float] = {}

    async def process(self, state: dict) -> None:
        if self.previous is None:
            self.previous = dict(state)
            return
        now = time.monotonic()
        events = self.detector(self.previous, state, now)
        self.previous = dict(state)
        if self.busy():
            return
        for kind, text in events:
            if now - self.last_alert_time.get(kind, 0.0) < self.cooldown.get(kind, 10.0):
                continue
            self.last_alert_time[kind] = now
            await self.announce(kind, text)
            break

    def reset(self) -> None:
        self.previous = None
        self.last_alert_time.clear()
