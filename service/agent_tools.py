"""寻物意图和实时拾取物计算，只依赖本次请求的快照。"""
from __future__ import annotations

import logging
import math

log = logging.getLogger("agent_tools")

NEAREST_HINTS = ("最近", "附近", "哪里", "哪儿", "在哪", "带我", "标一下",
                 "标个", "我想要", "我要", "想找", "找一", "给我", "需要",
                 "补充", "补给", "去哪", "怎么找", "多远")
PICKUP_KEYWORDS = {
    "armor": ("防弹衣", "护甲", "盔甲", "armor", "armour"),
    "health": ("医疗包", "回血", "血包", "急救", "health"),
}
KIND_LABEL = {"armor": "防弹衣", "health": "医疗包", "weapon": "武器"}


def _norm_weapon(name: str) -> str:
    return name.lower().replace("-", "").replace(" ", "")


def parse_nearest_intent(question: str, pickups: list[dict]) -> tuple[str, str] | None:
    if not any(hint in question for hint in NEAREST_HINTS):
        return None
    q = question.lower()
    for kind, words in PICKUP_KEYWORDS.items():
        if any(word in q for word in words):
            return kind, ""

    names = {p.get("name", "") for p in pickups if p.get("kind") == "weapon"}
    for name in names:
        if name and _norm_weapon(name) in _norm_weapon(question):
            return "weapon", name
    if "武器" in question or "枪" in question or "弹药" in question:
        return "weapon", ""
    return None


def build_pickup_context(intent: tuple[str, str] | None,
                         state: dict | None,
                         pickups: list[dict]) -> tuple[str, str] | None:
    if not intent or not state or not pickups:
        return None
    kind, weapon_name = intent
    candidates = [p for p in pickups if p.get("kind") == kind]
    if kind == "weapon" and weapon_name:
        wanted = _norm_weapon(weapon_name)
        candidates = [p for p in candidates
                      if wanted in _norm_weapon(p.get("name", ""))]
    if not candidates:
        return None

    px, py = state.get("x", 0.0), state.get("y", 0.0)
    nearest = min(candidates,
                  key=lambda p: math.hypot(p["x"] - px, p["y"] - py))
    distance = math.hypot(nearest["x"] - px, nearest["y"] - py)
    dx, dy = nearest["x"] - px, nearest["y"] - py
    direction = _direction(dx, dy)
    label = nearest.get("name") or KIND_LABEL.get(kind, "目标")
    zone = nearest.get("zone") or "未知区域"
    payload = f"{nearest['x']:.1f},{nearest['y']:.1f},{nearest['z']:.1f}"
    clue = (f"游戏内实时扫描到，离玩家最近的{label}位于 {zone}，"
            f"在玩家{direction}方向约 {int(distance)} 米处，"
            "已自动标记到玩家的雷达上。")
    log.info("邻近线索: %s (%s)", clue, payload)
    return clue, payload


def _direction(dx: float, dy: float) -> str:
    if abs(dx) < 1e-6 and abs(dy) < 1e-6:
        return "就在脚下"
    directions = ("北", "东北", "东", "东南", "南", "西南", "西", "西北")
    degrees = math.degrees(math.atan2(dx, dy)) % 360.0
    return directions[int((degrees + 22.5) % 360.0 // 45)]
