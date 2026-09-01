"""阶段 5任务辅助节点的离线测试。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "service"))
sys.path.insert(0, str(HERE))

from agent_runtime import AgentRuntime  # noqa: E402
from test_pipeline import FakeLLM, FakeRetriever  # noqa: E402


class TaskAssistTests(unittest.TestCase):
    def test_known_current_mission_is_added_to_prompt(self) -> None:
        llm = FakeLLM()
        retriever = FakeRetriever([{
            "title": "Wrong Side of the Tracks",
            "section": "Walkthrough",
            "text": "Stay close to the train.",
        }])
        pipeline = type("PipelineStub", (), {
            "retriever": retriever, "llm": llm, "top_k": 5, "rewrite": False,
        })()

        list(AgentRuntime(pipeline).stream(
            "当前任务怎么过", {"on_mission": True,
                               "mission_script": "smoke3"}))

        prompt = llm.answer_prompts[0][0]
        self.assertIn("当前任务：Wrong Side of the Tracks", prompt)
        self.assertIn("当前任务资料", prompt)
        self.assertGreaterEqual(len(retriever.calls), 2)

    def test_unknown_mission_is_not_guessed(self) -> None:
        llm = FakeLLM()
        retriever = FakeRetriever([{
            "title": "General",
            "section": "Notes",
            "text": "General gameplay notes.",
        }])
        pipeline = type("PipelineStub", (), {
            "retriever": retriever, "llm": llm, "top_k": 5, "rewrite": False,
        })()

        list(AgentRuntime(pipeline).stream(
            "当前任务下一步", {"on_mission": True,
                               "mission_script": "unknown_script"}))

        self.assertNotIn("当前任务：", llm.answer_prompts[0][0])


if __name__ == "__main__":
    unittest.main()
