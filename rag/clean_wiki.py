"""wikitext 清洗与切块。

Fandom 的 wikitext 里混杂大量对检索无用的内容：导航框、图库、
跨作品段落、剧情台词转录。直接切块会严重稀释检索质量，
所以这里按章节做白/黑名单过滤，并保留章节标题作为块的上下文。

产出 data/chunks.jsonl。
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

import mwparserfromhell

ROOT = Path(__file__).resolve().parent.parent
RAW_FILE = ROOT / "data" / "raw" / "pages.jsonl"
CHUNKS_FILE = ROOT / "data" / "chunks.jsonl"

log = logging.getLogger("clean")

# 整段丢弃的章节（对攻略问答无价值或会污染检索）
DROP_SECTIONS = {
    "gallery", "images", "screenshots", "artwork", "videos",
    "video walkthroughs", "video walkthrough",
    "references", "reference", "external links", "see also",
    "navigation", "navigations",
    "transcript", "transcripts", "dialogue", "dialogues",
    "post-mission phone call", "post-mission phone calls",
    "soundtrack", "music", "audio",
    "in other languages", "translations",
    "achievements and trophies",
}

# 其他作品专属章节。wiki 上载具/武器/角色页普遍按作品分章节
# （"Grand Theft Auto IV"、"GTA Online"……），这些段落对 SA 问答是纯噪声，
# 且会稀释检索。章节名同时提到 San Andreas 时保留。
OTHER_GAME_SECTION = re.compile(
    r"\b(gta|grand theft auto)\b.{0,12}\b("
    r"iii|iv|v|vi|2|1|online|vice city|liberty city|chinatown wars|"
    r"advance|london)\b"
    r"|^(events of|in) (gta|grand theft auto)\b"
    r"|\b(vice city|liberty city) stories\b"
    r"|\bepisodes from liberty city\b"
    r"|\bred dead\b",
    re.IGNORECASE,
)

# 高价值章节，切块时优先且允许更大长度
PRIORITY_SECTIONS = {
    "walkthrough", "mission", "mission objectives", "objectives",
    "strategy", "tips", "notes", "rewards", "reward",
    "aftermath", "trivia", "oversights", "bugs",
}

# 游戏名简写模板，必须展开为全名否则句子会缺主语
# （例："storyline missions in {{SA}}" → 删掉就成了 "missions in ."）
GAME_ABBREV = {
    "sa": "Grand Theft Auto: San Andreas",
    "gtasa": "Grand Theft Auto: San Andreas",
    "vc": "Grand Theft Auto: Vice City",
    "iii": "Grand Theft Auto III",
    "lcs": "GTA Liberty City Stories",
    "vcs": "GTA Vice City Stories",
    "iv": "Grand Theft Auto IV",
    "v": "Grand Theft Auto V",
    "cw": "GTA Chinatown Wars",
    "o": "GTA Online",
    "hd": "HD Universe",
    "tde": "The Definitive Edition",
}

# 无信息量的模板（直接删）
DROP_TEMPLATES = re.compile(
    r"^(under construction|stub|cleanup|expand|citation needed|cn|fact|vague|"
    r"spoiler|ref|reflist|navbox.*|navboxes|gtasa missions|infobox.*|"
    r"fake infobox|dialogue|scriptbox|hudicons|fandom|wp|wc|yt|url|"
    r"disambiglink|h:title|md|emdash|line|clr|clear|color|colour|"
    r"videogallery|help text|red dead|displaytitle:.*|#ev:.*|=)$",
    re.IGNORECASE,
)

# 保留内容但去掉包装的模板（取第一个有内容的参数）
KEEP_INNER_TEMPLATES = re.compile(
    r"^(main|see also|about|for|quote|small|s|v|games)$", re.IGNORECASE
)

# Infobox 参数里纯排版/图片用的键，提取时跳过
INFOBOX_SKIP_PARAMS = re.compile(
    r"image|caption|icon|style|logo|pic|photo|width|height|align|"
    r"color|colour|border|font|^name change$|^box|^header|^label|"
    # 引擎内部技术字段，对玩家问答无意义
    r"^roadspawn|^inttxd|^dashtype|^wheeltype|^flags$|^txd|^modelname|"
    r"^handling|^swankness|^audio|^anim|^lineup|^basevariant|^radar",
    re.IGNORECASE,
)


def extract_infobox(wikitext: str) -> str:
    """把 Infobox 的结构化参数抽成 "键: 值" 文本。

    载具的车身类型/载客数、武器的伤害/弹匣、任务的委托人/奖励都只存在于
    infobox 里，正文不会重复。原先 DROP_TEMPLATES 把 infobox 整块删掉，
    等于丢掉了最适合回答"这车/这枪什么参数"的数据。
    """
    code = mwparserfromhell.parse(wikitext)
    lines: list[str] = []

    for tpl in code.filter_templates(recursive=False):
        if not re.match(r"^infobox", str(tpl.name).strip(), re.IGNORECASE):
            continue
        for p in tpl.params:
            if not p.showkey:
                continue
            key = str(p.name).strip()
            if INFOBOX_SKIP_PARAMS.search(key):
                continue
            val = strip_wikitext(str(p.value)).strip()
            if not val or len(val) > 300:
                continue

            # 值常按作品分行（"$3,990,000 (GTA Online)"），逐行剔除他作品
            kept = [ln.strip() for ln in val.split("\n")
                    if ln.strip() and not is_other_game_section(ln)]
            if not kept:
                continue
            val = "; ".join(kept)

            key = key.replace("_", " ").strip().title()
            lines.append(f"{key}: {val}")

    return "\n".join(lines)



def strip_wikitext(text: str) -> str:
    """把 wikitext 转成干净的纯文本。"""
    code = mwparserfromhell.parse(text)

    # 删模板
    for tpl in list(code.filter_templates(recursive=True)):
        name = str(tpl.name).strip()
        low = name.lower()
        try:
            if low in GAME_ABBREV:
                code.replace(tpl, GAME_ABBREV[low])
            elif DROP_TEMPLATES.match(name):
                code.remove(tpl)
            elif KEEP_INNER_TEMPLATES.match(name):
                # 取最长的位置参数作为正文（模板首参常是正文）
                vals = [str(p.value).strip() for p in tpl.params
                        if not p.showkey]
                inner = max(vals, key=len) if vals else ""
                code.replace(tpl, " " + inner + " " if inner else " ")
            else:
                # 未知模板：保留最长位置参数，避免误删正文
                vals = [str(p.value).strip() for p in tpl.params
                        if not p.showkey]
                inner = max(vals, key=len) if vals else ""
                code.replace(tpl, " " + inner + " " if len(inner) > 15 else " ")
        except ValueError:
            # 已被父节点移除
            continue

    # 删注释
    for c in list(code.filter_comments(recursive=True)):
        try:
            code.remove(c)
        except ValueError:
            continue

    # 图片/分类/跨语言链接删掉，普通链接保留显示文字
    for link in list(code.filter_wikilinks(recursive=True)):
        title = str(link.title).strip()
        low = title.lower()
        try:
            if low.startswith(("file:", "image:", "category:", "media:")):
                code.remove(link)
            elif re.match(r"^[a-z]{2,3}:", low) and ":" in title:
                # 跨语言链接 [[de:...]]
                code.remove(link)
            else:
                shown = str(link.text) if link.text else title
                code.replace(link, shown)
        except ValueError:
            continue

    # 删 ref 标签内容
    for tag in list(code.filter_tags(recursive=True)):
        try:
            if str(tag.tag).lower() in ("ref", "gallery", "imagemap"):
                code.remove(tag)
        except ValueError:
            continue

    out = str(code)
    out = flatten_tables(out)

    # 残留标记清理
    out = re.sub(r"'{2,5}", "", out)                    # 粗斜体
    out = re.sub(r"<[^>]+>", " ", out)                  # 残留 HTML
    # 迭代删残留模板：{{a|{{b}}}} 这类嵌套一次替换清不干净
    for _ in range(5):
        new = re.sub(r"\{\{[^{}]*\}\}", " ", out)
        if new == out:
            break
        out = new
    out = re.sub(r"\{\{[^\n]{0,300}?\}\}", " ", out)    # 兜底：单行未闭合残留
    out = re.sub(r"\[\[[^\[\]]*\]\]", " ", out)         # 残留链接
    out = re.sub(r"^[*#:;]+\s*", "", out, flags=re.MULTILINE)  # 列表符号
    out = re.sub(r"&nbsp;?", " ", out)
    out = re.sub(r"&[a-z]+;", " ", out)
    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def flatten_tables(text: str) -> str:
    """把 wikitable 转成可读文本行。

    表格里是任务列表、奖励数值这类高价值结构化数据，不能直接删；
    但 `{| class="wikitable" ! |- |` 这些标记会污染检索，
    所以按行提取单元格，用 " | " 连接成自然文本。
    """
    lines = text.split("\n")
    out: list[str] = []
    depth = 0
    row: list[str] = []

    def flush_row() -> None:
        nonlocal row
        cells = [c.strip(" |!") for c in row if c.strip(" |!")]
        if cells:
            out.append(" | ".join(cells))
        row = []

    for line in lines:
        s = line.strip()

        if s.startswith("{|"):
            depth += 1
            continue
        if s.startswith("|}"):
            flush_row()
            depth = max(0, depth - 1)
            continue

        if depth == 0:
            out.append(line)
            continue

        # 表格内部
        if s.startswith("|-"):
            flush_row()
            continue
        if s.startswith("|+"):          # 表格标题
            cap = s[2:].strip()
            if cap:
                out.append(cap)
            continue
        if s.startswith("!") or s.startswith("|"):
            body = s[1:]
            # 同行多单元格：!! 或 ||
            parts = re.split(r"\|\||!!", body)
            for p in parts:
                # 去掉单元格属性（如 rowspan="3" | 内容）
                if "|" in p and re.match(r"^[^|]{0,60}=[^|]{0,60}\|", p):
                    p = p.split("|", 1)[1]
                p = p.strip()
                if p:
                    row.append(p)
            continue

        # 表格内的续行文本
        if s:
            row.append(s)

    flush_row()
    return "\n".join(out)


def split_sections(wikitext: str) -> list[tuple[str, str]]:
    """按 == 标题 == 切分，返回 (章节名, 内容)。开头无标题的部分归为 Intro。"""
    parts: list[tuple[str, str]] = []
    pattern = re.compile(r"^(={2,6})\s*(.+?)\s*\1\s*$", re.MULTILINE)

    matches = list(pattern.finditer(wikitext))
    if not matches:
        return [("Intro", wikitext)]

    head = wikitext[: matches[0].start()].strip()
    if head:
        parts.append(("Intro", head))

    for i, m in enumerate(matches):
        name = clean_heading(m.group(2))
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(wikitext)
        parts.append((name, wikitext[start:end].strip()))
    return parts


def clean_heading(raw: str) -> str:
    """章节标题也是 wikitext，含链接、图标和格式标记，需要清掉。
    标题会拼进索引文本，不清洗会污染检索。"""
    s = raw.strip()
    s = re.sub(r"\{\{[^{}]*\}\}", "", s)

    # 反复剥离链接，处理嵌套如 [[File:icon.png|[[Sweet Johnson|Sweet]]]]
    for _ in range(5):
        before = s
        # 图片/文件链接：整个删掉（含其内部的嵌套链接）
        s = re.sub(r"\[\[\s*(?:File|Image|Media)\s*:[^\[\]]*\]\]", " ", s,
                   flags=re.IGNORECASE)
        # 普通链接：取显示文字
        s = re.sub(r"\[\[(?:[^\[\]|]*\|)?([^\[\]|]*)\]\]", r"\1", s)
        if s == before:
            break

    # 仍残留的 File: 前缀片段（嵌套被剥开后暴露出来的）
    s = re.sub(r"(?:File|Image|Media)\s*:\s*\S+?\.(?:png|jpg|jpeg|gif|svg)",
               " ", s, flags=re.IGNORECASE)
    s = re.sub(r"[\[\]|]+", " ", s)
    s = re.sub(r"'{2,5}", "", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip(" -–—:") or "Section"


def chunk_text(text: str, target: int, overlap: int) -> list[str]:
    """按段落边界切块，尽量不切断句子。"""
    if len(text) <= target:
        return [text] if text.strip() else []

    chunks: list[str] = []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]

    cur = ""
    for para in paragraphs:
        # 单段就超长：按句子再切
        if len(para) > target:
            if cur:
                chunks.append(cur)
                cur = ""
            sentences = re.split(r"(?<=[.!?])\s+", para)
            buf = ""
            for s in sentences:
                if len(buf) + len(s) + 1 > target and buf:
                    chunks.append(buf.strip())
                    buf = buf[-overlap:] if overlap else ""
                buf += " " + s
            if buf.strip():
                cur = buf.strip()
            continue

        if len(cur) + len(para) + 2 > target and cur:
            chunks.append(cur)
            # 重叠：保留上一块尾部，缓解边界处语义割裂
            cur = (cur[-overlap:] + "\n\n" + para) if overlap else para
        else:
            cur = (cur + "\n\n" + para) if cur else para

    if cur.strip():
        chunks.append(cur.strip())
    return chunks


def is_other_game_section(section: str) -> bool:
    """章节是否专讲其他 GTA 作品。提到 San Andreas 的一律保留。"""
    low = section.lower()
    if "san andreas" in low or "sa" == low.strip():
        return False
    return bool(OTHER_GAME_SECTION.search(section))


def is_sa_page(title: str, wikitext: str) -> bool:
    """排除与 SA 无关的页面（wiki 上大量页面跨多作品）。"""
    # Shared pages often place the SA-specific section after a long infobox or
    # a section for another game, so inspecting only the opening text loses
    # useful entries such as Carl Johnson and shared vehicles.
    return ("San Andreas" in wikitext or "GTA SA" in wikitext
            or "San Andreas" in title)


def extract_map_markers(title: str, wikitext: str) -> list[str] | None:
    """Turn Fandom Map namespace JSON into individually retrievable markers."""
    if not title.startswith("Map:"):
        return None
    try:
        data = json.loads(wikitext)
    except json.JSONDecodeError:
        return None

    markers = data.get("markers")
    if not isinstance(markers, list):
        return None

    out: list[str] = []
    for marker in markers:
        if not isinstance(marker, dict):
            continue
        popup = marker.get("popup") or {}
        name = str(popup.get("title") or marker.get("title") or "Location")
        description = str(popup.get("description")
                          or marker.get("description") or "")
        description = strip_wikitext(description).replace("\n", " ").strip()
        position = marker.get("position") or []
        coordinates = ""
        if isinstance(position, list) and len(position) >= 2:
            coordinates = f" Coordinates: {position[0]:.0f}, {position[1]:.0f}."
        if description:
            out.append(f"{title} {name}. {description}.{coordinates}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="清洗 wikitext 并切块")
    ap.add_argument("--target", type=int, default=900,
                    help="目标块大小（字符），默认 900")
    ap.add_argument("--overlap", type=int, default=120,
                    help="块间重叠字符数，默认 120")
    ap.add_argument("--min-chars", type=int, default=80,
                    help="小于此长度的块丢弃，默认 80")
    ap.add_argument("--sample", type=int, default=0,
                    help="打印前 N 个块用于人工检查")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s  %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")

    if not RAW_FILE.exists():
        log.error("找不到 %s，请先运行 fetch_wiki.py", RAW_FILE)
        return 1

    pages = []
    with RAW_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                pages.append(json.loads(line))
    log.info("读入 %d 页", len(pages))

    chunks: list[dict] = []
    stats = {"skipped_page": 0, "dropped_section": 0, "other_game": 0,
             "short": 0, "infobox": 0, "map_marker": 0}

    for page in pages:
        title = page["title"]
        wikitext = page["wikitext"]

        markers = extract_map_markers(title, wikitext)
        if markers is not None:
            for index, text in enumerate(markers, 1):
                chunks.append({
                    "id": f"{page['pageid']}-map-{index}",
                    "title": title,
                    "section": "Locations",
                    "text": text,
                    "priority": True,
                    "url": "https://gta.fandom.com/wiki/"
                           + title.replace(" ", "_"),
                })
            stats["map_marker"] += len(markers)
            continue

        if not is_sa_page(title, wikitext):
            stats["skipped_page"] += 1
            continue

        # Infobox 单独成块：字段密集，混进正文块会被长段落淹没
        info = extract_infobox(wikitext)
        if info:
            stats["infobox"] += 1
            chunks.append({
                "id": f"{page['pageid']}-info",
                "title": title,
                "section": "Infobox",
                "text": f"{title} 基本资料\n{info}",
                "priority": True,
                "url": "https://gta.fandom.com/wiki/"
                       + title.replace(" ", "_"),
            })

        for section, body in split_sections(wikitext):
            if section.strip().lower() in DROP_SECTIONS:
                stats["dropped_section"] += 1
                continue

            if is_other_game_section(section):
                stats["other_game"] += 1
                continue

            clean = strip_wikitext(body)
            if not clean:
                continue

            for piece in chunk_text(clean, args.target, args.overlap):
                if len(piece) < args.min_chars:
                    stats["short"] += 1
                    continue
                chunks.append({
                    "id": f"{page['pageid']}-{len(chunks)}",
                    "title": title,
                    "section": section,
                    "text": piece,
                    "priority": section.strip().lower() in PRIORITY_SECTIONS,
                    "url": "https://gta.fandom.com/wiki/"
                           + title.replace(" ", "_"),
                })

    CHUNKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with CHUNKS_FILE.open("w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    total = sum(len(c["text"]) for c in chunks)
    prio = sum(1 for c in chunks if c["priority"])
    log.info("产出 %d 块（高价值 %d），共 %.1f 万字符，平均 %.0f 字符/块",
             len(chunks), prio, total / 10000, total / max(len(chunks), 1))
    log.info("跳过：非 SA 页 %d，无用章节 %d，他作品章节 %d，过短块 %d",
             stats["skipped_page"], stats["dropped_section"],
             stats["other_game"], stats["short"])
    log.info("提取 infobox %d 页", stats["infobox"])
    log.info("提取地图标记 %d 个", stats["map_marker"])
    log.info("→ %s", CHUNKS_FILE)

    for c in chunks[: args.sample]:
        print(f"\n{'=' * 70}")
        print(f"[{c['title']}] §{c['section']}  prio={c['priority']}  {len(c['text'])} 字符")
        print(c["text"][:500])

    return 0


if __name__ == "__main__":
    sys.exit(main())
