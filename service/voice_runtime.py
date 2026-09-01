"""语音运行时：麦克风占用、唤醒同步和边生成边播报。"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import speech
import voice


class Mic:
    """一条连接上的麦克风占用适配器。"""

    def __init__(self, pause: Callable[[], None] | None = None,
                 resume: Callable[[], None] | None = None) -> None:
        self.recorder = voice.Recorder()
        self._held = False
        self._pause = pause or (lambda: None)
        self._resume = resume or (lambda: None)

    def acquire(self) -> None:
        if not self._held:
            self._held = True
            self._pause()

    def release(self) -> None:
        if self._held:
            self._held = False
            self._resume()

    def abort(self) -> None:
        self.recorder.abort()
        self.release()


class VoiceRuntime:
    """不直接接触协议的语音流程适配器。"""

    @staticmethod
    async def sync_wake(game_active: bool, listener) -> None:
        if listener is None:
            return
        if game_active and not listener.active:
            await asyncio.to_thread(listener.start)
        elif not game_active and listener.active:
            await asyncio.to_thread(listener.stop)

    @staticmethod
    async def speak_reply(
        question: str,
        state: dict | None,
        status: Callable[[str], Awaitable[None]],
        generate_reply: Callable[..., object],
        token_getter: Callable[[], int],
        token: int,
        pickups: list[dict] | None = None,
        waypoint: Callable[[str], Awaitable[None]] | None = None,
    ) -> tuple[str, bool]:
        """消费文本生成器并按句推送给 SAPI，返回回答和是否正常结束。"""
        segmenter = speech.Segmenter()
        parts: list[str] = []
        speaking = False
        speech.begin()

        async def emit(text: str) -> None:
            nonlocal speaking
            if not speaking:
                speaking = True
                await status("speaking")
            speech.push(text)

        async for chunk in generate_reply(question, state, voice=True,
                                          pickups=pickups, waypoint=waypoint):
            if token != token_getter():
                return "".join(parts), False
            parts.append(chunk)
            for sentence in segmenter.feed(chunk):
                await emit(sentence)

        tail = segmenter.flush()
        if tail:
            await emit(tail)
        if token != token_getter():
            return "".join(parts), False
        await asyncio.to_thread(speech.finish)
        return "".join(parts), True
