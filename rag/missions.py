"""main.scm 脚本代号 → 任务正式英文名。

用途：把内存里读到的任务脚本代号（如 "music2"）翻译成 wiki 上的任务名，
既用于检索改写，也用于回答时向玩家提及。

数据来源：
  - 主线与支线任务的对应关系来自 mission_titles.SCRIPT_TITLES，由
    tools/gen_mission_titles.py 从 main.scm 字节码 + american.gxt 提取：
    任务标题的 GXT key 出现在该任务脚本自己的区段内，属于实据，不是猜测。
  - 少数活动脚本（驾校、打工支线、小游戏）在 SCM 里不声明标题键，
    只能按脚本代号手工对应，列在 ACTIVITIES 中，每条都已与语料页面核对。

宁可回答"任务未识别"，也不能给模型一个错误的任务名——
那会让它自信地讲解错误的关卡。
"""

from .mission_titles import SCRIPT_TITLES

# SCM 里的标题写法与 wiki 页面标题偶有出入（大小写、连字符、别名）。
# 这里统一成语料中真实存在的页面标题，否则检索改写会拿到语料里没有的词。
WIKI_ALIAS: dict[str, str] = {
    "A Home In The Hills": "A Home in the Hills",
    "Amphibious Assault": "Amphibious Assault (GTA San Andreas)",
    "Back to School": "Driving School",        # dskool，游戏内标题是 Back to School
    "Blood Ring": "Blood Ring (GTA San Andreas)",
    "Drive-thru": "Drive-Thru",
    "End Of The Line": "End of the Line",
    "Inside Track Betting": "Inside Track",
    "Jizzy": "Jizzy (mission)",
    "Key to her Heart": "Key to Her Heart",
    "Lure": "Lure (GTA San Andreas)",
    "Madd Dogg": "Madd Dogg (mission)",
    "Monster": "Monster (mission)",
    "OG Loc": "OG Loc (mission)",
    "Quarry": "Quarry Missions",
    "Riot": "Riot (GTA San Andreas)",
    "Tagging up Turf": "Tagging Up Turf",
    "Verdant Meadows": "Verdant Meadows (mission)",
    "Wear Flowers in your Hair": "Wear Flowers in Your Hair",
    "You've had your Chips": "You've Had Your Chips",
}

# SCM 中不声明标题键的活动脚本。用 wiki 上的实际页面标题，
# 而不是游戏内的活动名，否则检索改写会拿到一个语料里不存在的词。
ACTIVITIES: dict[str, str] = {
    "intro": "In the Beginning",
    "bschoo": "Boat School",
    "bskool": "Bike School",
    "shrange": "Ammu-Nation Shooting Ranges",
    "gym": "Gyms",
    "gymls": "Ganton Gym",
    "gymsf": "Cobra Marital Arts Gym",
    "gymlv": "Below the Belt Gym",
    "taxiodd": "Taxi Driver in GTA San Andreas",
    "ambulan": "Paramedic in GTA San Andreas",
    "firetru": "Firefighter in GTA San Andreas",
    "copcar": "Vigilante in GTA San Andreas",
    "burgjb": "Burglar",
    "freight": "Freight Train Challenge",
    "kicksta": "Kickstart",
    "hotr": "8-Track",
    "lowr": "Lowrider Challenge",
    "valet_l": "Valet",
    "impexpc": "Exports and Imports",
    "impexpm": "Exports and Imports",
    "bcour": "Courier",
    "hj": "Unique Stunt Jumps in GTA San Andreas",
    "pool2": "Pool",
    "cprace": "Race Tournaments in GTA San Andreas",
}


def mission_name(script: str | None) -> str | None:
    """脚本代号转任务名。未知代号返回 None——不猜。"""
    if not script:
        return None
    key = script.strip().lower()
    title = SCRIPT_TITLES.get(key)
    if title:
        return WIKI_ALIAS.get(title, title)
    return ACTIVITIES.get(key)
