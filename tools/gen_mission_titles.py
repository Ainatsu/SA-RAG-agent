"""从 main.scm 反推 脚本代号 → 任务标题，并生成 rag/missions.py 的映射表。

原理与证据链：
  1. 每个任务脚本用 SCRIPT_NAME (03A4) 声明自己的代号。
  2. 任务标题以 GXT key 的形式出现在脚本区段内（开场显示任务名）。
  3. 同一个标题键通常出现在两处：调度它的主线程（如 SWEET）与任务脚本
     本身（如 SWEET1）。取"非主线程"的那个，即得到代号→标题的对应。

因此映射关系有实据，不依赖对代号命名的猜测。
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from gen_zones import GAME, gxt_crc, parse_gxt

SCM = GAME / "data" / "script" / "main.scm"
OUT = Path(__file__).resolve().parent.parent / "rag" / "mission_titles.py"
OUT_INC = Path(__file__).resolve().parent.parent / "overlay" / "src" / "missions.inc"

SHORT_STR = 0x09


def find_script_names(data: bytes) -> list[tuple[int, str]]:
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
    out = []
    i = start
    while i < end - 9:
        if data[i] == SHORT_STR:
            s = data[i + 1:i + 9].split(b"\0")[0]
            if re.fullmatch(rb"[A-Za-z0-9_]{2,8}", s):
                out.append(s.decode("ascii"))
                i += 9
                continue
        i += 1
    return out


def looks_like_title(text: str) -> bool:
    """任务标题：纯文本、首字母大写、无格式码、非完整句子、非指令提示。"""
    if "~" in text or "$" in text:
        return False
    if not (4 <= len(text) <= 40):
        return False
    if not text[0].isupper():
        return False
    if text.endswith((".", "!", "?", ",", ":")):
        return False
    if text.isupper():          # HEALTH、SWEET 之类的 HUD 标签
        return False
    first = text.split()[0].lower()
    if first in {"get", "go", "drive", "take", "find", "put", "wait", "walk",
                 "then", "run", "steal", "kill", "follow", "return", "defeat",
                 "which", "be", "you", "your", "all", "now", "hold", "stop",
                 "shoot", "if", "come", "cops", "score", "time", "high"}:
        return False
    return True


# 只认这些前缀的键为"任务标题"。它们是各条任务线的标题键命名空间，
# 由 dump_mission_titles.py 的输出观察得出。
TITLE_PREFIXES = (
    "SWEET", "STRAP", "SMOKE", "RYDER", "CRASH", "MAN_", "HEIST", "ZERO",
    "SYND", "WUZI", "CAT_", "CATCUT", "DESERT", "GROVE", "RIOT", "LA1FIN",
    "TRUTH", "STEAL", "CASINO", "CASEEN", "CASIN10", "BCESAR", "BCRASH",
    "GARAGE", "GAR_", "FAR_", "TRACE", "SCRA", "VCRASH", "DOC_", "OTB",
    "TRUCK", "QUARRY", "PIMP", "BLOOD",
)

# 机械提取会误配的脚本，逐个核对后排除，理由如下：
#   cprace  — 街头竞速总控，覆盖几十场比赛，却只匹配到其中一场 Dirt Track
#   catcut  — Catalina 过场调度，误匹配到无关的 BCESAR2 (Big Smoke's Cash)
#   r3      — 出租/警车/拉皮条等多种支线的总控，只匹配到 Pimping 一项
# 与其给出一个片面的名字，不如不给，让它退化为"正在进行任务"。
EXCLUDE = {"CPRACE", "CATCUT", "R3"}


def main() -> None:
    data = SCM.read_bytes()
    gxt = parse_gxt(GAME / "text" / "american.gxt")
    names = find_script_names(data)

    # 每个标题键出现在哪些脚本区段
    loc: dict[tuple[str, str], list[str]] = {}
    for idx, (off, script) in enumerate(names):
        end = names[idx + 1][0] if idx + 1 < len(names) else len(data)
        for key in short_strings_in(data, off + 11, end):
            text = gxt.get(gxt_crc(key))
            if not text or not looks_like_title(text):
                continue
            if not key.upper().startswith(TITLE_PREFIXES):
                continue
            entry = loc.setdefault((key, text), [])
            if script not in entry:
                entry.append(script)

    # 调度线程会引用同一条线上的多个标题（如 SWEET 引用 SWEET_1..SWEET_7），
    # 而任务脚本本身只引用自己的那一个。据此把调度线程剔除，
    # 剩下的即"脚本 ↔ 标题"一对一关系。
    refs: dict[str, set[str]] = {}
    for (_key, text), scripts in loc.items():
        for script in scripts:
            refs.setdefault(script.upper(), set()).add(text)

    mapping: dict[str, str] = {}
    for (_key, text), scripts in loc.items():
        for script in scripts:
            su = script.upper()
            if len(refs[su]) != 1:
                continue        # 调度线程，跳过
            if su in EXCLUDE:
                continue
            mapping[su] = text

    print(f"提取到 {len(mapping)} 条 代号→标题")
    multi = {s for s, t in refs.items() if len(t) > 1}
    print(f"已排除的调度线程 {len(multi)} 个: {' '.join(sorted(multi))}")

    lines = [
        '"""由 tools/gen_mission_titles.py 从 main.scm + american.gxt 提取，请勿手改。',
        "",
        "每条对应关系都有实据：任务标题的 GXT key 出现在该任务脚本的字节码区段内。",
        "重新生成：python tools/gen_mission_titles.py",
        '"""',
        "",
        "# 脚本代号（小写）→ 游戏内任务标题（英文原文）",
        "SCRIPT_TITLES: dict[str, str] = {",
    ]
    for script in sorted(mapping):
        title = mapping[script].replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'    "{script.lower()}": "{title}",')
    lines.append("}")
    lines.append("")

    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"已写出 {OUT}")

    # 覆盖层状态面板也要显示任务名，共用同一份提取结果，避免两处各自维护。
    # 这里给的是游戏内原标题，不做 wiki 页面名替换——面板是给玩家看的，
    # 不该出现 "Jizzy (mission)" 这种检索用写法。
    inc = [
        "// 由 tools/gen_mission_titles.py 从 main.scm + american.gxt 生成，",
        "// 请勿手改。重新生成：python tools/gen_mission_titles.py",
        "",
        f"// 脚本代号 → 游戏内任务标题（{len(mapping)} 项）",
        "static const MissionEntry kMissionTitles[] = {",
    ]
    for script in sorted(mapping):
        title = mapping[script].replace("\\", "\\\\").replace('"', '\\"')
        inc.append(f'    {{"{script.lower()}", "{title}"}},')
    inc.append("};")
    inc.append("")

    OUT_INC.write_text("\n".join(inc), encoding="utf-8")
    print(f"已写出 {OUT_INC}")


if __name__ == "__main__":
    main()
