"""RAG 问答管线：查询改写 → 检索 → DeepSeek 生成。

查询改写这一步是必需的：语料是英文 wiki，玩家提问是中文口语
（"火车那关跟不上怎么办"），纯 BM25 字面匹配召回率极低。
先让模型把问题转成英文检索词，再喂给 BM25。
"""
from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path

from .llm_client import LLMClient
from .missions import mission_name
from .retriever import Retriever

log = logging.getLogger("pipeline")

ROOT = Path(__file__).resolve().parent.parent
REWRITE_SYS = """你是 GTA 圣安地列斯攻略助手的检索模块。
把玩家的中文问题转成英文检索关键词，用于在英文 GTA Wiki 中检索。

规则：
- 只输出关键词，空格分隔，不要解释、不要标点、不要换行
- 玩家描述的任务要给出该任务的英文原名。例："火车那关"→"Wrong Side of the Tracks train"
- 人物、地点、载具用游戏内英文原名
- 补充相关英文术语。例："打不过"→"strategy tips difficult"
- 问题可能来自语音识别，专名常有同音错字。按发音还原成游戏里真实存在的名字，
  再给英文原名。例："封狗多克的月铺"→"疯狗多克的乐谱"→"Madd Dogg's Rhymes"
- 最多 12 个词

示例：
问题：火车那关跟不上怎么办
输出：Wrong Side of the Tracks train Big Smoke Sanchez motorbike strategy tips

问题：封狗多克的月铺怎么拿
输出：Madd Dogg's Rhymes rhyme book OG Loc mission walkthrough

问题：怎么提高肌肉
输出：Muscle stat gym workout increase Stamina"""

ANSWER_SYS = """你是《GTA：圣安地列斯》的游戏内攻略助手。

你的口吻：
- 像 Los Santos 的老玩家，带一点 Grove Street / West Coast 街头感，直接、松弛、有兄弟式的现场感
- 可以自然使用少量中文口语和 GTA 语境里的短俚语，例如“这关”“老哥”“别硬刚”“先稳住”；每次最多 1-2 处，不要连续堆黑话
- 不要模仿现实族群口音，不要使用歧视性称呼，不要为了耍酷加入资料没有的剧情或脏话

回答规则：
- 用简体中文，先给结论，再给最少但可执行的步骤；不要寒暄、复述问题或写长篇背景
- 严格依据提供的资料回答。资料没有的内容，明确说“资料里没有”，不要编造
- 任务名、人名、地名保留英文原名，可在括号内附常见中文叫法
- 回答控制在 200 字内，适合玩家边玩边看；必要时用短项目符号
- 如果是问“怎么过关”，优先给操作顺序、目标、关键风险和奖励
- 只在确实有帮助时使用一句收尾，例如“稳着来，别急着硬冲。”"""

# 语音播报专用。念出来的内容和看的不一样：没法回读，项目符号和括号注释
# 念出来只会变成噪音，玩家还在开车分不出注意力听长句。
VOICE_ANSWER_SYS = """你是《GTA：圣安地列斯》的游戏内语音助手，回答会被念给正在游戏的玩家听。

你的口吻：
- 像旁边坐着的老玩家，直接、松弛，说人话
- 偶尔一处口语即可，例如“这关”“别硬刚”；不要堆黑话

回答规则（这些是硬性要求）：
- 用简体中文，最多三句话，控制在 80 字内
- 只说最关键的那一点。玩家在开车，听不进步骤清单
- 纯口语，不要项目符号、不要序号、不要括号注释、不要 Markdown
- 任务名、人名用玩家听得懂的中文叫法，不要念英文原名
- 资料里没有就直接说“这个资料里没有”，别编
- 玩家想知道细节，会自己呼出面板看，不用在语音里交代这句"""


def describe_state(state: dict | None) -> str:
    """把玩家状态 JSON 转成给模型看的中文描述。无状态时返回空串。"""
    if not state:
        return ""

    parts: list[str] = []

    hp = state.get("health")
    if hp is not None:
        line = f"血量 {hp:.0f}/{state.get('max_health', 100):.0f}"
        armour = state.get("armour") or 0
        line += f"，护甲 {armour:.0f}" if armour else "，无护甲"
        parts.append(line)

    weapon = state.get("weapon")
    if weapon and weapon != "拳头":
        ammo = state.get("ammo_total") or 0
        parts.append(f"手持{weapon}（备弹 {ammo}）" if ammo else f"手持{weapon}")
    elif weapon:
        parts.append("赤手空拳")

    parts.append("在载具内" if state.get("in_vehicle") else "步行中")

    if state.get("on_mission"):
        name = mission_name(state.get("mission_script"))
        # 认不出具体是哪一关时只说"在任务中"，不把内部代号抛给模型——
        # 它会拿代号硬编一个任务名出来。
        parts.append(f"正在进行任务《{name}》" if name else "正在进行任务")

    wanted = state.get("wanted") or 0
    if wanted:
        parts.append(f"通缉 {wanted} 星")

    money = state.get("money")
    if money is not None:
        parts.append(f"现金 ${money}")

    x, y = state.get("x"), state.get("y")
    zone, city = state.get("zone"), state.get("city")
    if zone or city:
        where = zone or ""
        if city and city != zone:
            where = f"{where}（{city}）" if where else city
        parts.append(f"位于 {where}")
    elif x is not None and y is not None:
        # 落在所有 zone 之外（海面、边界），只能报坐标
        parts.append(f"坐标 ({x:.0f}, {y:.0f}, {state.get('z', 0):.0f})")

    muscle, fat, stamina = (state.get("muscle"), state.get("fat"),
                            state.get("stamina"))
    if muscle is not None:
        parts.append(f"肌肉 {muscle:.0f}/1000，肥胖 {fat:.0f}/1000，"
                     f"耐力 {stamina:.0f}/1000")

    # 游戏进度
    progress = state.get("progress")
    if progress is not None:
        parts.append(f"游戏完成度 {progress:.1f}%")

    # 行驶距离统计（已转换为公里）
    dist_car = state.get("distance_by_car_km")
    dist_bike = state.get("distance_by_bike_km")
    if dist_car is not None and dist_car > 0.1:
        parts.append(f"累计开车 {dist_car:.1f} 公里")
    if dist_bike is not None and dist_bike > 0.1:
        parts.append(f"累计骑行 {dist_bike:.1f} 公里")

    # 游戏统计
    missions = state.get("missions_passed")
    days = state.get("days_passed")
    if missions is not None and missions > 0:
        parts.append(f"已完成 {missions} 个任务")
    if days is not None and days > 0:
        parts.append(f"已游玩 {days} 天")

    return "；".join(parts)


class Pipeline:
    def __init__(self, top_k: int = 5, rewrite: bool = True,
                 retriever: Retriever | None = None,
                 llm_client: LLMClient | None = None):
        self.retriever = retriever or Retriever()
        self.llm = llm_client or LLMClient()
        self.top_k = top_k
        self.rewrite = rewrite

    def rewrite_query(self, question: str, state: dict | None = None) -> list[str]:
        """把中文口语问题转成英文检索词。失败时返回空列表，退回纯 BM25。"""
        if not self.rewrite:
            return []
        try:
            # 带上状态，让"这关怎么打"这类指代性提问能被改写成具体检索词
            prompt = f"问题：{question}\n"
            desc = describe_state(state)
            if desc:
                prompt += f"玩家当前状态：{desc}\n"
            prompt += "输出："

            terms = self.llm.rewrite_query(prompt, REWRITE_SYS)
            log.info("查询改写: %s", " ".join(terms))
            return terms
        except Exception as e:
            log.warning("查询改写失败（退回纯 BM25）: %s", e)
            return []

    def build_context(self, hits: list[dict]) -> str:
        parts = []
        for i, h in enumerate(hits, 1):
            parts.append(
                f"【资料{i}】{h['title']} — {h['section']}\n{h['text']}"
            )
        return "\n\n".join(parts)

    def answer(self, question: str, state: dict | None = None,
               voice: bool = False) -> Iterator[str]:
        """流式产出回答片段。

        state 是覆盖层读来的玩家实时状态（可为 None）。它同时用于查询改写
        和作答上下文，让"这关怎么打"这类指代性提问能落到具体内容上。

        voice=True 时换用语音专用提示词并压低长度上限：回答会被念出来，
        长答案和项目符号在朗读场景下都不可用。
        """
        terms = self.rewrite_query(question, state)
        hits = self.retriever.search(question, top_k=self.top_k,
                                     extra_terms=terms)

        if not hits:
            yield "资料库里没找到相关内容，换个说法试试？"
            return

        log.info("检索命中 %d 块: %s", len(hits),
                 " / ".join(f"{h['title']}§{h['section']}" for h in hits))

        user = f"玩家问题：{question}\n\n"

        desc = describe_state(state)
        if desc:
            user += f"玩家当前游戏内状态：{desc}\n\n"

        user += (
            f"以下是从 GTA Wiki 检索到的资料：\n\n{self.build_context(hits)}\n\n"
            f"请依据资料回答玩家问题。"
        )
        if desc:
            user += "若状态与问题相关（如血量偏低、缺少弹药、属性不足），可在回答中一并提醒。"

        yield from self.llm.stream_answer(
            user, VOICE_ANSWER_SYS if voice else ANSWER_SYS, voice=voice
        )


def load_env() -> None:
    """读取 sa-agent/.env（不覆盖已有环境变量）。"""
    f = ROOT / ".env"
    if not f.exists():
        return
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(levelname)-7s %(message)s")
    load_env()

    p = Pipeline()

    questions = sys.argv[1:] or [
        "火车那关跟不上怎么办",
        "CJ的姐姐是谁",
        "怎么提高肌肉",
        "黑帮战争怎么打",
    ]

    for q in questions:
        print(f"\n{'=' * 72}\n问: {q}\n{'-' * 72}")
        for piece in p.answer(q):
            print(piece, end="", flush=True)
        print()
