"""Pipeline 阶段 1 的离线兼容性测试，不需要 API key 或向量模型。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from rag.pipeline import Pipeline  # noqa: E402
from rag.llm_client import LLMClient  # noqa: E402


class FakeLLM:
    def __init__(self) -> None:
        self.rewrite_prompts: list[str] = []
        self.answer_prompts: list[tuple[str, str, bool]] = []

    def rewrite_query(self, prompt: str, system_prompt: str) -> list[str]:
        self.rewrite_prompts.append(prompt)
        return ["Wrong", "Side", "Tracks"]

    def stream_answer(self, prompt: str, system_prompt: str,
                      voice: bool = False):
        self.answer_prompts.append((prompt, system_prompt, voice))
        yield "先跟上火车。"
        yield "别撞车。"


class FakeRetriever:
    def __init__(self, hits: list[dict]) -> None:
        self.hits = hits
        self.calls: list[tuple[str, int, list[str]]] = []

    def search(self, question: str, top_k: int = 5,
               extra_terms: list[str] | None = None) -> list[dict]:
        self.calls.append((question, top_k, extra_terms or []))
        return self.hits


class FakeCompletions:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            return iter([
                SimpleNamespace(choices=[SimpleNamespace(
                    delta=SimpleNamespace(content="第一段"))]),
                SimpleNamespace(choices=[SimpleNamespace(
                    delta=SimpleNamespace(content=None))]),
                SimpleNamespace(choices=[SimpleNamespace(
                    delta=SimpleNamespace(content="第二段"))]),
            ])
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="Wrong, Side\nTracks"))])


class FakeOpenAI:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=FakeCompletions())


class PipelineTests(unittest.TestCase):
    def test_llm_client_adapts_completion_shapes(self) -> None:
        api = FakeOpenAI()
        client = LLMClient(client=api, model="test-model")

        self.assertEqual(client.rewrite_query("问题", "改写"),
                         ["Wrong", "Side", "Tracks"])
        self.assertEqual(list(client.stream_answer("回答", "系统", voice=True)),
                         ["第一段", "第二段"])
        self.assertEqual(api.chat.completions.calls[0]["model"], "test-model")
        self.assertEqual(api.chat.completions.calls[1]["max_tokens"], 200)

    def test_answer_uses_separated_rewrite_and_generation(self) -> None:
        llm = FakeLLM()
        retriever = FakeRetriever([{
            "title": "Wrong Side of the Tracks",
            "section": "Walkthrough",
            "text": "Stay close to the train and avoid obstacles.",
        }])
        pipeline = Pipeline(top_k=3, retriever=retriever, llm_client=llm)

        result = "".join(pipeline.answer("火车那关怎么办", {"health": 80}))

        self.assertEqual(result, "先跟上火车。别撞车。")
        self.assertEqual(retriever.calls[0][1:],
                         (3, ["Wrong", "Side", "Tracks"]))
        self.assertIn("玩家当前游戏内状态", llm.answer_prompts[0][0])
        self.assertFalse(llm.answer_prompts[0][2])

    def test_no_hits_does_not_call_generation(self) -> None:
        llm = FakeLLM()
        pipeline = Pipeline(retriever=FakeRetriever([]), llm_client=llm)

        self.assertEqual(list(pipeline.answer("不存在的内容")),
                         ["资料库里没找到相关内容，换个说法试试？"])
        self.assertEqual(llm.answer_prompts, [])


if __name__ == "__main__":
    unittest.main()
