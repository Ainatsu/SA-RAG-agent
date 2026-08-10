"""GTA Wiki 语料抓取。

只走 MediaWiki API（网页与 S3 dump 均返回 403）。
页面清单有两个来源：
1. Template:Gtasa missions 的嵌入反查——wiki 自身维护的 SA 任务导航模板；
2. Category:GTA San Andreas 及其子分类的递归枚举——覆盖角色、载具、
   武器、地点等任务模板够不到的百科条目。

产出 data/raw/pages.jsonl，每行一个页面的原始 wikitext。
重复运行会跳过已抓取的页面，可安全中断续传。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://gta.fandom.com/api.php"
UA = "SA-Agent/0.1 (personal RAG project for GTA:SA walkthroughs)"

# SA 任务导航模板：wiki 自身维护的权威清单
MISSION_TEMPLATE = "Template:Gtasa missions"

# SA 百科分类根节点，递归展开子分类
ROOT_CATEGORY = "Category:GTA San Andreas"

# 跳过的子分类：纯图片/地图素材、现实人物名单，对文字问答无价值
SKIP_CATEGORIES = {
    "category:images of gta san andreas",
    "category:maps of gta san andreas",
    "category:real-world people credited on gta san andreas",
}

# 额外补充的通用攻略页（任务模板覆盖不到的内容）
EXTRA_PAGES = [
    "Grand Theft Auto: San Andreas",
    "Grand Theft Auto: San Andreas/Official Strategy Guide",
    "Grand Theft Auto: San Andreas/Official Strategy Guide/Basics",
    "Grand Theft Auto: San Andreas/Official Strategy Guide/Chapter 1: Los Santos",
    "Grand Theft Auto: San Andreas/Gameplay Features",
    "100% Completion in GTA San Andreas",
    "Achievements and Trophies in GTA San Andreas",
    "Weapons in GTA San Andreas",
    "Vehicles in GTA San Andreas",
    "Gang Wars",
    "Girlfriends in GTA San Andreas",
    "Safehouses in GTA San Andreas",
    "Oysters",
    "Horseshoes",
    "Snapshots",
    "Tags",
    "Stunt Jumps in GTA San Andreas",
    "Cheats in GTA San Andreas",
    "Respect",
    "Muscle",
    "Stamina",
    "Lung Capacity",
    "Sex Appeal",
    "Driving Skill",
    "Weapon Skills",
]

# 交互地图页（Map: 命名空间）。内容是 JSON，每个标记带收集品所在地名，
# 是牡蛎/马蹄铁/照片这类"在哪找"问题唯一的结构化位置来源。
MAP_PAGES = [
    "Map:GTA San Andreas: Oysters",
    "Map:GTA San Andreas: Horseshoes",
    "Map:GTA San Andreas: Dating Guide",
    "Map:GTA San Andreas: Export Vehicles",
    "Map:GTA San Andreas: Items and Weapons",
]

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
PAGES_FILE = RAW_DIR / "pages.jsonl"

log = logging.getLogger("fetch")


class Wiki:
    def __init__(self, proxy: str | None, delay: float):
        handlers = []
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        self._opener = urllib.request.build_opener(*handlers)
        self._delay = delay
        self._last = 0.0

    def _throttle(self) -> None:
        gap = time.monotonic() - self._last
        if gap < self._delay:
            time.sleep(self._delay - gap)
        self._last = time.monotonic()

    def get(self, **params) -> dict:
        params.setdefault("format", "json")
        params.setdefault("formatversion", "2")
        url = API + "?" + urllib.parse.urlencode(params)

        last_err: Exception | None = None
        for attempt in range(4):
            self._throttle()
            req = urllib.request.Request(url)
            req.add_header("User-Agent", UA)
            req.add_header("Accept-Encoding", "gzip")
            try:
                with self._opener.open(req, timeout=45) as r:
                    data = r.read()
                    if r.headers.get("Content-Encoding") == "gzip":
                        import gzip
                        data = gzip.decompress(data)
                    return json.loads(data.decode("utf-8", "replace"))
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
                last_err = e
                wait = 2 ** attempt
                log.warning("请求失败(%s)，%ds 后重试: %s", type(e).__name__, wait, e)
                time.sleep(wait)
        raise RuntimeError(f"请求最终失败: {last_err}")

    def paged(self, key: str, **params):
        """自动跟随 continue 分页。"""
        cont: dict = {}
        while True:
            d = self.get(action="query", **params, **cont)
            yield from d.get("query", {}).get(key, [])
            if "continue" not in d:
                return
            cont = d["continue"]


def walk_category(wiki: Wiki, root: str, max_depth: int) -> list[str]:
    """广度优先遍历分类树，返回主命名空间页面标题。

    只取 ns=0：分类里混有 User blog、Forum、Template 等噪声命名空间。
    """
    pages: list[str] = []
    seen_pages: set[str] = set()
    visited_cats = {root.lower()}
    frontier = [root]

    for depth in range(max_depth + 1):
        if not frontier:
            break
        next_frontier: list[str] = []
        for cat in frontier:
            for m in wiki.paged("categorymembers", list="categorymembers",
                                cmtitle=cat, cmlimit=500, cmtype="page|subcat"):
                title = m["title"]
                if m.get("ns") == 14:
                    low = title.lower()
                    if low in visited_cats or low in SKIP_CATEGORIES:
                        continue
                    visited_cats.add(low)
                    next_frontier.append(title)
                elif m.get("ns") == 0 and title not in seen_pages:
                    seen_pages.add(title)
                    pages.append(title)
        log.info("  深度 %d：分类 %d 个，累计页面 %d 个",
                 depth, len(frontier), len(pages))
        frontier = next_frontier

    return pages


def collect_titles(wiki: Wiki, cat_depth: int) -> list[str]:
    """汇总待抓取页面标题。"""
    titles: list[str] = []
    seen: set[str] = set()

    def add(t: str) -> None:
        if t not in seen:
            seen.add(t)
            titles.append(t)

    log.info("枚举 %s 的嵌入页面……", MISSION_TEMPLATE)
    n = 0
    for m in wiki.paged("embeddedin", list="embeddedin",
                        eititle=MISSION_TEMPLATE, eilimit=500, einamespace=0):
        add(m["title"])
        n += 1
    log.info("  任务页 %d 个", n)

    if cat_depth >= 0:
        log.info("递归枚举 %s（深度 %d）……", ROOT_CATEGORY, cat_depth)
        before = len(titles)
        for t in walk_category(wiki, ROOT_CATEGORY, cat_depth):
            add(t)
        log.info("  分类新增 %d 个页面", len(titles) - before)

    for t in EXTRA_PAGES:
        add(t)
    for t in MAP_PAGES:
        add(t)
    log.info("  合计 %d 个待抓取", len(titles))
    return titles


def load_done() -> set[str]:
    """已抓取的标题，用于续传。"""
    if not PAGES_FILE.exists():
        return set()
    done = set()
    with PAGES_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                done.add(json.loads(line)["title"])
            except (json.JSONDecodeError, KeyError):
                continue
    return done


def fetch_batch(wiki: Wiki, titles: list[str]) -> list[dict]:
    """一次取多页 wikitext。API 上限 50 页/次。"""
    d = wiki.get(action="query", prop="revisions", rvprop="content|timestamp",
                 rvslots="main", titles="|".join(titles))

    out = []
    for page in d.get("query", {}).get("pages", []):
        title = page.get("title", "")
        if "missing" in page or "invalid" in page:
            log.warning("  页面不存在: %s", title)
            continue
        revs = page.get("revisions")
        if not revs:
            log.warning("  无修订内容: %s", title)
            continue
        slot = revs[0].get("slots", {}).get("main", {})
        text = slot.get("content")
        if not text:
            log.warning("  内容为空: %s", title)
            continue
        out.append({
            "title": title,
            "pageid": page.get("pageid"),
            "timestamp": revs[0].get("timestamp"),
            "wikitext": text,
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="抓取 GTA Wiki 的 SA 相关页面")
    ap.add_argument("--proxy", default=os.environ.get("SA_AGENT_PROXY", ""),
                    help="HTTP 代理，如 http://127.0.0.1:7897")
    ap.add_argument("--delay", type=float, default=0.5,
                    help="请求间隔秒数，默认 0.5（对 wiki 友好）")
    ap.add_argument("--limit", type=int, default=0,
                    help="只抓前 N 页，用于快速试跑")
    ap.add_argument("--cat-depth", type=int, default=2,
                    help="分类递归深度，默认 2；设为 -1 关闭分类枚举")
    ap.add_argument("--titles", default="",
                    help="逗号分隔的指定页面标题；用于补抓时跳过目录枚举")
    ap.add_argument("--dry-run", action="store_true",
                    help="只枚举页面清单并统计，不实际抓取")
    ap.add_argument("--refetch", action="store_true", help="忽略已有数据重新抓取")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    wiki = Wiki(args.proxy or None, args.delay)

    titles = ([title.strip() for title in args.titles.split(",")
               if title.strip()]
              if args.titles else collect_titles(wiki, args.cat_depth))
    if args.limit:
        titles = titles[: args.limit]

    done = set() if args.refetch else load_done()
    if done:
        log.info("已有 %d 页，续传剩余部分", len(done))
    todo = [t for t in titles if t not in done]

    if args.dry_run:
        log.info("dry-run：待抓取 %d 页（已有 %d 页）", len(todo), len(done))
        for t in todo[:30]:
            log.info("    %s", t)
        if len(todo) > 30:
            log.info("    ……另有 %d 页", len(todo) - 30)
        return 0

    if not todo:
        log.info("全部页面已抓取完毕，无需操作")
        return 0

    log.info("开始抓取 %d 页……", len(todo))
    mode = "w" if args.refetch else "a"
    saved = 0
    chars = 0
    with PAGES_FILE.open(mode, encoding="utf-8") as f:
        for i in range(0, len(todo), 50):
            batch = todo[i : i + 50]
            pages = fetch_batch(wiki, batch)
            for p in pages:
                f.write(json.dumps(p, ensure_ascii=False) + "\n")
                chars += len(p["wikitext"])
            f.flush()
            saved += len(pages)
            log.info("  进度 %d/%d  （已存 %d 页，%.1f 万字符）",
                     min(i + 50, len(todo)), len(todo), saved, chars / 10000)

    log.info("完成：新增 %d 页，共 %.1f 万字符 → %s", saved, chars / 10000, PAGES_FILE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
