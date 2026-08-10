"""离线验证唤醒词那一路：匹配规则 + 扫描识别 + 监听器全流程。

不需要麦克风：用 SAPI 合成一句「小龟J，这个任务怎么打」当输入，按 100 ms
一块喂给 WakeListener，看它能不能唤醒、能不能把问题完整交出来。
需要装好 faster-whisper 和模型（首次运行会下载 tiny）。
"""
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "service"))

import voice  # noqa: E402

WAV = HERE / "_wake_sample.wav"
PHRASE = "小龟J，这个任务怎么打"

# 实测的识别结果（TTS 音源 + 真人音源都出现过）。唤醒词是生造的名字，
# 模型没见过，写成同音字是常态，匹配规则必须容得下这些。
CASES = [
    ("小龟J", ""),
    ("小龟J 这个任务怎么打", "这个任务怎么打"),
    ("小规矩，这个任务怎么打", "这个任务怎么打"),
    ("小规罪,这个任务", "这个任务"),
    ("小規刺、这个任务。", "这个任务"),
    ("小龟翠 我现在该去哪", "我现在该去哪"),
    ("小归鸡这关怎么过", "这关怎么过"),
]
MISSES = ["这个任务怎么打", "小明在哪", "打开地图"]


def synth() -> None:
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    v = win32com.client.Dispatch("SAPI.SpVoice")
    for t in v.GetVoices():
        if "Chinese" in t.GetDescription() or "中文" in t.GetDescription():
            v.Voice = t
            break
    fs = win32com.client.Dispatch("SAPI.SpFileStream")
    fs.Format.Type = 18          # SAFT16kHz16BitMono，正好是识别要的格式
    fs.Open(str(WAV), 3, False)  # SSFMCreateForWrite
    v.AudioOutputStream = fs
    v.Speak(PHRASE, 0)           # 同步，念完再返回
    fs.Close()


def sample() -> np.ndarray:
    if not WAV.exists():
        synth()
    with wave.open(str(WAV), "rb") as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == \
            (voice.SAMPLE_RATE, 1, 2), "合成出来的 wav 格式不对"
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def test_match() -> None:
    print("── 唤醒词匹配 ──")
    for text, want in CASES:
        assert voice.find_wake_word(text) >= 0, f"{text!r} 没认出唤醒词"
        got = voice.strip_wake_word(text)
        assert got == want, f"{text!r} 剥完是 {got!r}，应该是 {want!r}"
        print(f"  {text!r:<24} -> {got!r}")
    for text in MISSES:
        assert voice.find_wake_word(text) < 0, f"{text!r} 不该算唤醒"
    print("  OK\n")


def test_scan(audio: np.ndarray) -> None:
    """扫描一个窗口的耗时必须小于扫描间隔，否则积压会越拖越远。"""
    print("── 扫描 ──")
    scanner = voice.get_scanner()
    t0 = time.monotonic()
    scanner.load()
    print(f"  模型 {scanner.name} 加载 {time.monotonic() - t0:.2f}s")

    win = audio[:int(voice.WAKE_WINDOW_SECONDS * voice.SAMPLE_RATE)]
    best = None
    for _ in range(3):
        t1 = time.monotonic()
        text = scanner.scan(win)
        dt = time.monotonic() - t1
        best = dt if best is None else min(best, dt)
    print(f"  {best:.2f}s / {voice.WAKE_WINDOW_SECONDS:.1f}s 窗口: {text!r}")

    assert voice.find_wake_word(text) >= 0, f"扫不出唤醒词: {text!r}"
    assert best < voice.WAKE_STRIDE_SECONDS, \
        f"扫描 {best:.2f}s 超过间隔 {voice.WAKE_STRIDE_SECONDS}s，会一直积压"
    print("  OK\n")


def test_listener(audio: np.ndarray) -> None:
    """全流程：静音 → 说话 → 静音，监听器应当唤醒并交出问题。"""
    print("── 监听器 ──")
    heard: list[str] = []
    states: list[str] = []
    done = threading.Event()

    def on_utterance(text: str) -> None:
        heard.append(text)
        done.set()

    listener = voice.WakeListener(on_utterance, states.append)
    # 没有麦克风，音频由测试直接投进队列
    listener._open_stream = lambda: None
    listener._close_stream = lambda: None
    listener.start()

    silence = np.zeros(int(2.0 * voice.SAMPLE_RATE), dtype=np.float32)
    feed = np.concatenate([silence, audio, silence])
    n = voice.BLOCK
    t0 = time.monotonic()

    def feeder() -> None:
        for i in range(len(feed) // n):
            if done.is_set():
                return
            listener._audio.put(feed[i * n:(i + 1) * n])
            time.sleep(n / voice.SAMPLE_RATE)   # 按实时速率喂

    threading.Thread(target=feeder, daemon=True).start()
    got = done.wait(timeout=90)
    listener.stop()

    print(f"  {time.monotonic() - t0:.1f}s  阶段 {states}")
    assert got, "没有唤醒"
    print(f"  问题: {heard[0]!r}")
    assert voice.find_wake_word(heard[0]) < 0, "问题里还留着唤醒词"
    assert len(heard[0]) >= 4, f"问题被砍得太短: {heard[0]!r}"
    assert states[:2] == ["listening", "thinking"], f"阶段不对: {states}"
    print("  OK\n")


if __name__ == "__main__":
    audio = sample()
    print(f"样本 {PHRASE!r} {len(audio) / voice.SAMPLE_RATE:.2f}s\n")
    test_match()
    test_scan(audio)
    test_listener(audio)
    print("全部通过")
