"""语音输出：把 agent 的回答念出来。

实时语音问答里游戏不暂停，玩家一边开车一边听，所以播报必须能随时打断——
玩家问下一个问题时，上一段没念完的要立刻停掉，否则两段话会叠在一起。

回答是流式生成的，等整段生成完再念要让玩家干等好几秒。所以有两套接口：
    say / say_sync          文本已经齐了，一次念完
    begin / push / finish   边生成边念，配合 Segmenter 按句切开
后者是实时语音问答的主路径，第一句一出来就开始念，后面的接着排进去，
听起来是连续的一段话。

后端通过 SA_TTS_BACKEND 选择，统一走 Speaker 接口：
    sapi    Windows 自带语音合成（默认，零依赖、无延迟）
换成神经 TTS 时新增一个实现即可，调用方不感知。

环境变量：
    SA_TTS_BACKEND  合成后端，默认 sapi
    SA_TTS_VOICE    音色名关键词，默认挑第一个中文音色
    SA_TTS_RATE     语速，-10..10，默认 1（比默认稍快，听着不拖沓）
"""
from __future__ import annotations

import logging
import os
import queue
import re
import threading
from typing import Iterable, Protocol

log = logging.getLogger("speech")

# 提示词已经要求纯口语，但模型偶尔还是会漏出 **加粗**、`代码`、1. 序号。
# SAPI 会把这些符号照着念（"星星"、"反引号"），所以进合成前清一遍。
_MD_MARKS = re.compile(r"[*_`#>|~]+")
_LIST_HEAD = re.compile(r"^[ \t]*(?:[-+*·•]|\d+[.、)])[ \t]+", re.MULTILINE)
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_SPACES = re.compile(r"\s+")


def clean_for_speech(text: str) -> str:
    """把回答整理成适合朗读的纯文本。

    换行一律并成空格：SAPI 遇到换行会停顿得很生硬，听着像卡了。
    """
    text = _LINK.sub(r"\1", text)
    text = _LIST_HEAD.sub("", text)
    text = _MD_MARKS.sub("", text)
    return _SPACES.sub(" ", text).strip()


# ── 分句 ────────────────────────────────────────────────────────────
# 切得太碎，每段之间会有合成器的起停停顿，听着一顿一顿；切得太粗，第一句
# 迟迟排不进去，等待就白省了。所以按标点切，并给一个长度上限兜住长句。

# 句子结束，连标点一起念（标点本身影响语调，不能丢）
_STRONG_END = "。！？!?；;\n"
# 长句里退而求其次的切点
_WEAK_END = "，,、：:"
# 超过这么多字还没遇到强标点，就在最近的弱标点处切开
_SOFT_LIMIT = 36
# 比这还短的片段不单独送去合成："好。"念出来只是一声闷响
_MIN_SEGMENT = 4


class Segmenter:
    """把流式文本攒成适合逐句朗读的片段。

    feed() 每次返回本次能确定念出来的完整片段（可能是 0 段或多段），
    剩下不成句的留在缓冲里；流结束时用 flush() 把尾巴取走。
    """

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, text: str) -> list[str]:
        self._buf += text
        out: list[str] = []
        while True:
            seg = self._take()
            if seg is None:
                break
            if seg:
                out.append(seg)
        return out

    def flush(self) -> str:
        """取走缓冲里剩下的内容。不足一句也要念，那是回答的最后一截。"""
        seg, self._buf = self._buf, ""
        return seg.strip()

    def _take(self) -> str | None:
        """切下一个可念的片段。切不出来时返回 None。"""
        for i, c in enumerate(self._buf):
            if c in _STRONG_END and i + 1 >= _MIN_SEGMENT:
                seg, self._buf = self._buf[:i + 1], self._buf[i + 1:]
                return seg.strip()

        if len(self._buf) < _SOFT_LIMIT:
            return None

        # 一句话长得没完没了（模型偶尔会写成一长串逗号），在上限内最靠后的
        # 弱标点处切开。找不到弱标点就继续等，硬按字数切会切断词。
        cut = max(self._buf.rfind(c, 0, _SOFT_LIMIT) for c in _WEAK_END)
        if cut + 1 < _MIN_SEGMENT:
            return None
        seg, self._buf = self._buf[:cut + 1], self._buf[cut + 1:]
        return seg.strip()


class Speaker(Protocol):
    def say(self, text: str) -> None:
        """开始朗读。立即返回，不等念完。"""
        ...

    def say_sync(self, text: str) -> None:
        """朗读并等念完。唤醒模式靠它决定何时恢复麦克风监听。"""
        ...

    def begin(self) -> None:
        """开始一轮流式朗读，清掉上一轮没念完的。立即返回。"""
        ...

    def push(self, text: str) -> None:
        """往本轮末尾追加一段要念的文本。立即返回，接着上一段念。"""
        ...

    def finish(self) -> None:
        """等本轮推进去的都念完。被 stop() 打断时提前返回。"""
        ...

    def stop(self) -> None:
        """立刻掐掉正在念的内容。没在念时是空操作。"""
        ...


class SapiSpeaker:
    """Windows SAPI。通过 COM 调用，无需下载模型。

    SAPI 的 COM 对象有线程亲和性：在创建它的 STA 里才能安全调用。播报请求
    来自 asyncio 事件循环线程，而初始化可能发生在预热线程，所以这里固定用
    一个自有线程持有对象，外部只往队列投递指令。

    流式朗读靠 SAPI 自己的播放队列实现：push 下发的 Speak 不带 PURGE，
    一段接一段排进去，段间没有空隙；finish 排在所有 push 之后，取到它时
    该轮的 Speak 都已下发，等 WaitUntilDone 即可。
    """

    # SpVoice.Speak 的标志位
    _ASYNC = 1          # 不阻塞，交给 SAPI 自己的播放线程
    _PURGE = 2          # 清掉队列里没念完的，实现打断

    # 等待时的轮询粒度（毫秒）。打断的响应延迟就是这个数量级
    _POLL_MS = 80

    def __init__(self) -> None:
        self._cmds: queue.Queue[tuple[str, str, threading.Event | None] | None] = queue.Queue()
        # stop() 来自别的线程，而 say_sync 正卡在自己的线程里等念完。
        # 光靠队列递不进去（stop 排在 say_sync 后面），得有个旁路的标志。
        self._interrupt = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="sapi-speaker")
        self._thread.start()

    def _run(self) -> None:
        voice = None
        try:
            import pythoncom
            import win32com.client

            pythoncom.CoInitialize()
            voice = win32com.client.Dispatch("SAPI.SpVoice")

            want = os.environ.get("SA_TTS_VOICE", "").strip()
            for token in voice.GetVoices():
                desc = token.GetDescription()
                # 没指定音色时挑中文的：英文音色念中文会逐字母拼读
                hit = (want.lower() in desc.lower() if want
                       else ("Chinese" in desc or "中文" in desc))
                if hit:
                    voice.Voice = token
                    log.info("TTS 音色: %s", desc)
                    break

            try:
                voice.Rate = int(os.environ.get("SA_TTS_RATE", "1"))
            except ValueError:
                pass
        except Exception as e:
            log.warning("TTS 初始化失败，语音播报已禁用: %s", e)
            # 继续排空队列，否则调用方会在 put 上堆积
            voice = None

        while True:
            cmd = self._cmds.get()
            if cmd is None:
                break
            op, text, done = cmd
            if voice is None:
                if done is not None:
                    done.set()
                continue
            try:
                if op == "say":
                    self._interrupt.clear()
                    voice.Speak(text, self._ASYNC | self._PURGE)
                elif op == "say_sync":
                    self._interrupt.clear()
                    voice.Speak(text, self._ASYNC | self._PURGE)
                    self._wait_done(voice)
                elif op == "begin":
                    # 上一轮没念完的到此为止，本轮从干净的队列开始
                    voice.Speak("", self._ASYNC | self._PURGE)
                    self._interrupt.clear()
                elif op == "push":
                    # 不带 PURGE，排在本轮已下发的内容后面
                    voice.Speak(text, self._ASYNC)
                elif op == "finish":
                    self._wait_done(voice)
                else:
                    # 念一段空文本并带 PURGE，是 SAPI 里清空队列的惯用做法
                    voice.Speak("", self._ASYNC | self._PURGE)
                    self._interrupt.clear()
            except Exception as e:
                log.warning("TTS %s 失败: %s", op, e)
            finally:
                if done is not None:
                    done.set()

    def _wait_done(self, voice) -> None:
        """等 SAPI 把已下发的内容念完，但中途可被 stop() 打断。

        不能直接用同步 Speak：那样这个线程会一直卡到念完，玩家想插话
        （按侧键重新提问）就得干等一整段。改成异步下发 + 分段等待，
        每一轮都看一眼打断标志。
        """
        while not voice.WaitUntilDone(self._POLL_MS):
            if self._interrupt.is_set():
                voice.Speak("", self._ASYNC | self._PURGE)
                log.info("朗读被打断")
                return

    def say(self, text: str) -> None:
        text = clean_for_speech(text)
        if text:
            self._cmds.put(("say", text, None))

    def say_sync(self, text: str) -> None:
        """念完再返回。调用方通常是线程池，别在事件循环里直接调。"""
        text = clean_for_speech(text)
        if not text:
            return
        done = threading.Event()
        self._cmds.put(("say_sync", text, done))
        done.wait()

    def begin(self) -> None:
        # 先立打断标志：上一轮可能正卡在 finish 里等念完，光排队进不去
        self._interrupt.set()
        self._cmds.put(("begin", "", None))

    def push(self, text: str) -> None:
        text = clean_for_speech(text)
        if text:
            self._cmds.put(("push", text, None))

    def finish(self) -> None:
        """阻塞直到本轮念完。调用方通常是线程池，别在事件循环里直接调。"""
        done = threading.Event()
        self._cmds.put(("finish", "", done))
        done.wait()

    def stop(self) -> None:
        # 先立标志：正在 _wait_done 里等的那一轮靠它退出，
        # 队列里的 stop 只负责事后把标志清掉
        self._interrupt.set()
        self._cmds.put(("stop", "", None))


_speaker: Speaker | None = None
_speaker_lock = threading.Lock()


def get_speaker() -> Speaker:
    global _speaker
    with _speaker_lock:
        if _speaker is None:
            backend = os.environ.get("SA_TTS_BACKEND", "sapi").strip().lower()
            if backend != "sapi":
                raise RuntimeError(f"未知的 TTS 后端: {backend}")
            _speaker = SapiSpeaker()
        return _speaker


def say(text: str) -> None:
    get_speaker().say(text)


def say_sync(text: str) -> None:
    get_speaker().say_sync(text)


def begin() -> None:
    get_speaker().begin()


def push(text: str) -> None:
    get_speaker().push(text)


def finish() -> None:
    get_speaker().finish()


def stop() -> None:
    get_speaker().stop()


def prewarm() -> None:
    """提前把合成后端建起来。

    SAPI 的 COM 初始化和音色枚举要几百毫秒，摊在第一次回答里就是听得出的
    延迟。失败不影响服务启动，真到要念的时候还会再试一次。
    """

    def worker() -> None:
        try:
            get_speaker()
        except Exception as e:
            log.warning("语音合成预热失败（首次使用时会重试）: %s", e)

    threading.Thread(target=worker, daemon=True).start()


def say_stream(chunks: Iterable[str]) -> str:
    """边收边念，返回念过的完整文本。阻塞直到念完或被打断。

    给同步调用方（自测脚本）用；服务端在事件循环里跑，用的是
    begin / push / finish 三件套，见 server.speak_reply。
    """
    speaker = get_speaker()
    speaker.begin()
    seg = Segmenter()
    parts: list[str] = []
    for chunk in chunks:
        parts.append(chunk)
        for s in seg.feed(chunk):
            speaker.push(s)
    tail = seg.flush()
    if tail:
        speaker.push(tail)
    speaker.finish()
    return "".join(parts)
