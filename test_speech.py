"""离线验证语音输出：分句 + 边生成边念 + 打断。

不需要麦克风、不需要服务端、不调 DeepSeek，只用一个假的流式生成器。
会出声，音量调好再跑。
"""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "service"))

import speech


ANSWER = ("先稳住，别硬刚。开车绕到后面那条巷子，从侧门摸进去。"
          "打完记得捡地上的护甲，奖励是两千块和一点尊敬。")


def fake_stream(text: str, delay: float = 0.05):
    """模拟 DeepSeek 的流式输出：一个字一个字往外吐。"""
    for ch in text:
        time.sleep(delay)
        yield ch


def test_segmenter() -> None:
    print("── 分句 ──")
    seg = speech.Segmenter()
    out = []
    for ch in ANSWER:
        out += seg.feed(ch)
    tail = seg.flush()
    if tail:
        out.append(tail)

    for s in out:
        print(f"  {len(s):>3} 字  {s}")
    assert len(out) >= 3, "至少该切成三句"
    assert "".join(out) == ANSWER, "切完拼不回原文"
    assert all(len(s) >= 4 for s in out), "有片段短到不值得单独念"
    print("  OK\n")


def test_stream() -> None:
    """边生成边念：出第一声的时间应该远小于整段生成完的时间。"""
    print("── 边生成边念 ──")
    speaker = speech.get_speaker()
    speaker.begin()

    seg = speech.Segmenter()
    t0 = time.monotonic()
    first = None
    for chunk in fake_stream(ANSWER):
        for s in seg.feed(chunk):
            if first is None:
                first = time.monotonic() - t0
                print(f"  首句出声  {first:.2f}s：{s}")
            speaker.push(s)
    tail = seg.flush()
    if tail:
        speaker.push(tail)

    gen = time.monotonic() - t0
    speaker.finish()
    print(f"  生成结束  {gen:.2f}s")
    print(f"  念完      {time.monotonic() - t0:.2f}s")

    assert first is not None, "一句都没念出来"
    assert first < gen * 0.6, f"首句延迟 {first:.2f}s 相对生成 {gen:.2f}s 没省下来"
    print("  OK\n")


def test_interrupt() -> None:
    """念到一半打断：finish 要立刻返回，不能等整段念完。"""
    print("── 打断 ──")
    speaker = speech.get_speaker()
    speaker.begin()
    speaker.push("这一段很长，正常念完要好几秒，"
                 "但是玩家在中途就按了侧键要问下一个问题，"
                 "所以它应该在一秒之内被掐掉。")

    threading.Timer(1.0, speaker.stop).start()
    t0 = time.monotonic()
    speaker.finish()
    cost = time.monotonic() - t0
    print(f"  finish 返回于 {cost:.2f}s")
    assert cost < 2.5, f"打断没生效，等了 {cost:.2f}s"
    print("  OK\n")


if __name__ == "__main__":
    test_segmenter()
    test_stream()
    test_interrupt()
    print("全部通过")
