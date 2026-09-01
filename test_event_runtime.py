"""事件运行时调度器的离线测试。"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "service"))

from event_runtime import EventRuntime  # noqa: E402


class EventRuntimeTests(unittest.TestCase):
    def test_tracks_state_and_respects_cooldown(self) -> None:
        announced: list[tuple[str, str]] = []

        async def announce(kind: str, text: str) -> None:
            announced.append((kind, text))

        runtime = EventRuntime(
            lambda prev, curr, now: [("health", "撤退")], announce,
            {"health": 60}, lambda: False,
        )
        asyncio.run(runtime.process({"health": 100}))
        asyncio.run(runtime.process({"health": 50}))
        asyncio.run(runtime.process({"health": 40}))
        self.assertEqual(announced, [("health", "撤退")])
        self.assertEqual(runtime.previous, {"health": 40})

    def test_busy_does_not_announce(self) -> None:
        announced: list[str] = []

        async def announce(kind: str, text: str) -> None:
            announced.append(text)

        runtime = EventRuntime(
            lambda prev, curr, now: [("wanted", "别浪")], announce,
            {"wanted": 1}, lambda: True,
        )
        asyncio.run(runtime.process({"wanted": 0}))
        asyncio.run(runtime.process({"wanted": 1}))
        self.assertEqual(announced, [])


if __name__ == "__main__":
    unittest.main()
