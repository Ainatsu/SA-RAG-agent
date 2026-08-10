"""从 gta_sa.exe 提取版本指纹字节，并核算 CPed 结构偏移。

一次性核验工具，用完即删。
"""
import struct
from pathlib import Path

EXE = Path(r"G:\SanAndreas\GTA San Andreas\gta_sa.exe")

# .text 段：VA 0x00401000 对应文件偏移 0x400
TEXT_VA = 0x00401000
TEXT_RAW = 0x400


def va_to_raw(va):
    return va - TEXT_VA + TEXT_RAW


def main():
    data = EXE.read_bytes()

    # 选三个我们实际依赖的函数入口作为指纹（均来自 plugin-sdk 公布的 1.0 US 地址）
    sites = {
        "CStats::GetStatValue": 0x558E40,
        "CWorld::FindPlayerSlotWithPedPointer": 0x563FA0,
        "CStats::GetStatID": 0x558DE0,
    }
    print("版本指纹候选（各取入口 8 字节）：")
    for name, va in sites.items():
        raw = va_to_raw(va)
        b = data[raw:raw + 8]
        print(f"  {name:<40} VA 0x{va:06X} -> {' '.join(f'{x:02X}' for x in b)}")

    # 核算 CPed 偏移链（依据 plugin-sdk CPed.h 的成员顺序）
    print("\nCPed 关键偏移推算：")
    off = {
        "m_fHealth": 0x540,
        "m_fMaxHealth": 0x544,
        "m_fArmour": 0x548,
        "m_pVehicle": 0x58C,
        "m_nPedType": 0x598,
        "m_aWeapons": 0x5A0,
        "m_nSelectedWepSlot": 0x718,
    }
    for k, v in off.items():
        print(f"  {k:<22} 0x{v:03X}")

    # m_aWeapons 13 项 * 0x1C = 0x16C，起点 0x5A0 -> 终点 0x70C
    end = 0x5A0 + 13 * 0x1C
    print(f"\n  m_aWeapons 结束于 0x{end:03X}"
          f"  (m_nSavedWeapon 应在此处，plugin-sdk 顺序一致: {'OK' if end == 0x70C else '不一致'})")
    # 0x70C savedWeapon, 0x710 delayedWeapon, 0x714 delayedAmmo, 0x718 selectedSlot
    print(f"  0x70C+0x4*3 = 0x{0x70C + 12:03X}"
          f"  应等于 m_nSelectedWepSlot 0x718: {'OK' if 0x70C + 12 == 0x718 else '不一致'}")

    # CStats 索引核对：StatTypesFloat = 0xB79380
    print("\nCStats 浮点统计索引核对（基址 0xB79380）：")
    base = 0xB79380
    known = {21: ("Fat", 0xB793D4), 22: ("Stamina", 0xB793D8),
             23: ("Muscle", 0xB793DC), 24: ("MaxHealth", 0xB793E0),
             25: ("SexAppeal", 0xB793E4)}
    for idx, (name, addr) in known.items():
        calc = base + idx * 4
        print(f"  索引 {idx:<3} {name:<10} 计算 0x{calc:X}  "
              f"SDA 实测 0x{addr:X}  {'OK' if calc == addr else '不一致'}")


if __name__ == "__main__":
    main()
