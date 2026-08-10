"""语音输入：麦克风录音 + 语音识别。

覆盖层里按住左 Ctrl 说话，松开后把识别文本回填到输入框。录音采集放在
Python 侧（C++ 只发控制帧），这样音频管线整体可替换——之后接实时语音
agent 时，只需把 Recorder 的分片直接推给流式接口，协议和 C++ 侧不用动。

识别后端通过 SA_ASR_BACKEND 选择，统一走 Transcriber 接口：
    local   本地 faster-whisper（默认，离线可用）
换成云端流式 ASR 时新增一个实现即可，调用方不感知。

环境变量：
    SA_ASR_BACKEND    识别后端，默认 local
    SA_WHISPER_MODEL  本地模型规格，默认 small
    SA_WHISPER_DEVICE 推理设备，默认 cpu
    SA_WAKE_MODEL     唤醒扫描用的模型，默认 tiny（见 WakeScanner）
    SA_WAKE_LOG       打印每次唤醒扫描听到了什么，默认 1
"""
from __future__ import annotations

import logging
import os
import queue
import threading
from collections import deque
from typing import Callable, Protocol

import numpy as np
import sounddevice as sd

log = logging.getLogger("voice")

# Whisper 系模型固定吃 16 kHz 单声道，直接按这个采样率采集，省一次重采样
SAMPLE_RATE = 16000
CHANNELS = 1
BLOCK = 1600  # 100 ms 一块

# 短于此长度的录音按误触处理：按住说话难免手滑点一下
MIN_SECONDS = 0.3
# 录音上限，防止按键卡住导致内存无限增长
MAX_SECONDS = 60.0


class Recorder:
    """按住说话的录音器。

    start() 立即返回，采集在 sounddevice 的回调线程里进行；stop() 返回
    完整录音。分块存放而不是预分配大缓冲，是为了之后能在录音过程中就把
    分块推给流式识别。
    """

    def __init__(self) -> None:
        self._stream: sd.InputStream | None = None
        self._chunks: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._frames = 0

    @property
    def active(self) -> bool:
        return self._stream is not None

    def _callback(self, indata, frames, time_info, status) -> None:
        if status:
            log.debug("音频流状态: %s", status)
        with self._lock:
            if self._frames >= MAX_SECONDS * SAMPLE_RATE:
                return
            self._chunks.append(indata.copy().reshape(-1))
            self._frames += frames

    def start(self) -> None:
        if self._stream is not None:
            return
        with self._lock:
            self._chunks = []
            self._frames = 0
        stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            blocksize=BLOCK,
            callback=self._callback,
        )
        stream.start()
        self._stream = stream
        log.info("开始录音")

    def stop(self) -> np.ndarray:
        """停止录音并返回采集到的音频。未在录音时返回空数组。"""
        stream, self._stream = self._stream, None
        if stream is None:
            return np.empty(0, dtype=np.float32)

        stream.stop()
        stream.close()

        with self._lock:
            chunks, self._chunks = self._chunks, []
            self._frames = 0

        audio = np.concatenate(chunks) if chunks else np.empty(0, dtype=np.float32)
        log.info("录音结束，%.1f 秒", len(audio) / SAMPLE_RATE)
        return audio

    def abort(self) -> None:
        """丢弃当前录音（连接断开时清理用）。"""
        if self._stream is not None:
            self.stop()


class Transcriber(Protocol):
    def transcribe(self, audio: np.ndarray) -> str:
        """把 16 kHz 单声道 float32 音频转成文本。识别不出内容时返回空串。"""
        ...


class LocalWhisper:
    """本地 faster-whisper。模型加载慢，做成懒加载 + 可预热。"""

    def __init__(self) -> None:
        self._model = None
        self._lock = threading.Lock()
        self.name = os.environ.get("SA_WHISPER_MODEL", "small").strip()
        self.device = os.environ.get("SA_WHISPER_DEVICE", "cpu").strip()

    def load(self) -> None:
        with self._lock:
            if self._model is not None:
                return
            from faster_whisper import WhisperModel

            log.info("加载 Whisper 模型 %s（%s，首次运行需要下载）", self.name, self.device)
            compute = "int8" if self.device == "cpu" else "float16"
            self._model = WhisperModel(self.name, device=self.device, compute_type=compute)
            log.info("Whisper 模型就绪")

    def transcribe(self, audio: np.ndarray) -> str:
        self.load()
        assert self._model is not None
        segments, _ = self._model.transcribe(
            audio,
            language="zh",
            beam_size=5,
            vad_filter=True,          # 掐掉前后静音，减少幻听式输出
            # 默认阈值偏严，玩家刚按下键就开口时会把开头整段当静音滤掉，
            # 表现为"识别不出内容"或吞掉第一个字。这里取一组很宽松的值：
            # 宁可多留一点静音让模型自己判断，也不要截掉真实语音。
            vad_parameters={
                "threshold": 0.2,
                "min_silence_duration_ms": 1000,
                "speech_pad_ms": 800,
            },
            condition_on_previous_text=False,  # 每次录音独立，避免上一句污染
            # 玩家说的多是游戏专名，通用模型没见过，容易按发音写成别的字
            #（"疯狗多克"→"封狗多克"）。把常见中文译名塞进解码上下文做锚点。
            initial_prompt=(
                "这是《GTA 圣安地列斯》的游戏对话。可能出现的名字有："
                "CJ、卡尔、大烟、斯莫克、莱德尔、ryder、甜哥、斯维特、坎德尔、凯撒、塞萨尔、疯狗多克、特鲁斯、真理、托雷诺、吴子、基茨、门德斯、汤普尼、"
                "坦佩尼、洛圣都、圣菲耶罗、拉斯云祖华、格罗夫街、巴拉斯、瓦格斯、墨西哥帮、"
                "以及任务、支线、武器、载具、帮派、地盘等词。"
            ),
        )
        return "".join(s.text for s in segments).strip()


_transcriber: Transcriber | None = None


def get_transcriber() -> Transcriber:
    global _transcriber
    if _transcriber is None:
        backend = os.environ.get("SA_ASR_BACKEND", "local").strip().lower()
        if backend != "local":
            raise RuntimeError(f"未知的 ASR 后端: {backend}")
        _transcriber = LocalWhisper()
    return _transcriber


def prewarm(on_ready: Callable[[], None] | None = None) -> None:
    """后台预加载模型，避免第一次说话时干等。失败不影响服务启动。

    on_ready 在模型可用后于加载线程里调用。唤醒监听靠它决定何时开麦：
    模型还在下载就把麦克风打开，只会攒下一堆处理不过来的过期音频。
    """

    def worker() -> None:
        try:
            t = get_transcriber()
            if isinstance(t, LocalWhisper):
                t.load()
        except Exception as e:
            log.warning("语音识别预热失败（首次使用时会重试）: %s", e)
            return
        if on_ready is None:
            return
        # 只有开着唤醒监听才会传 on_ready，扫描模型也只在那时才用得上
        try:
            get_scanner().load()
        except Exception as e:
            log.warning("唤醒扫描模型加载失败，唤醒词不可用: %s", e)
            return
        try:
            on_ready()
        except Exception as e:
            log.warning("语音识别就绪回调失败: %s", e)

    threading.Thread(target=worker, daemon=True).start()


def transcribe(audio: np.ndarray) -> str:
    """识别一段录音。太短的录音直接忽略，不去打扰识别后端。"""
    if len(audio) < MIN_SECONDS * SAMPLE_RATE:
        log.info("录音过短，忽略")
        return ""
    return get_transcriber().transcribe(audio)


# ── 唤醒词监听 ──────────────────────────────────────────────────────
# 麦克风常开，滑动窗口反复识别找唤醒词；命中后转入录制，靠静音判断说完了。
# 用能量做静音检测而不是再跑一次 VAD：这一步每 100 ms 判一次，跑模型太贵。

WAKE_WORD_DISPLAY = "小龟J"

# 唤醒词比对。不做整词匹配：「小龟J」是个生造的名字，模型没见过，识别结果
# 十有八九是同音字（实测有小规矩、小规罪、小規刺、小龟翠）。所以先找两个字
# 的头，再顺手吃掉一个像「J」的尾字——尾字不吃掉会留在问题开头（"矩，这关
# 怎么打"）。头里带上繁体写法，whisper 输出简繁混用。
WAKE_HEADS = ("小龟", "小龜", "小归", "小规", "小規", "小鬼", "小柜", "小贵",
              "小闺", "小乖", "晓龟", "晓规")
WAKE_TAILS = "jJ鸡机基吉记及即几既矩则罪醉刺翠撤窃切"

# 唤醒扫描窗口。太短会把唤醒词切两半，太长会拖慢响应
WAKE_WINDOW_SECONDS = 2.0
# 每隔多久扫一次
WAKE_STRIDE_SECONDS = 0.7
# 说完后静音多久算一句结束
END_SILENCE_SECONDS = 1.2
# 单轮提问的最长时间，防止环境噪音导致永远结束不了
UTTERANCE_MAX_SECONDS = 15.0
# 静音判定阈值（RMS）。麦克风增益差异大，必要时用 SA_VAD_RMS 调
SILENCE_RMS = float(os.environ.get("SA_VAD_RMS", "0.012"))


def _normalize(text: str) -> str:
    """去掉标点和空格再比对，识别结果里常夹杂逗号句号。"""
    return "".join(c for c in text.lower() if c.isalnum())


def find_wake_word(text: str) -> int:
    """在识别文本里找唤醒词，返回唤醒词结束的位置；没有则返回 -1。

    位置按去标点后的下标计，配合 strip_wake_word 换算回原文。
    """
    flat = _normalize(text)
    tails = _normalize(WAKE_TAILS)
    for head in WAKE_HEADS:
        pos = flat.find(_normalize(head))
        if pos < 0:
            continue
        end = pos + len(_normalize(head))
        if end < len(flat) and flat[end] in tails:
            end += 1
        return end
    return -1


# 识别结果两端常见的标点，剥完唤醒词后一并清掉
_EDGE_PUNCT = "，。,. 、？?！!；;：:~—-"


def strip_wake_word(text: str) -> str:
    """去掉提问开头的唤醒词。

    唤醒词往往和问题连着说（"小龟J这关怎么打"），识别出来是一句。
    find_wake_word 给的下标是在去标点文本上的，这里边扫原文边数可比对的
    字符，数到位就把前面连标点一起切掉。
    """
    cut = find_wake_word(text)
    if cut < 0:
        return text.strip(_EDGE_PUNCT)

    flat_pos = 0
    for i, c in enumerate(text):
        if c.isalnum():
            flat_pos += 1
        if flat_pos >= cut:
            text = text[i + 1:]
            break
    return text.strip(_EDGE_PUNCT)


class WakeScanner:
    """唤醒扫描专用的识别器。和提问识别刻意分开，两处的要求正好相反。

    提示词：提问那边拿一长串游戏专名当解码锚点，扫描绝不能用它——实测
    「小龟J」会被那串专名带偏成「小规矩」，唤醒词永远匹配不上。这里换成
    只放唤醒词本身，同一段音频就能稳定念成「小龟J」。

    模型与参数：提问一轮只识别一次，慢一点无所谓；扫描是每 0.7 秒一次的
    常态开销，必须比窗口短，否则积压会越拖越远（small + beam 5 在这台机器上
    要 2.5~3 秒识别一个 2 秒窗口，等于永远在处理几十秒前的声音）。所以扫描
    用 tiny + beam 1 + 不跑 VAD，约 0.3 秒。tiny 认字差，但这一步只要认出
    唤醒词的前两个字，剩下的问题正文仍由 small 重新识别。

    要是自己的声音扫不出来，用 SA_WAKE_MODEL=small 换回大模型，代价是响应变慢。
    """

    def __init__(self) -> None:
        self._model = None
        self._lock = threading.Lock()
        self.name = os.environ.get("SA_WAKE_MODEL", "tiny").strip()
        self.device = os.environ.get("SA_WHISPER_DEVICE", "cpu").strip()

    def load(self) -> None:
        with self._lock:
            if self._model is not None:
                return
            from faster_whisper import WhisperModel

            log.info("加载唤醒扫描模型 %s（%s）", self.name, self.device)
            compute = "int8" if self.device == "cpu" else "float16"
            # 扫描常态占着 CPU，线程数放开会和游戏抢核。留两个够用了。
            self._model = WhisperModel(self.name, device=self.device,
                                       compute_type=compute, cpu_threads=2)
            log.info("唤醒扫描模型就绪")

    def scan(self, audio: np.ndarray) -> str:
        self.load()
        assert self._model is not None
        segments, _ = self._model.transcribe(
            audio,
            language="zh",
            beam_size=1,
            vad_filter=False,
            without_timestamps=True,
            condition_on_previous_text=False,
            initial_prompt=WAKE_WORD_DISPLAY,
        )
        return "".join(s.text for s in segments).strip()


_scanner: WakeScanner | None = None


def get_scanner() -> WakeScanner:
    global _scanner
    if _scanner is None:
        _scanner = WakeScanner()
    return _scanner


def _wake_log_enabled() -> bool:
    """默认把扫描听到的内容打出来。唤醒不灵时这是唯一的线索——分不清是没
    听见、听成了别的字，还是压根没在扫。嫌吵设 SA_WAKE_LOG=0。"""
    return os.environ.get("SA_WAKE_LOG", "1").strip().lower() \
        not in ("0", "false", "off", "no", "")


class WakeListener:
    """常开麦克风，听到唤醒词后录下后续内容并回调。

    C++ 侧完全不参与——按键在游戏里拿不到，这条路绕开输入层。
    on_utterance 在监听线程里调用，耗时操作应自行切走。

    麦克风常开是有代价的（一直在录 + 每 0.7 秒跑一次识别），所以 start/stop
    做成可反复调用：调用方按游戏是否正在进行来开关，见 server.py。
    """

    def __init__(self, on_utterance: Callable[[str], None],
                 on_state: Callable[[str], None] | None = None) -> None:
        self._on_utterance = on_utterance
        self._on_state = on_state or (lambda s: None)
        self._stream: sd.InputStream | None = None
        self._stream_lock = threading.Lock()
        self._audio: queue.Queue[np.ndarray] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        # 说话期间挂起监听，免得把 agent 自己的声音当成提问
        self._paused = threading.Event()
        # pause/resume 可能来自多处（朗读、按住说话），用计数避免一方
        # 提前 resume 把另一方还需要的挂起解掉
        self._pause_lock = threading.Lock()
        self._pause_depth = 0

    def _callback(self, indata, frames, time_info, status) -> None:
        if status:
            log.debug("唤醒监听音频状态: %s", status)
        self._audio.put(indata.copy().reshape(-1))

    def _open_stream(self) -> None:
        with self._stream_lock:
            if self._stream is not None:
                return
            stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="float32",
                blocksize=BLOCK, callback=self._callback,
            )
            stream.start()
            self._stream = stream

    def _close_stream(self) -> None:
        with self._stream_lock:
            stream, self._stream = self._stream, None
        if stream is not None:
            stream.stop()
            stream.close()

    def _drain(self) -> None:
        while True:
            try:
                self._audio.get_nowait()
            except queue.Empty:
                return

    @property
    def active(self) -> bool:
        """当前是否在听。start/stop 可以反复来回——服务端用它跟着游戏的起落
        开关麦，见 server.py 的 wake_sync()。"""
        return self._thread is not None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._running.set()
        # 上一轮停下之前攒在队列里的音频已经过期，别让它拼进新一轮的窗口
        self._drain()
        # 挂起期间被重新拉起时先不开麦（游戏重开时上一轮的回答可能还在念），
        # 等 resume() 来开，否则会把 agent 自己的朗读声录进去
        if not self._paused.is_set():
            self._open_stream()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="wake-listener")
        self._thread.start()
        log.info("唤醒监听已启动，说「%s」唤醒", WAKE_WORD_DISPLAY)

    def stop(self) -> None:
        self._running.clear()
        self._close_stream()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=3.0)

    def pause(self) -> None:
        """挂起监听并放开麦克风。

        只置标志不够：常开的输入流会一直把 agent 自己的朗读声灌进队列，
        而"按住说话"那一路还要另开一条输入流录同一个设备。真正关掉流，
        既不会自问自答，也不和 Recorder 抢设备。
        """
        with self._pause_lock:
            self._pause_depth += 1
            first = self._pause_depth == 1
        if first:
            self._paused.set()
            self._close_stream()

    def resume(self) -> None:
        with self._pause_lock:
            if self._pause_depth == 0:
                return
            self._pause_depth -= 1
            last = self._pause_depth == 0
        if not last:
            return
        # 挂起期间攒下的音频要丢掉，否则会把 agent 的回答当提问识别
        self._drain()
        self._paused.clear()
        if self._running.is_set():
            try:
                self._open_stream()
            except Exception as e:
                log.warning("唤醒监听恢复麦克风失败: %s", e)

    def _run(self) -> None:
        win_blocks = int(WAKE_WINDOW_SECONDS * SAMPLE_RATE / BLOCK)
        stride_blocks = int(WAKE_STRIDE_SECONDS * SAMPLE_RATE / BLOCK)
        window = deque(maxlen=win_blocks)
        fresh = 0
        verbose = _wake_log_enabled()

        while self._running.is_set():
            try:
                block = self._audio.get(timeout=0.5)
            except queue.Empty:
                continue

            # 识别期间麦克风没停，队列里攒了一截。一块一块地取会越拖越远：
            # 每扫一次只消化 0.7 秒音频，却花掉一两秒，落后的量只增不减，
            # 最后扫的是几十秒前的声音，玩家早就说完了。这里一次取干，
            # 只留最近的一窗，宁可丢掉中间那段也要扫当下的声音。
            blocks = [block] + self._take_pending()
            if self._paused.is_set():
                # 挂起前后的音频接不上，窗口里的旧块留着只会拼出怪句子
                window.clear()
                fresh = 0
                continue

            dropped = max(0, len(blocks) - win_blocks)
            if dropped:
                log.debug("唤醒扫描跟不上，丢掉 %.1f 秒音频",
                          dropped * BLOCK / SAMPLE_RATE)
            window.extend(blocks)
            fresh += len(blocks)
            if fresh < stride_blocks or len(window) < window.maxlen:
                continue
            fresh = 0

            audio = np.concatenate(list(window))
            # 整段都很安静就别送去识别了，白费算力还容易出幻听
            if float(np.sqrt(np.mean(audio ** 2))) < SILENCE_RMS:
                continue

            try:
                text = get_scanner().scan(audio)
            except Exception as e:
                log.warning("唤醒扫描识别失败: %s", e)
                continue

            if not text:
                continue
            if verbose:
                log.info("唤醒扫描: %s", text)
            else:
                log.debug("唤醒扫描: %s", text)

            if find_wake_word(text) < 0:
                continue

            log.info("检测到唤醒词: %s", text)
            # 窗口里除了唤醒词，往往已经含着问题的开头（"小龟J这关怎么打"
            # 是一口气说的）。扫描要花上一两秒，这段不能丢，否则问题被砍头。
            prefix = list(window)
            window.clear()
            fresh = 0
            self._capture(prefix)

    def _take_pending(self) -> list[np.ndarray]:
        """取走队列里已经到达的音频块，不等待。"""
        out: list[np.ndarray] = []
        while True:
            try:
                out.append(self._audio.get_nowait())
            except queue.Empty:
                return out

    def _capture(self, prefix: list[np.ndarray] | None = None) -> None:
        """唤醒后录下玩家接着说的话，静音一段时间就算说完。

        prefix 是唤醒扫描窗口里的音频，接在录音前面一起识别。
        """
        self._on_state("listening")
        chunks: list[np.ndarray] = list(prefix or [])
        silent_blocks = 0
        need_silent = int(END_SILENCE_SECONDS * SAMPLE_RATE / BLOCK)
        max_blocks = int(UTTERANCE_MAX_SECONDS * SAMPLE_RATE / BLOCK) + len(chunks)
        # 前缀里必然有唤醒词的人声，直接算已经开口，静音计数从这里起算
        spoke = bool(chunks)

        while self._running.is_set() and len(chunks) < max_blocks:
            try:
                block = self._audio.get(timeout=1.0)
            except queue.Empty:
                break
            if self._paused.is_set():
                log.info("唤醒录音被挂起，放弃本轮")
                self._on_state("idle")
                return
            chunks.append(block)
            if float(np.sqrt(np.mean(block ** 2))) < SILENCE_RMS:
                silent_blocks += 1
                # 开口之前的静音不计入结束判断，玩家可能要想一下
                if spoke and silent_blocks >= need_silent:
                    break
                if not spoke and silent_blocks >= need_silent * 3:
                    break
            else:
                spoke = True
                silent_blocks = 0

        if not spoke or not chunks:
            log.info("唤醒后没听到内容")
            self._on_state("idle")
            return

        audio = np.concatenate(chunks)
        log.info("唤醒录音 %.1f 秒", len(audio) / SAMPLE_RATE)
        self._on_state("thinking")

        try:
            text = transcribe(audio)
        except Exception as e:
            log.warning("唤醒录音识别失败: %s", e)
            self._on_state("idle")
            return

        # 唤醒词连在提问里（"小龟J 这个任务怎么过"），要去掉
        text = strip_wake_word(text)

        log.info("唤醒提问: %s", text or "（无内容）")
        if not text:
            self._on_state("idle")
            return
        self._on_utterance(text)
