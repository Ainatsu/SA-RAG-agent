"""扫描 main.scm，提取所有 SCRIPT_NAME (opcode 03A4) 设置的脚本代号。

用途：确定任务脚本名的完整集合，作为 script→任务名 映射表的基准，
避免映射表里出现游戏中根本不存在的代号。

opcode 03A4 的参数是 SCRIPTPARAM_STATIC_SHORT_STRING（类型标记 0x09）+ 8 字节定长串。
"""

import re
import sys
from pathlib import Path

SCM = Path(r"g:\SanAndreas\GTA San Andreas\data\script\main.scm")


def main() -> None:
    data = SCM.read_bytes()
    names = []
    seen = set()

    i = 0
    while True:
        i = data.find(b"\xa4\x03\x09", i)
        if i < 0:
            break
        raw = data[i + 3:i + 11]
        # 8 字节定长，NUL 填充
        name = raw.split(b"\0")[0]
        if re.fullmatch(rb"[A-Za-z0-9_]{2,8}", name):
            key = name.decode("ascii")
            if key not in seen:
                seen.add(key)
                names.append(key)
        i += 3

    print(f"共 {len(names)} 个脚本代号\n")
    for n in sorted(names):
        print(n)


if __name__ == "__main__":
    main()
