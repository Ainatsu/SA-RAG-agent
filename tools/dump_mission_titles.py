"""从 main.scm 反推 脚本代号 → 任务标题 的真实对应关系。

原理：每个任务脚本在 SCRIPT_NAME (03A4) 之后不远处会用 GXT key 显示任务标题
（00BA print_big 等）。把每个脚本区段内出现的 GXT key 解析成文本，
即可得到有实据的对应关系，无需猜测。

输出供人工筛选后写入 rag/missions.py。
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from gen_zones import GAME, gxt_crc, parse_gxt

SCM = GAME / "data" / "script" / "main.scm"

# 静态短字符串参数标记：SCRIPTPARAM_STATIC_SHORT_STRING
SHORT_STR = 0x09


def find_script_names(data: bytes) -> list[tuple[int, str]]:
    """返回 [(偏移, 代号)]，按偏移升序。"""
    out = []
    i = 0
    while True:
        i = data.find(b"\xa4\x03\x09", i)
        if i < 0:
            break
        raw = data[i + 3:i + 11].split(b"\0")[0]
        if re.fullmatch(rb"[A-Za-z0-9_]{2,8}", raw):
            out.append((i, raw.decode("ascii")))
        i += 3
    return out


def short_strings_in(data: bytes, start: int, end: int) -> list[str]:
    """扫出区段内所有静态短字符串参数。"""
    out = []
    i = start
    while i < end - 9:
        if data[i] == SHORT_STR:
            raw = data[i + 1:i + 9]
            s = raw.split(b"\0")[0]
            if re.fullmatch(rb"[A-Za-z0-9_]{2,8}", s):
                out.append(s.decode("ascii"))
                i += 9
                continue
        i += 1
    return out


def looks_like_title(text: str) -> bool:
    """任务标题的特征：纯文本、首字母大写、无格式控制码、不是完整句子。

    排除对白（含 ~z~ 等颜色码、以小写开头、带句末标点）和提示语
    （"Get in the car" 这类以动词开头的指令）。
    """
    if "~" in text or "$" in text:
        return False
    if not (4 <= len(text) <= 40):
        return False
    if not text[0].isupper():
        return False
    if text.endswith((".", "!", "?", ",", ":")):
        return False
    # 指令提示常以这些动词开头
    first = text.split()[0].lower()
    if first in {"get", "go", "drive", "take", "find", "put", "wait", "walk",
                 "then", "run", "steal", "kill", "follow", "return", "defeat",
                 "which", "be", "you", "your", "all", "now", "hold", "stop",
                 "shoot", "if", "come", "cops", "score", "time", "high"}:
        return False
    return True


def main() -> None:
    data = SCM.read_bytes()
    gxt = parse_gxt(GAME / "text" / "american.gxt")

    names = find_script_names(data)
    print(f"脚本代号 {len(names)} 个\n")

    for idx, (off, script) in enumerate(names):
        end = names[idx + 1][0] if idx + 1 < len(names) else len(data)
        window_end = min(end, off + 4000)

        seen: list[tuple[str, str]] = []
        for key in short_strings_in(data, off + 11, window_end):
            text = gxt.get(gxt_crc(key))
            if text and looks_like_title(text) and text not in [t for _, t in seen]:
                seen.append((key, text))

        if seen:
            shown = "; ".join(f"{k}={t}" for k, t in seen[:5])
            print(f"{script:<10} {shown}")


if __name__ == "__main__":
    main()
