"""语料覆盖审计：抓了哪些页、切出多少块、哪些点名页面缺失。"""
import json
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "data" / "raw" / "pages.jsonl"
CHUNKS = ROOT / "data" / "chunks.jsonl"

raw = {}
with RAW.open(encoding="utf-8") as f:
    for line in f:
        if line.strip():
            d = json.loads(line)
            raw[d["title"]] = d

chunks = []
with CHUNKS.open(encoding="utf-8") as f:
    for line in f:
        if line.strip():
            chunks.append(json.loads(line))

per_title = Counter(c["title"] for c in chunks)

print(f"raw 页面 {len(raw)}  |  chunk {len(chunks)}  |  有块的页面 {len(per_title)}")
print(f"抓到但零块的页面：{len(raw) - len(set(raw) & set(per_title))}")

# 命名空间分布
ns = Counter()
for t in raw:
    ns[t.split(":", 1)[0] if ":" in t and t.split(":", 1)[0] in
       ("Map", "Category", "Template", "File", "User blog") else "(main)"] += 1
print("\n命名空间分布:", dict(ns))

# 用户点名的三个层级
PROBE = [
    "Category:GTA San Andreas",
    "Characters in GTA San Andreas",
    "Kendl Johnson",
    "Carl Johnson",
    "Sweet Johnson",
    "Big Smoke",
    "Ryder",
    "Cesar Vialpando",
    "Grand Theft Auto: San Andreas",
    "Oysters",
    "Horseshoes",
    "Map:GTA San Andreas: Oysters",
    "Map:GTA San Andreas: Horseshoes",
    "Map:GTA San Andreas: Dating Guide",
    "Map:GTA San Andreas: Export Vehicles",
    "Map:GTA San Andreas: Items and Weapons",
]
print("\n=== 点名页面 ===")
for t in PROBE:
    if t in raw:
        n = per_title.get(t, 0)
        flag = "OK " if n else "!! "
        print(f"  {flag}{t}: raw {len(raw[t]['wikitext'])} 字符 → {n} 块")
    else:
        print(f"  XX {t}: 未抓取")

# Map: 页的块内容抽样——JSON 当 wikitext 洗会出垃圾
print("\n=== Map: 页产出的块抽样 ===")
mapchunks = [c for c in chunks if c["title"].startswith("Map:")]
print(f"Map: 页共 {len(mapchunks)} 块")
for c in mapchunks[:3]:
    print(f"  --- [{c['title']}] §{c['section']} {len(c['text'])} 字符")
    print("      " + c["text"][:300].replace("\n", " ⏎ "))

# 角色覆盖：随便挑一批 SA 主要人物看在不在
print("\n=== 主要人物页覆盖 ===")
PEOPLE = ["Carl Johnson", "Sweet Johnson", "Kendl Johnson", "Big Smoke",
          "Ryder", "Cesar Vialpando", "Wu Zi Mu", "Mike Toreno",
          "The Truth", "Catalina", "Frank Tenpenny", "Eddie Pulaski",
          "Jeffrey Cross", "Melvin Harris", "Lance Wilson",
          "Officer Tenpenny", "Madd Dogg", "OG Loc", "Zero",
          "Jizzy B.", "T-Bone Mendez", "Ken Rosenberg", "Salvatore Leone",
          "Millie Perkins", "Denise Robinson", "Michelle Cannes",
          "Helena Wankstein", "Barbara Schternvart", "Katie Zhan"]
miss = [p for p in PEOPLE if p not in raw]
print(f"  在库 {len(PEOPLE) - len(miss)}/{len(PEOPLE)}；缺: {miss}")

# chunk 里含地名的收集品文本有没有
print("\n=== 收集品位置线索 ===")
for kw in ["Oyster", "Horseshoe", "Snapshot"]:
    hit = [c for c in chunks if kw.lower() in c["text"].lower()]
    print(f"  '{kw}' 出现在 {len(hit)} 块中")

# 最大/最小页面，看清洗是否吞内容
zero = [t for t in raw if t not in per_title]
print(f"\n=== 零块页面前 25 个（共 {len(zero)}）===")
for t in sorted(zero, key=lambda x: -len(raw[x]["wikitext"]))[:25]:
    print(f"  {len(raw[t]['wikitext']):7d} 字符  {t}")
