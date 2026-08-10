"""一次性工具：从 info.zon + american.gxt 生成 zone 表。

产出 overlay/src/zones.inc，供 game.cpp 直接 #include。
只在游戏数据变动时才需要重跑。

GXT (SA 版) 结构：
    TABL 块：每项 12 字节 = 8 字节表名 + 4 字节该表在文件中的偏移
    每张表：MAIN 表直接是 TKEY/TDAT；其余表前有 8 字节表名
    TKEY 块：每项 8 字节 = 4 字节 TDAT 内偏移 + 4 字节 CRC32 key
    TDAT 块：连续的 NUL 结尾字符串
SA 的 key 是 CRC32 哈希，不是明文，所以要用同样的算法反查。
"""

import struct
import zlib
from pathlib import Path

GAME = Path(r"g:\SanAndreas\GTA San Andreas")
OUT = Path(r"g:\SanAndreas\sa-agent\overlay\src\zones.inc")


def gxt_crc(name: str) -> int:
    """SA 的 GXT key 哈希：JAMCRC，即标准 CRC32 但末尾不做取反。
    输入先转大写。"""
    return (~zlib.crc32(name.upper().encode("ascii"))) & 0xFFFFFFFF


def parse_gxt(path: Path) -> dict[int, str]:
    """返回 {crc32 key: 文本}，合并所有表。"""
    data = path.read_bytes()
    out: dict[int, str] = {}

    # 文件头：4 字节版本，其中后 2 字节是字符宽度（8 = 单字节，16 = UTF-16）。
    # EFIGS 版 SA 是 8 位 Windows-1252。
    char_bits = struct.unpack_from("<H", data, 2)[0]
    assert char_bits in (8, 16), f"未知字符宽度: {char_bits}"
    wide = char_bits == 16

    base = 4
    assert data[base:base + 4] == b"TABL", f"不是 SA 版 GXT: {data[base:base+4]!r}"
    tabl_size = struct.unpack_from("<I", data, base + 4)[0]

    tables = []
    for i in range(tabl_size // 12):
        off = base + 8 + i * 12
        name = data[off:off + 8].split(b"\0")[0].decode("ascii")
        addr = struct.unpack_from("<I", data, off + 8)[0]
        tables.append((name, addr))

    for name, addr in tables:
        p = addr
        # 非 MAIN 表在 TKEY 前多一个 8 字节表名
        if data[p:p + 4] != b"TKEY":
            p += 8
        assert data[p:p + 4] == b"TKEY", f"表 {name} 结构异常"

        tkey_size = struct.unpack_from("<I", data, p + 4)[0]
        tkey_start = p + 8

        q = tkey_start + tkey_size
        assert data[q:q + 4] == b"TDAT", f"表 {name} 缺 TDAT"
        tdat_size = struct.unpack_from("<I", data, q + 4)[0]
        tdat_start = q + 8

        for i in range(tkey_size // 8):
            e = tkey_start + i * 8
            txt_off, key = struct.unpack_from("<Ii", data, e)
            s = tdat_start + txt_off
            limit = tdat_start + tdat_size

            if wide:
                end = s
                while end + 1 < limit and data[end:end + 2] != b"\0\0":
                    end += 2
                text = data[s:end].decode("utf-16-le", "replace")
            else:
                end = data.index(b"\0", s, limit)
                text = data[s:end].decode("cp1252", "replace")

            if text:
                out[key & 0xFFFFFFFF] = text

    return out


def parse_zones(path: Path) -> list[tuple]:
    """解析 zon 文件，返回 (x1,y1,z1, x2,y2,z2, island, key) 列表。"""
    zones = []
    for line in path.read_text(encoding="latin-1").splitlines():
        line = line.strip()
        if not line or line in ("zone", "end"):
            continue
        f = [t.strip() for t in line.split(",")]
        if len(f) < 10:
            continue
        # name, type, x1,y1,z1, x2,y2,z2, island, gxtkey
        x1, y1, z1, x2, y2, z2 = (float(v) for v in f[2:8])
        zones.append((x1, y1, z1, x2, y2, z2, int(f[8]), f[9]))
    return zones


# map.zon 的 island 字段：1=洛圣都, 2=圣菲耶罗, 3=拉斯云图斯
CITY_NAMES = {1: "洛圣都", 2: "圣菲耶罗", 3: "拉斯云图斯"}


def cf(v: float) -> str:
    """格式化为合法的 C++ float 字面量（%g 会把 0 输出成 "0"，
    直接加 f 后缀得到非法的 "0f"）。"""
    s = f"{v:g}"
    if "." not in s and "e" not in s and "E" not in s:
        s += ".0"
    return s + "f"


def main() -> None:
    gxt = parse_gxt(GAME / "text" / "american.gxt")
    print(f"GXT 条目: {len(gxt)}")

    zones = parse_zones(GAME / "data" / "info.zon")
    print(f"zone 数: {len(zones)}")

    missing = set()
    rows = []
    for x1, y1, z1, x2, y2, z2, _island, key in zones:
        name = gxt.get(gxt_crc(key))
        if name is None:
            missing.add(key)
            name = key
        rows.append((x1, y1, z1, x2, y2, z2, key, name))

    if missing:
        print(f"未在 GXT 找到的键 ({len(missing)}): {' '.join(sorted(missing))}")

    cities = parse_zones(GAME / "data" / "map.zon")
    print(f"city 数: {len(cities)}")

    lines = [
        "// 由 tools/gen_zones.py 从 info.zon / map.zon / american.gxt 生成，",
        "// 请勿手改。重新生成：python tools/gen_zones.py",
        "",
        f"// 街区 zone（{len(rows)} 项）",
        "static const Zone kZones[] = {",
    ]
    for x1, y1, z1, x2, y2, z2, key, name in rows:
        esc = name.replace("\\", "\\\\").replace('"', '\\"')
        coords = ", ".join(cf(v) for v in (x1, y1, z1, x2, y2, z2))
        lines.append(f'    {{{coords}, "{esc}"}},')
    lines.append("};")
    lines.append("")

    lines.append(f"// 城市 zone（{len(cities)} 项），来自 map.zon")
    lines.append("static const Zone kCities[] = {")
    for x1, y1, z1, x2, y2, z2, island, _key in cities:
        name = CITY_NAMES.get(island, "")
        coords = ", ".join(cf(v) for v in (x1, y1, z1, x2, y2, z2))
        lines.append(f'    {{{coords}, u8"{name}"}},')
    lines.append("};")
    lines.append("")

    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"已写出 {OUT}")


if __name__ == "__main__":
    main()
