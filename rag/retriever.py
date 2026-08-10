"""混合检索：BM25 + 专有名词加权。

语料是英文，玩家提问多为中文，直接 BM25 会因语言不通召回率极低。
这里的做法：
  1. 建立中英专有名词映射表（CJ、大烟哥、条子等 → 英文原名）
  2. 查询扩展：把中文问题里的实体替换/补充为英文
  3. BM25 检索 + 章节优先级加权

暂不引入向量模型（torch 约 2.5GB），先看 BM25 的实际效果。
"""
from __future__ import annotations

import json
import logging
import math
import re
from pathlib import Path

from rank_bm25 import BM25Okapi

ROOT = Path(__file__).resolve().parent.parent
CHUNKS_FILE = ROOT / "data" / "chunks.jsonl"
# 手写补充语料。clean_wiki.py 会覆盖 chunks.jsonl，所以人工内容单独放一个文件。
CUSTOM_FILE = ROOT / "data" / "custom.jsonl"

# RRF 融合的平滑常数，文献典型值
RRF_K = 60

log = logging.getLogger("retriever")

# 中文/俗称 → 英文 wiki 用词。玩家提问基本绕不开这些实体。
ALIASES: dict[str, list[str]] = {
    # 人物
    "cj": ["Carl Johnson", "CJ"],
    "卡尔": ["Carl Johnson", "CJ"],
    "大哥": ["Sweet Johnson"],
    "甜哥": ["Sweet Johnson"],
    "斯威特": ["Sweet Johnson"],
    "妹妹": ["Kendl Johnson"],
    "姐姐": ["Kendl Johnson"],
    "肯德尔": ["Kendl Johnson"],
    "大烟": ["Big Smoke"],
    "大烟哥": ["Big Smoke"],
    "烟哥": ["Big Smoke"],
    "莱德": ["Ryder"],
    "赖德": ["Ryder"],
    "凯撒": ["Cesar Vialpando"],
    "武藏": ["Wu Zi Mu", "Woozie"],
    "吴仔": ["Wu Zi Mu", "Woozie"],
    "托雷诺": ["Mike Toreno"],
    "真理": ["The Truth"],
    "催化剂": ["Catalina"],
    "卡塔琳娜": ["Catalina"],
    "警察": ["Tenpenny", "police", "C.R.A.S.H."],
    "条子": ["Tenpenny", "C.R.A.S.H.", "police"],
    "坦佩尼": ["Frank Tenpenny"],
    "普拉斯基": ["Eddie Pulaski"],
    "疯狗": ["Madd Dogg"],
    "疯狗多克": ["Madd Dogg"],
    "麦德多格": ["Madd Dogg"],
    # 语音识别常把"疯狗"听成同音字，这里一并兜住，省得玩家白说一遍
    "封狗": ["Madd Dogg"],
    "风狗": ["Madd Dogg"],
    "乐谱": ["Madd Dogg's Rhymes", "rhyme book"],
    "月铺": ["Madd Dogg's Rhymes", "rhyme book"],
    "歌词本": ["Madd Dogg's Rhymes", "rhyme book"],
    # 地名
    "洛圣都": ["Los Santos"],
    "圣菲罗": ["San Fierro"],
    "圣辉洛": ["San Fierro"],
    "拉斯云图": ["Las Venturas"],
    "赌城": ["Las Venturas"],
    "格罗夫街": ["Grove Street"],
    "农场": ["Angel Pine", "countryside", "Red County"],
    # 帮派
    "帮派": ["gang", "Grove Street Families", "Ballas"],
    "巴拉斯": ["Ballas"],
    "瓦格斯": ["Vagos"],
    "三合会": ["Triads"],
    "黑帮战争": ["Gang Wars", "gang territory"],
    # 玩法
    "任务": ["mission"],
    "支线": ["side mission"],
    "主线": ["storyline mission"],
    "奖励": ["reward"],
    "武器": ["weapon"],
    "车": ["vehicle", "car"],
    "飞机": ["aircraft", "plane"],
    "摩托": ["motorcycle", "bike"],
    "女友": ["girlfriend"],
    "约会": ["girlfriend", "date"],
    "健身": ["Muscle", "gym", "Stamina"],
    "体力": ["Stamina"],
    "肌肉": ["Muscle"],
    "肺活量": ["Lung Capacity"],
    "驾校": ["Driving School"],
    "飞行学校": ["Pilot School", "flight school"],
    "船校": ["Boat School"],
    "车校": ["Driving School"],
    "秘籍": ["cheat"],
    "作弊": ["cheat"],
    "全完成": ["100% Completion"],
    "百分百": ["100% Completion"],
    "牡蛎": ["Oysters"],
    "马蹄铁": ["Horseshoes"],
    "涂鸦": ["Tags", "graffiti"],
    "照片": ["Snapshots"],
    "特技跳跃": ["Stunt Jumps"],
    "安全屋": ["safehouse"],
    "存档": ["save", "safehouse"],
    "通缉": ["wanted level"],
    "怎么过": ["walkthrough", "objectives", "strategy"],
    "攻略": ["walkthrough", "objectives", "strategy"],
    "打不过": ["strategy", "tips", "difficult"],
    "技巧": ["tips", "strategy"],
    "漏洞": ["oversight", "glitch", "bug"],
    "彩蛋": ["trivia", "easter egg"],
}


def tokenize(text: str) -> list[str]:
    """英文按词切分并小写；保留数字。中文按字切（便于别名命中）。"""
    text = text.lower()
    tokens = re.findall(r"[a-z][a-z'\-]*|\d+", text)
    # 中文逐字，作为兜底信号
    tokens += re.findall(r"[\u4e00-\u9fff]", text)
    return tokens


def expand_query(question: str) -> tuple[list[str], list[str]]:
    """把中文问题扩展出英文检索词。

    返回 (检索用 token, 命中的别名英文词) —— 后者用于加权。
    """
    q = question.lower()
    extra: list[str] = []

    for cn, ens in ALIASES.items():
        if cn in q:
            extra.extend(ens)

    # 原文 token + 扩展词 token
    tokens = tokenize(question)
    for e in extra:
        tokens.extend(tokenize(e))
    return tokens, extra


def _rrf_fuse(list_a: list[int], list_b: list[int],
              k: int = RRF_K) -> list[tuple[int, float]]:
    """RRF（Reciprocal Rank Fusion）融合两个排序列表。

    rrf_score(item) = sum( 1 / (k + rank_in_list_i) )

    k=60 是文献里的典型值。排名从 1 开始计，所以首位得分 1/61，
    第二位 1/62，以此类推。两个列表的得分求和后重新排序。

    量纲无关：只看排名不看原始分数，天然适合融合 BM25（无上界）
    和余弦相似度（[-1, 1]）这种不可比的异构分数。

    返回 [(块下标, 融合分数)]，按分数降序。
    """
    scores: dict[int, float] = {}
    for i, idx in enumerate(list_a, 1):
        scores[idx] = scores.get(idx, 0.0) + 1.0 / (k + i)
    for i, idx in enumerate(list_b, 1):
        scores[idx] = scores.get(idx, 0.0) + 1.0 / (k + i)

    return sorted(scores.items(), key=lambda kv: -kv[1])


def _load_custom(chunks: list[dict], custom_file: Path) -> int:
    """加载手写补充语料。文件不存在时静默跳过。

    只要求 title / text 两个字段，其余给默认值，方便手工维护。
    单行格式错误只跳过该行并告警，不影响服务启动。
    """
    if not custom_file.exists():
        return 0

    n = 0
    with custom_file.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            try:
                c = json.loads(line)
                title = c["title"].strip()
                text = c["text"].strip()
            except (json.JSONDecodeError, KeyError, AttributeError) as e:
                log.warning("custom.jsonl 第 %d 行已跳过：%s", lineno, e)
                continue
            if not title or not text:
                log.warning("custom.jsonl 第 %d 行已跳过：title/text 为空", lineno)
                continue

            n += 1
            chunks.append({
                "id": c.get("id") or f"custom-{n}",
                "title": title,
                "section": c.get("section") or "Notes",
                "text": text,
                "priority": bool(c.get("priority", True)),
                "url": c.get("url", ""),
            })
    return n


def load_chunks(chunks_file: Path = CHUNKS_FILE,
                custom_file: Path = CUSTOM_FILE) -> list[dict]:
    """读取全部语料。

    抽成模块级函数是为了让 embed.py 复用——向量下标按列表位置对应块，
    两边的加载顺序必须完全一致，否则检索会取到错误的块。
    """
    if not chunks_file.exists():
        raise FileNotFoundError(
            f"找不到语料 {chunks_file}，请先运行 rag/fetch_wiki.py 与 rag/clean_wiki.py"
        )

    chunks: list[dict] = []
    with chunks_file.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                chunks.append(json.loads(line))

    _load_custom(chunks, custom_file)
    return chunks


class Retriever:
    def __init__(self, chunks_file: Path = CHUNKS_FILE,
                 custom_file: Path = CUSTOM_FILE, use_vectors: bool = True):
        self.chunks = load_chunks(chunks_file, custom_file)

        # 索引文本 = 标题 + 章节 + 正文。标题重复三次以提升实体权重，
        # 因为玩家提问几乎总是带任务名/人名。
        corpus = []
        for c in self.chunks:
            head = f"{c['title']} {c['title']} {c['title']} {c['section']}"
            corpus.append(tokenize(head + " " + c["text"]))

        self.bm25 = BM25Okapi(corpus)

        # 向量索引是可选增强：没有预构建的索引就退回纯 BM25，
        # 不阻塞服务启动（构建一次要几分钟）。
        self.vectors = None
        if use_vectors:
            from .embed import VectorIndex

            self.vectors = VectorIndex.load(self.chunks)

        log.info("检索索引就绪：%d 块，向量 %s", len(self.chunks),
                 "已启用" if self.vectors else "未启用（纯 BM25）")

    def _bm25_ranking(self, question: str,
                      extra_terms: list[str] | None) -> tuple[list[int], list[str]]:
        """BM25 打分并加权，返回按分数降序的块下标。"""
        tokens, aliases = expand_query(question)

        if extra_terms:
            for t in extra_terms:
                tokens.extend(tokenize(t))
            aliases = aliases + extra_terms

        if not tokens:
            return [], aliases

        scores = self.bm25.get_scores(tokens)

        # 加权：高价值章节 + 标题命中别名
        alias_low = [a.lower() for a in aliases]
        ranked = []
        for i, base in enumerate(scores):
            if base <= 0:
                continue
            c = self.chunks[i]
            score = float(base)

            if c["priority"]:
                score *= 1.25

            title_low = c["title"].lower()
            if any(a in title_low for a in alias_low):
                score *= 1.6

            ranked.append((score, i))

        ranked.sort(key=lambda x: -x[0])
        return [i for _, i in ranked], aliases

    def search(self, question: str, top_k: int = 5,
               extra_terms: list[str] | None = None) -> list[dict]:
        """混合检索。extra_terms 为可选的额外检索词（如 LLM 改写产出的英文关键词）。

        BM25 与向量各自出一个排名，再用 RRF（Reciprocal Rank Fusion）合并。
        用排名而非分数融合，是因为两者量纲不可比：BM25 分数无上界且随查询长度
        漂移，余弦相似度恒在 [-1, 1]，加权求和需要针对查询反复调参。
        """
        bm25_order, _ = self._bm25_ranking(question, extra_terms)

        if self.vectors is None:
            fused = [(i, 1.0 / (RRF_K + rank))
                     for rank, i in enumerate(bm25_order, 1)]
        else:
            # 候选池取得比 top_k 大，给融合留出重排空间
            pool = max(top_k * 8, 40)
            vec_order = [i for i, _ in self.vectors.search(question, pool)]
            fused = _rrf_fuse(bm25_order[:pool], vec_order)

        out = []
        seen_titles: dict[str, int] = {}
        for i, score in fused:
            c = self.chunks[i]
            # 同一页面最多取 2 块，保证结果多样性
            n = seen_titles.get(c["title"], 0)
            if n >= 2:
                continue
            seen_titles[c["title"]] = n + 1
            out.append({**c, "score": round(score, 5)})
            if len(out) >= top_k:
                break
        return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    r = Retriever()

    TESTS = [
        "CJ的姐姐是谁",
        "Wrong Side of the Tracks 这关怎么过",
        "火车那关跟不上怎么办",
        "怎么提高肌肉",
        "黑帮战争怎么打",
        "大烟哥是谁",
        "驾校金牌怎么拿",
        "100%完成度需要做什么",
    ]

    for q in TESTS:
        print(f"\n{'=' * 72}\n问: {q}")
        for hit in r.search(q, top_k=3):
            print(f"  [{hit['score']:6.2f}] {hit['title']} §{hit['section']}")
            print(f"          {hit['text'][:150].strip()}")
