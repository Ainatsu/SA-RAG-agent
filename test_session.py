"""离线验证「只在游戏进行中才收语音」这道闸门。

不需要麦克风、Whisper 模型和 DeepSeek 密钥：以假覆盖层的身份直接连上
server.handle_client，再拿一个假的 WakeListener 观察麦克风何时开、何时关。
"""
import asyncio
import logging
import struct
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "service"))

import server  # noqa: E402

HOST = "127.0.0.1"
PORT = 51679          # 换个端口，别撞上正在跑的正式服务

# 覆盖层推来的状态帧，字段取够用的一小部分即可
STATE = ('{"health":100,"max_health":100,"armour":0,'
         '"x":2488.0,"y":-1666.0,"z":13.0,"wanted":0}')


class FakeListener:
    """替掉真的 WakeListener：只记账，不碰麦克风。"""

    def __init__(self) -> None:
        self.active = False
        self.starts = 0
        self.stops = 0
        self.pauses = 0

    def start(self) -> None:
        self.active = True
        self.starts += 1

    def stop(self) -> None:
        self.active = False
        self.stops += 1

    def pause(self) -> None:
        self.pauses += 1

    def resume(self) -> None:
        pass


def frame(ftype: bytes, payload: str = "") -> bytes:
    body = ftype + payload.encode("utf-8")
    return struct.pack("<I", len(body)) + body


async def wait_for(cond, what: str, timeout: float = 5.0) -> None:
    """等条件成立。开关麦是异步做的（要切线程），发完帧不能立刻断言。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"等「{what}」超过 {timeout:.0f} 秒")


async def main() -> None:
    server.GAME_IDLE_TIMEOUT = 2.0      # 正式值 15 秒，测试等不起
    fake = FakeListener()
    server.wake_listener = fake         # 假装识别模型刚刚就绪

    srv = await asyncio.start_server(server.handle_client, HOST, PORT)
    watchdog = asyncio.create_task(server.game_watchdog())

    print("── 服务刚起来，游戏还没开：不该在听 ──")
    assert not fake.active and fake.starts == 0, "没游戏就开麦了"
    print("  OK\n")

    print("── 覆盖层连上了，但还在主菜单（状态帧是 {}）：仍然不该在听 ──")
    reader, writer = await asyncio.open_connection(HOST, PORT)
    writer.write(frame(b"S", "{}"))
    writer.write(frame(b"L", "heartbeat"))
    await writer.drain()
    await asyncio.sleep(0.5)
    assert not server.game_active, "空状态不该算游戏正在进行中"
    assert not fake.active, "主菜单里麦克风不该开"
    print("  OK\n")

    print("── 进了存档，状态帧有内容：开麦 ──")
    writer.write(frame(b"S", STATE))
    writer.write(frame(b"L", "heartbeat"))
    await writer.drain()
    await wait_for(lambda: fake.active, "开麦")
    assert server.game_active
    assert fake.starts == 1, f"开麦次数不对: {fake.starts}"
    print(f"  OK（start × {fake.starts}）\n")

    print("── 关麦之后交上来的唤醒提问要丢掉 ──")
    server.game_active = False
    await server.handle_wake_question("这关怎么打")
    assert fake.pauses == 0, "游戏不在跑，这一句本该被丢掉"
    server.game_active = True
    print("  OK\n")

    print(f"── 状态帧断流 {server.GAME_IDLE_TIMEOUT:.0f} 秒"
          "（退回主菜单 / 切到后台）：关麦 ──")
    await wait_for(lambda: not fake.active, "关麦",
                   timeout=server.GAME_IDLE_TIMEOUT + 3.0)
    assert not server.game_active
    assert fake.stops == 1, f"关麦次数不对: {fake.stops}"
    print(f"  OK（stop × {fake.stops}）\n")

    print("── 回到游戏里：重新开麦 ──")
    writer.write(frame(b"S", STATE))
    await writer.drain()
    await wait_for(lambda: fake.active, "重新开麦")
    assert fake.starts == 2, f"开麦次数不对: {fake.starts}"
    print(f"  OK（start × {fake.starts}）\n")

    print("── 游戏退出（覆盖层断开）：立刻关麦 ──")
    writer.close()
    await writer.wait_closed()
    await wait_for(lambda: not fake.active, "断开后关麦")
    assert not server.game_active
    assert fake.stops == 2, f"关麦次数不对: {fake.stops}"
    # 会话缓存也要跟着清掉，否则换个存档进来会拿上一局的数据作答
    assert server.prev_state is None, "差分基线没清"
    assert server.latest_state is None, "状态缓存没清"
    assert server.latest_pickups == [], "拾取物清单没清"
    print(f"  OK（stop × {fake.stops}）\n")

    watchdog.cancel()
    srv.close()
    await srv.wait_closed()
    print("全部通过")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="        %(message)s")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n用户中断")
