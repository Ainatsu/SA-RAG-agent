"""LangGraph 文字问答图的离线测试。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "service"))
sys.path.insert(0, str(HERE))

from agent_runtime import AgentRuntime  # noqa: E402
from test_pipeline import FakeLLM, FakeRetriever  # noqa: E402


class RuntimeTests(unittest.TestCase):
    def test_graph_streams_answer_chunks(self) -> None:
        llm = FakeLLM()
        retriever = FakeRetriever([{
            "title": "Gym",
            "section": "Stats",
            "text": "Train at the gym to increase Muscle.",
        }])
        pipeline = type("PipelineStub", (), {
            "retriever": retriever,
            "llm": llm,
            "top_k": 5,
            "rewrite": True,
        })()

        result = list(AgentRuntime(pipeline).stream("怎么提高肌肉", {"health": 90}))

        self.assertEqual(result, ["先跟上火车。", "别撞车。"])
        self.assertEqual(retriever.calls[0][1:],
                         (5, ["Wrong", "Side", "Tracks"]))

    def test_graph_keeps_no_hit_fallback_in_stream(self) -> None:
        llm = FakeLLM()
        pipeline = type("PipelineStub", (), {
            "retriever": FakeRetriever([]),
            "llm": llm,
            "top_k": 5,
            "rewrite": True,
        })()

        self.assertEqual(list(AgentRuntime(pipeline).stream("没有这条资料")),
                         ["资料库里没找到相关内容，换个说法试试？"])
        self.assertEqual(llm.answer_prompts, [])

    def test_graph_emits_waypoint_event_from_pickup_snapshot(self) -> None:
        llm = FakeLLM()
        pipeline = type("PipelineStub", (), {
            "retriever": FakeRetriever([{
                "title": "Weapons",
                "section": "Locations",
                "text": "Weapons can be collected.",
            }]),
            "llm": llm,
            "top_k": 5,
            "rewrite": True,
        })()
        pickups = [{"kind": "armor", "name": "",
                    "x": 12.0, "y": 24.0, "z": 3.0, "zone": "Grove Street"}]

        result = list(AgentRuntime(pipeline).stream(
            "最近的防弹衣在哪", {"x": 10.0, "y": 20.0}, pickups=pickups))

        self.assertEqual(result[0], {"type": "waypoint", "payload": "12.0,24.0,3.0"})
        self.assertEqual(result[1:], ["先跟上火车。", "别撞车。"])
        self.assertIn("实时拾取物线索", llm.answer_prompts[0][0])


if __name__ == "__main__":
    unittest.main()
