"""端到端验证实时语音问答：模拟覆盖层发 L 帧，检查 P 帧回传与朗读。

阶段之间会打时间戳，能看出 speaking 比 idle 早多少——那就是"边生成边念"
省下来的等待。
"""
import asyncio
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


async def main():
    print("连接到 SA Agent 服务（确保 service/start.bat 已运行）")
    reader, writer = await asyncio.open_connection("127.0.0.1", 51678)
    print("已连接")

    def send(ftype: bytes, payload: str = ""):
        body = ftype + payload.encode("utf-8")
        writer.write(struct.pack("<I", len(body)) + body)

    send(b"S", '{"health":50,"wanted":2,"zone":"Ganton"}')
    print("\n发送 L start，录音 4 秒——请对着麦克风提个问题")
    send(b"L", "start")
    await writer.drain()
    await asyncio.sleep(4)

    print("发送 L stop，等待处理……")
    send(b"L", "stop")
    await writer.drain()

    t0 = time.monotonic()
    while True:
        header = await reader.readexactly(4)
        length = struct.unpack("<I", header)[0]
        body = await reader.readexactly(length)
        ftype, payload = body[:1], body[1:].decode("utf-8", errors="replace")

        if ftype == b"E":
            print(f"[错误] {payload}")
            break
        if ftype == b"P":
            print(f"[{time.monotonic() - t0:5.2f}s] [阶段] {payload}")
            # idle 才是收尾。speaking 之后还有整段朗读，提前退出会看不到
            # 首句延迟到底省了多少
            if payload == "idle":
                break
        else:
            print(f"[其他帧 {ftype.decode()}] {payload!r}")

    writer.close()
    await writer.wait_closed()
    print("\n测试完成")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n用户中断")
    except Exception as e:
        print(f"\n测试失败: {e}")
