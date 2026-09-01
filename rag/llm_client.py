"""DeepSeek 的低层 LLM 适配。

该模块只负责 OpenAI-compatible API 调用，不负责检索、玩家状态或传输层。
通过注入 ``client`` 可以在离线测试中替换真实的 OpenAI client。
"""
from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterator

from openai import OpenAI

log = logging.getLogger("llm_client")

MODEL = "deepseek-chat"


class LLMClient:
    """DeepSeek 请求的最小适配层。"""

    def __init__(self, client: OpenAI | None = None,
                 model: str = MODEL) -> None:
        self.client = client or self._build_client()
        self.model = model

    @staticmethod
    def _build_client() -> OpenAI:
        key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if not key:
            raise RuntimeError(
                "未设置 DEEPSEEK_API_KEY。请在 sa-agent/.env 中配置。"
            )
        return OpenAI(api_key=key, base_url="https://api.deepseek.com",
                      timeout=60)

    def rewrite_query(self, prompt: str, system_prompt: str) -> list[str]:
        """请求查询改写，并将模型输出解析为关键词列表。"""
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            max_tokens=60,
            temperature=0,
        )
        text = (response.choices[0].message.content or "").strip()
        terms = [term for term in re.split(r"[\s,，、]+", text) if term]
        return terms[:12]

    def stream_answer(self, prompt: str, system_prompt: str,
                      voice: bool = False) -> Iterator[str]:
        """请求回答并产出非空文本片段，不触碰 socket 或 TTS。"""
        stream = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            stream=True,
            temperature=0.3,
            max_tokens=200 if voice else 600,
        )
        for chunk in stream:
            piece = chunk.choices[0].delta.content
            if piece:
                yield piece
