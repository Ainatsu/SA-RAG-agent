"""端到端验证语音链路：模拟覆盖层发 V 帧，检查 X 帧回传。"""
import asyncio
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


async def main():
    print("连接到 SA Agent 服务（确保 service/start.bat 已运行）")
    reader, writer = await asyncio.open_connection("127.0.0.1", 51678)
    print("已连接")

    def send(ftype: bytes, payload: str = ""):
        body = ftype + payload.encode("utf-8")
        writer.write(struct.pack("<I", len(body)) + body)

    print("\n发送 V start，录音 3 秒——请对着麦克风说句话")
    send(b"V", "start")
    await writer.drain()
    await asyncio.sleep(3)

    print("发送 V stop，等待识别结果……")
    send(b"V", "stop")
    await writer.drain()

    while True:
        header = await reader.readexactly(4)
        length = struct.unpack("<I", header)[0]
        body = await reader.readexactly(length)
        ftype, payload = body[:1], body[1:].decode("utf-8", errors="replace")
        
        if ftype == b"E":
            print(f"❌ 错误: {payload}")
        elif ftype == b"X":
            print(f"✓ 识别结果: {payload!r}")
            break
        else:
            print(f"收到帧 {ftype.decode()}: {payload!r}")

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
