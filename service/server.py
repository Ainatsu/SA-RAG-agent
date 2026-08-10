"""SA Agent 服务端。

与游戏内覆盖层（C++ ASI 插件）通过本地 TCP 通信。

协议：长度前缀帧，小端 4 字节长度 + 1 字节类型 + UTF-8 载荷。
长度字段计入类型字节，即 len == 1 + len(payload)。

    C++ -> Python
        Q  提问，载荷为问题文本
        S  玩家状态快照，载荷为 JSON。在 Q 帧之前发出，服务端暂存后
           与随后的 Q 帧配对使用。状态不可用时不发这一帧。
           这一帧同时是"游戏正在进行中"的唯一判据，见下面的会话说明。
        V  语音录制控制，载荷为 "start" 或 "stop"
        G  拾取物清单，载荷为 JSON 数组。覆盖层定期扫描游戏内存得到，
           服务端缓存下来做"最近的防弹衣在哪"这类空间查询。

    Python -> C++
        T  回复片段（流式，可多次）
        D  本轮回复结束，载荷为空
        E  出错，载荷为错误描述
        X  语音识别结果，载荷为识别出的文本（空表示没听到内容）
        W  标点请求，载荷为 "x,y,z"。覆盖层收到后在雷达上插一面旗。
        P  语音问答阶段提示，供覆盖层画 HUD。载荷取值：
             recording / listening  正在录音
             thinking               识别完了，正在检索生成
             q:<文本>               识别出的问题，给玩家核对
             speaking               正在朗读回答
             idle                   本轮结束。这是唯一的收尾标记，
                                    覆盖层读到它才认为不再有回帧
           唤醒词那一路没有对应的请求帧，P 帧是服务端主动广播的，
           覆盖层空闲时也要顺手收一下。

回复由 RAG 管线生成：查询改写 → BM25 检索 GTA Wiki 语料 → DeepSeek 生成。

语音有三条链路，共用 voice/speech 两个模块：
    覆盖层里按住左 Ctrl（V 帧）  录音 → 识别 → X 帧回填输入框，答案是文字
    游戏中按住鼠标侧键（L 帧）   录音 → 识别 → RAG → 朗读，全程语音
    说「小龟J」唤醒（无请求帧）  麦克风常开，听到唤醒词就录 → RAG → 朗读
三条都用同一个麦克风，所以录音期间要把唤醒监听挂起，见 Mic。
后两条边生成边念（speak_reply）：第一句生成好就开始出声，不等整段写完。

第三条的麦克风是常开的，所以它只在游戏正在进行中才开：覆盖层每 3 秒推一帧
玩家状态，而那一帧只有真的进了存档（且游戏版本对得上）才会发出来，收得到就
开麦、断流或覆盖层断开就关麦，见 mark_game_active / game_watchdog。
服务端可以先于游戏启动，模型照旧提前加载，只是不录音。
前两条不受这个开关影响——它们本来就得由覆盖层发帧才会动，游戏没跑就不会有帧。
"""

import asyncio
import json
import logging
import math
import os
import queue
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import speech
import voice
from rag.pipeline import Pipeline, load_env

HOST = "127.0.0.1"
PORT = 51678

FRAME_QUESTION = b"Q"
FRAME_STATE = b"S"
FRAME_VOICE = b"V"
FRAME_TOKEN = b"T"
FRAME_DONE = b"D"
FRAME_ERROR = b"E"
FRAME_TRANSCRIPT = b"X"
FRAME_LIVE = b"L"          # 实时语音问答控制：start/stop
FRAME_LIVE_STATUS = b"P"   # 实时语音进度，供覆盖层外的 HUD 提示
FRAME_PICKUPS = b"G"       # 拾取物清单，覆盖层定期上报
FRAME_WAYPOINT = b"W"      # 标点请求，载荷 "x,y,z"

MAX_FRAME = 1 << 20  # 1 MB，防止异常长度导致内存暴涨

log = logging.getLogger("sa-agent")

# 全局单例：索引构建耗时，只做一次
pipeline: Pipeline | None = None

# 唤醒词监听是全局的，不挂在某条连接上：它得在覆盖层没连上时也能工作。
# 已连接的覆盖层记在这里，用于把 HUD 提示广播出去。
clients: set[asyncio.StreamWriter] = set()
# 覆盖层最近推来的玩家状态，唤醒提问时取用
latest_state: dict | None = None
# 覆盖层最近推来的拾取物清单（武器/防弹衣/医疗包的坐标），
# 用于"最近的防弹衣在哪"这类查询。整包覆盖，不累加。
latest_pickups: list[dict] = []
# 事件检测：保存前一帧状态用于差分
prev_state: dict | None = None
# 告警冷却：避免短时间内重复播报同一事件
last_alert_time: dict[str, float] = {}
# 对话占用标志。录音、生成、朗读期间置位，事件告警见到就让路
dialog_busy = False
# 唤醒词监听器，朗读期间需要挂起它。模型就绪后建好，开麦另由 wake_sync 决定
wake_listener: "voice.WakeListener | None" = None

# ── 游戏会话 ──────────────────────────────────────────────────────
# 唤醒词是唯一不用按键的入口，代价是麦克风得常开，所以它必须跟着游戏的起落走：
# 服务端先起来等游戏是常态，但玩家没在玩的时候不该录音。
#
# 判据用覆盖层推来的状态帧：ReadPlayerState 只有在版本对得上、且玩家已经进了
# 存档时才给出有效数据，覆盖层也只在那时才发这一帧。主菜单、读盘、以及游戏被
# 切到后台不出帧（EndScene 不再被调用）时都收不到。
game_active = False
# 最近一帧有效状态到达的时刻（monotonic）
last_state_at = 0.0
# 状态帧断流多久算游戏不在进行中。覆盖层每 3 秒推一帧，但它的网络线程在打字
# 问答的整轮里都堵在收 token 上，那段时间的心跳会被丢掉——这个窗口要容得下
# 一次完整的生成，否则打一次字麦克风就关一次。
GAME_IDLE_TIMEOUT = 15.0

# 朗读轮次。玩家插话时上一轮的回答往往还在生成，光掐掉扬声器不够——
# 那一轮的后续句子还会接着往合成队列里推，念到新问题的答案中间去。
# 每开一轮就加一，旧轮次发现号变了就自己收手。
speak_token = 0


def next_speak_token() -> int:
    global speak_token
    speak_token += 1
    return speak_token


def cancel_speech() -> None:
    """打断正在进行的朗读，并让上一轮剩下的生成不要再往外念。"""
    next_speak_token()
    speech.stop()


def wake_pause() -> None:
    """挂起唤醒监听并放开麦克风。未启用监听时是空操作。"""
    if wake_listener is not None:
        wake_listener.pause()


def wake_resume() -> None:
    if wake_listener is not None:
        wake_listener.resume()


async def wake_sync() -> None:
    """把唤醒监听的开麦状态对齐到当前游戏状态。

    stop() 要 join 监听线程（最多 3 秒）、start() 要打开输入设备，两个都不算快，
    放到线程里做，别卡住事件循环。
    """
    if wake_listener is None:
        return          # 唤醒词没启用，或者识别模型还没就绪

    if game_active and not wake_listener.active:
        try:
            await asyncio.to_thread(wake_listener.start)
        except Exception as e:
            log.warning("唤醒监听开麦失败（按住鼠标侧键说话仍可用）: %s", e)
    elif not game_active and wake_listener.active:
        await asyncio.to_thread(wake_listener.stop)
        log.info("唤醒监听已停止，麦克风已释放")


async def mark_game_active() -> None:
    """收到一帧有效状态：游戏正在进行中。"""
    global game_active, last_state_at

    last_state_at = time.monotonic()
    if game_active:
        return
    game_active = True
    log.info("游戏正在进行中，开始接收语音")
    await wake_sync()


async def mark_game_idle(reason: str) -> None:
    """游戏不在进行中了：关麦，并丢掉只对刚结束那段游戏有意义的缓存。"""
    global game_active, prev_state, latest_state, latest_pickups

    if not game_active:
        return
    game_active = False
    log.info("游戏已不在进行中（%s），停止接收语音", reason)

    # 差分基线和拾取物清单描述的都是上一段游戏，留着只会喂出错的数据：
    # 换个存档进来，missions_passed 一变就会误报一句"任务通过"。
    prev_state = None
    latest_state = None
    latest_pickups = []

    await wake_sync()


async def game_watchdog() -> None:
    """盯着状态帧有没有断流。

    玩家退回主菜单、正在读盘、或者把游戏切到后台（不出帧，EndScene 不再被调用）
    时覆盖层不会专门说一声，只能靠状态帧停了来判断。
    """
    while True:
        await asyncio.sleep(1.0)
        if game_active and time.monotonic() - last_state_at > GAME_IDLE_TIMEOUT:
            await mark_game_idle("状态帧已断流")


class Mic:
    """一条连接上的麦克风占用。

    唤醒监听把麦克风常开着，而"按住说话"要另开一条输入流录同一个设备。
    录音前必须让监听把流交出来：既避免和 Recorder 抢设备，也免得玩家这段
    话被监听那一路再当成一次唤醒提问处理一遍。
    """

    def __init__(self) -> None:
        self.recorder = voice.Recorder()
        self._held = False

    def acquire(self) -> None:
        if not self._held:
            self._held = True
            wake_pause()

    def release(self) -> None:
        if self._held:
            self._held = False
            wake_resume()

    def abort(self) -> None:
        """出错或断连时的收尾：丢掉录音并交回麦克风。"""
        self.recorder.abort()
        self.release()



def broadcast_live_status(status: str) -> None:
    """把实时语音的阶段提示推给所有覆盖层。断开的连接静默跳过。"""
    for w in list(clients):
        try:
            write_frame(w, FRAME_LIVE_STATUS, status)
        except Exception:
            clients.discard(w)


def broadcast_waypoint(payload: str) -> None:
    """把标点请求推给所有覆盖层。唤醒词那一路没有对应连接，只能广播。"""
    for w in list(clients):
        try:
            write_frame(w, FRAME_WAYPOINT, payload)
        except Exception:
            clients.discard(w)


# 事件告警冷却，单位秒。同一类事件在冷却期内只播一次，
# 否则挨枪时每个心跳都会喊一遍。
ALERT_COOLDOWN = {
    "health": 12.0,
    "armour": 20.0,
    "wanted": 8.0,
    "mission": 5.0,
    "ammo": 15.0,
    "hit": 8.0,
    "death": 5.0,
}

# 受击来源跟踪：同一攻击者在短窗口内连续命中才算"被盯上"。
# 攻击者位移超阈值或间隔超时都视为换人，重新计数。
HIT_SERIES_WINDOW = 6.0
HIT_SERIES_DIST = 10.0
_hit_series = {"x": 0.0, "y": 0.0, "z": 0.0, "n": 0, "t": 0.0}

# eWeaponType 中非武器类的死因（gta-reversed eWeaponType.h）：
# 49 RAMMEDBYCAR / 50 RUNOVERBYCAR / 51 EXPLOSION /
# 52 UZI_DRIVEBY / 53 DROWNING / 54 FALL。
# 0-46 的武器名由 C++ 侧给到 last_damage_name。
_DEATH_CAUSES = {
    49: "被车撞死",
    50: "被车碾过",
    51: "被炸死",
    52: "被驾车枪击致死",
    53: "溺水淹死",
    54: "摔死",
}


def _death_cause(state: dict) -> str | None:
    """从最近一次伤害来源构造死因文案。说不清时返回 None。"""
    t = state.get("last_damage_weapon")
    if t in _DEATH_CAUSES:
        return _DEATH_CAUSES[t]
    name = state.get("last_damage_name") or ""
    if name and name not in ("拳头", "未知武器", "不明原因"):
        return f"被{name}打死"
    return None


def detect_events(prev: dict, curr: dict, now: float | None = None) -> list[tuple[str, str]]:
    """比较前后两帧状态，返回 [(事件类别, 播报文本)]。

    只认那些玩家来不及看 HUD 的突变，平稳变化一律不吭声。
    """
    if now is None:
        now = time.monotonic()
    events: list[tuple[str, str]] = []

    # 死亡复盘：TimesWasted 只增不减，涨了就是挂了一次。
    # 必须排最前：死亡瞬间血量掉到 0，若让血量告警先报，等下一次心跳
    # 玩家已满血复活，这次死亡就再也不会被提起。
    pt, ct = prev.get("times_wasted", 0), curr.get("times_wasted", 0)
    if ct > pt:
        cause = _death_cause(curr)
        text = (f"你挂了，{cause}。这是第{ct}次死亡。" if cause
                else f"你挂了，这是第{ct}次死亡。")
        events.append(("death", text))

    # 血量骤降：按最大血量的比例算，光看绝对值在高血上限时不敏感
    ph, ch = prev.get("health", 0.0), curr.get("health", 0.0)
    mx = curr.get("max_health", 100.0) or 100.0
    if ph - ch >= mx * 0.25 and ch > 0:
        events.append(("health", f"血量掉得厉害，只剩{int(ch)}了，先脱离。"))
    elif ch <= mx * 0.15 and ph > mx * 0.15:
        events.append(("health", "血量见底了，赶紧找回血点。"))

    # 护甲清空：护甲一没，下一发就是实打实的伤害
    pa, ca = prev.get("armour", 0.0), curr.get("armour", 0.0)
    if pa > 0 and ca <= 0:
        events.append(("armour", "护甲碎了，注意掩体。"))

    # 受击来源：心跳间隔内掉血且存在伤害来源 → 判断攻击方向。
    # 需要同一来源连续命中才播报，避免路边误伤之类的一次性事件刷屏。
    global _hit_series
    ph2, ch2 = prev.get("health", 0.0), curr.get("health", 0.0)
    if (ch2 < ph2 and (curr.get("last_damage_x") or curr.get("last_damage_y")
                       or curr.get("last_damage_z"))):
        hx, hy = curr["last_damage_x"], curr["last_damage_y"]
        if (now - _hit_series["t"] > HIT_SERIES_WINDOW
                or math.hypot(hx - _hit_series["x"], hy - _hit_series["y"]) > HIT_SERIES_DIST):
            _hit_series = {"x": hx, "y": hy, "z": curr.get("last_damage_z", 0.0),
                           "n": 0, "t": now}
        _hit_series["n"] += 1
        _hit_series["t"] = now

        if _hit_series["n"] >= 2:
            # 攻击者方位（从玩家指向攻击者）与玩家朝向的夹角。heading 0 = 北、
            # 顺时针增大，与 describe_direction 的方位约定一致。
            heading = curr.get("heading", 0.0)
            bearing = math.atan2(hx - curr.get("x", 0.0), hy - curr.get("y", 0.0))
            diff = (bearing - heading + math.pi) % (2.0 * math.pi) - math.pi
            if abs(diff) > math.pi / 2.0:
                events.append(("hit", "背后有人！"))
            else:
                direction = describe_direction(hx - curr.get("x", 0.0),
                                               hy - curr.get("y", 0.0))
                events.append(("hit", f"有人从{direction}方向打你"))

    # 通缉升星
    pw, cw = prev.get("wanted", 0), curr.get("wanted", 0)
    if cw > pw:
        if cw >= 4:
            events.append(("wanted", f"通缉{cw}星，特警要来了，赶紧甩掉。"))
        else:
            events.append(("wanted", f"通缉升到{cw}星了。"))
    elif pw > 0 and cw == 0:
        events.append(("wanted", "通缉解除了。"))

    # 任务通过：MissionsPassed 只增不减，涨了就是过了一关
    pm, cm = prev.get("missions_passed", 0), curr.get("missions_passed", 0)
    if cm > pm:
        events.append(("mission", f"任务通过，已完成{cm}个任务。"))

    # 子弹耗尽：同一把武器，备用弹药从有到无。切换武器也会让 ammo_total
    # 归零，但那不是"打光子弹"，用武器类型不变来排除。
    pw = prev.get("weapon_type", -1)
    cw = curr.get("weapon_type", -1)
    if pw == cw and pw >= 16:   # 0-15 为近战武器，没有弹药概念
        pa = prev.get("ammo_total", 0)
        ca = curr.get("ammo_total", 0)
        if pa > 0 and ca == 0:
            events.append(("ammo", "没子弹了，换武器！"))

    return events


async def announce_event(kind: str, text: str) -> None:
    """播报一条告警：HUD 文字 + 语音。

    玩家正在提问或听回答时直接放弃这一条——抢扬声器只会两段话糊在一起，
    告警本身也没重要到值得打断对话。
    """
    if dialog_busy:
        return

    log.info("事件告警[%s]: %s", kind, text)
    broadcast_live_status("q:" + text)
    try:
        await asyncio.to_thread(speech.say_sync, text)
    except Exception:
        log.exception("告警朗读失败")
    finally:
        broadcast_live_status("idle")


async def check_events(state: dict) -> None:
    """心跳帧到达时做一次差分检测，命中就播报。"""
    global prev_state

    if prev_state is None:
        prev_state = state
        return

    now = time.monotonic()
    events = detect_events(prev_state, state, now)
    prev_state = state

    for kind, text in events:
        if now - last_alert_time.get(kind, 0.0) < ALERT_COOLDOWN.get(kind, 10.0):
            continue
        last_alert_time[kind] = now
        await announce_event(kind, text)
        break  # 一次心跳最多播一条，避免连着念好几句


def _norm_weapon(name: str) -> str:
    """武器名归一化，用于模糊匹配：玩家会说"AK"，清单里写的是"AK-47"。"""
    return name.lower().replace("-", "").replace(" ", "")


def find_nearest_pickup(player_x: float, player_y: float, kind: str,
                        weapon_name: str = "") -> dict | None:
    """找最近的拾取物。

    kind 取 "armor" / "health" / "weapon"；weapon_name 只在 kind 为 weapon
    时有意义，空表示任意武器。找不到返回 None。

    只算平面距离：SA 的高度差对"该往哪跑"几乎没有参考价值，
    算上 z 反而会让立交桥上下的两个点排序错乱。
    """
    candidates = [p for p in latest_pickups if p.get("kind") == kind]
    if kind == "weapon" and weapon_name:
        wn = _norm_weapon(weapon_name)
        candidates = [p for p in candidates if wn in _norm_weapon(p.get("name", ""))]

    if not candidates:
        return None

    def dist(p: dict) -> float:
        return math.hypot(p["x"] - player_x, p["y"] - player_y)

    nearest = min(candidates, key=dist)
    return {
        "x": nearest["x"],
        "y": nearest["y"],
        "z": nearest["z"],
        "distance": dist(nearest),
        "name": nearest.get("name", ""),
        "zone": nearest.get("zone", ""),
    }


# 寻物意图的触发词。"我想要一把武器"和"最近的防弹衣在哪"都算，
# 前者没有疑问词，光靠"在哪/最近"这类位置词会漏判。
NEAREST_HINTS = (
    "最近", "附近", "哪里", "哪儿", "在哪", "带我去", "标一下", "标个点",
    "我想要", "我要", "想找", "找一", "给我", "需要", "补充", "补给",
    "去哪", "怎么走", "多远",
)

PICKUP_KEYWORDS = {
    "armor": ("防弹衣", "护甲", "盔甲", "armor", "armour"),
    "health": ("医疗包", "回血", "血包", "急救", "health"),
}


def parse_nearest_intent(question: str) -> tuple[str, str] | None:
    """识别寻物意图。

    命中返回 (kind, weapon_name)，否则返回 None。weapon_name 为空表示
    不限具体武器。判断故意保守：宁可漏判交给纯 RAG，也不要把
    "AK47伤害多少"这种纯知识问题误当成寻路请求。
    """
    if not any(h in question for h in NEAREST_HINTS):
        return None

    q = question.lower()
    for kind, words in PICKUP_KEYWORDS.items():
        if any(w in q for w in words):
            return (kind, "")

    # 武器：拿清单里实际存在的名字去反查，比维护一张关键词表更准，
    # 也自动跟着 game.cpp 的 WeaponName 走。
    names = {p.get("name", "") for p in latest_pickups if p.get("kind") == "weapon"}
    for name in names:
        if name and _norm_weapon(name) in _norm_weapon(question):
            return ("weapon", name)

    if "武器" in question or "枪" in question or "弹药" in question:
        return ("weapon", "")

    return None


def describe_direction(dx: float, dy: float) -> str:
    """把坐标差换算成方位词。SA 的 +y 是北，+x 是东。"""
    if abs(dx) < 1e-6 and abs(dy) < 1e-6:
        return "就在脚下"
    # 八方位，每格 45 度。从正北顺时针数。
    dirs = ("北", "东北", "东", "东南", "南", "西南", "西", "西北")
    deg = math.degrees(math.atan2(dx, dy)) % 360.0
    return dirs[int((deg + 22.5) % 360.0 // 45)]


KIND_LABEL = {"armor": "防弹衣", "health": "医疗包", "weapon": "武器"}


def build_pickup_context(kind: str, weapon_name: str,
                         state: dict | None) -> tuple[str, str] | None:
    """算出最近的拾取物，产出给 RAG 用的实时线索 + 标点载荷。

    返回 (线索文本, "x,y,z")。算不出来时返回 None，让问题按普通提问走
    RAG——语料里至少有"某某区域有火箭筒"这类描述，比直接回一句
    "没找到"有用。

    这里只负责取数，不负责作答：答案统一由 RAG 生成，实时坐标只是
    喂给它的一条额外资料，这样"在什么地方"（Wiki 语料）和"离这里多远"
    （内存实时数据）能揉进同一句话里。
    """
    if not state or not latest_pickups:
        return None

    hit = find_nearest_pickup(state.get("x", 0.0), state.get("y", 0.0),
                              kind, weapon_name)
    if hit is None:
        return None

    direction = describe_direction(hit["x"] - state.get("x", 0.0),
                                   hit["y"] - state.get("y", 0.0))
    label = hit["name"] or KIND_LABEL.get(kind, "目标")
    zone = hit["zone"] or "未知区域"
    payload = f"{hit['x']:.1f},{hit['y']:.1f},{hit['z']:.1f}"

    clue = (f"游戏内实时扫描到，离玩家最近的{label}位于 {zone}，"
            f"在玩家{direction}方向约 {int(hit['distance'])} 米处，"
            f"已自动标记到玩家的雷达上。")
    log.info("邻近线索: %s (%s)", clue, payload)
    return clue, payload


async def send_waypoint(writer: asyncio.StreamWriter | None, payload: str) -> None:
    """下发标点帧。writer 为 None 时广播到所有覆盖层。"""
    if writer is not None:
        write_frame(writer, FRAME_WAYPOINT, payload)
        await writer.drain()
    else:
        broadcast_waypoint(payload)


async def read_frame(reader: asyncio.StreamReader) -> tuple[bytes, str] | None:
    header = await reader.readexactly(4)
    length = int.from_bytes(header, "little")
    if length < 1 or length > MAX_FRAME:
        raise ValueError(f"帧长度异常: {length}")

    body = await reader.readexactly(length)
    return body[:1], body[1:].decode("utf-8", errors="replace")


def write_frame(writer: asyncio.StreamWriter, ftype: bytes, payload: str = "") -> None:
    data = payload.encode("utf-8")
    body = ftype + data
    writer.write(len(body).to_bytes(4, "little") + body)


async def generate_reply(question: str, state: dict | None, voice: bool = False):
    """产出回复片段。

    RAG 管线是同步阻塞的（HTTP 调用 + BM25 计算），直接在事件循环里跑
    会卡住整个服务。这里放到线程中执行，用队列把片段传回事件循环。
    """
    assert pipeline is not None

    q: queue.Queue = queue.Queue()
    SENTINEL = object()

    def worker() -> None:
        try:
            for piece in pipeline.answer(question, state, voice=voice):
                q.put(piece)
        except Exception as e:
            q.put(e)
        finally:
            q.put(SENTINEL)

    threading.Thread(target=worker, daemon=True).start()

    loop = asyncio.get_running_loop()
    while True:
        item = await loop.run_in_executor(None, q.get)
        if item is SENTINEL:
            break
        if isinstance(item, Exception):
            raise item
        yield item


async def speak_reply(question: str, state: dict | None,
                      status: "Callable[[str], Awaitable[None]]") -> tuple[str, bool]:
    """生成回答并边生成边念。返回 (念出去的文本, 本轮是否走到了头)。

    整段生成完再念的话，玩家按完键要干等好几秒（检索 + 改写 + 生成）。
    这里按句切开：第一句一出来就开始念，后面的接着排进合成队列，听起来
    是连续的一段话，而首句延迟只剩下生成第一句的时间。

    返回的第二个值为 False 表示玩家已经开了新一轮，本轮作废——调用方此时
    不该再推 P 帧，那些帧会盖掉新一轮的 HUD。

    status 用来推 P 帧；"speaking" 在真正出声的那一刻才发，早发会让 HUD
    先跳到"回答中"再干等。
    """
    token = next_speak_token()
    seg = speech.Segmenter()
    parts: list[str] = []
    speaking = False

    speech.begin()

    async def emit(text: str) -> None:
        nonlocal speaking
        if not speaking:
            speaking = True
            await status("speaking")
        speech.push(text)

    async for chunk in generate_reply(question, state, voice=True):
        if token != speak_token:
            log.info("朗读已被新一轮打断，放弃剩下的回答")
            return "".join(parts), False
        parts.append(chunk)
        for s in seg.feed(chunk):
            await emit(s)

    tail = seg.flush()
    if tail:
        await emit(tail)

    # 被打断时别再等：那时合成队列里装的是新一轮的内容，等下去会一直等到
    # 新回答念完，把本轮的收尾（idle、恢复麦克风）一起拖住。
    if token != speak_token:
        log.info("朗读已被新一轮打断，放弃剩下的回答")
        return "".join(parts), False

    await asyncio.to_thread(speech.finish)
    return "".join(parts), token == speak_token


async def handle_voice(writer: asyncio.StreamWriter, command: str,
                       mic: Mic) -> None:
    """处理语音控制帧。识别结果以 X 帧回传，供覆盖层填入输入框。

    录音启停要立刻生效（玩家按住键就该开始录），所以走同步调用；识别耗时
    较长，放到线程里，避免阻塞事件循环导致后续帧堆积。
    """
    if command == "start":
        mic.acquire()
        await asyncio.to_thread(mic.recorder.start)
        return

    if command != "stop":
        log.warning("未知语音指令 %r，忽略", command)
        return

    try:
        audio = await asyncio.to_thread(mic.recorder.stop)
    finally:
        # 识别不需要麦克风，录完就把设备交回唤醒监听
        mic.release()

    if len(audio) == 0:
        write_frame(writer, FRAME_TRANSCRIPT)
        await writer.drain()
        return

    text = await asyncio.to_thread(voice.transcribe, audio)
    log.info("语音识别: %s", text or "（无内容）")
    write_frame(writer, FRAME_TRANSCRIPT, text)
    await writer.drain()


async def handle_live(writer: asyncio.StreamWriter, command: str,
                      mic: Mic, state: dict | None) -> None:
    """实时语音问答：录音 → 识别 → RAG → 朗读。

    与 handle_voice 的区别是这一路不经过覆盖层输入框，答案直接念出来。
    游戏不暂停，所以每一步都往回发 P 帧，让玩家知道进行到哪了；一轮结束
    统一以 idle 收尾，覆盖层收到 idle 才认为本轮回帧结束。

    心跳指令走的是同一条 L 帧通道，但不进问答流程：它只带状态，
    交给事件检测做差分。
    """
    global dialog_busy

    if command == "heartbeat":
        if state is not None:
            await check_events(state)
        return

    async def status(s: str) -> None:
        write_frame(writer, FRAME_LIVE_STATUS, s)
        await writer.drain()

    if command == "start":
        # 玩家开新一轮提问，上一段还在念的立刻掐掉，否则两段话会叠在一起。
        # 连带把上一轮剩下的生成作废，不然它会接着往合成队列里推句子。
        # 只是立个标志加投一条指令，不会阻塞，直接在事件循环里做。
        cancel_speech()
        dialog_busy = True
        mic.acquire()
        await asyncio.to_thread(mic.recorder.start)
        await status("recording")
        return

    if command != "stop":
        log.warning("未知实时语音指令 %r，忽略", command)
        return

    # 这一路直到念完都占着麦克风：朗读期间要是把设备还给唤醒监听，
    # 监听立刻会把 agent 自己的回答录进去当成新的提问。
    try:
        audio = await asyncio.to_thread(mic.recorder.stop)
        if len(audio) == 0:
            await status("idle")
            return

        await status("thinking")
        question = await asyncio.to_thread(voice.transcribe, audio)
        log.info("实时语音提问: %s", question or "（无内容）")
        if not question:
            await status("speaking")
            await asyncio.to_thread(speech.say_sync, "没听清，再说一遍。")
            await status("idle")
            return

        # 把问题也发回去，玩家能在 HUD 上确认识别对不对
        await status("q:" + question)

        # "最近的防弹衣在哪"命中意图时不要绕过 RAG——Wiki 语料有区域描述，
        # 实时扫描给出距离，把后者拼进问题让模型揉成一句话。
        intent = parse_nearest_intent(question)
        ctx = build_pickup_context(intent[0], intent[1], state) if intent else None
        if ctx is not None:
            await send_waypoint(writer, ctx[1])
            question = f"{question}\n\n以下为游戏内实时扫描信息（供参考）：\n{ctx[0]}"

        answer, ok = await speak_reply(question, state, status)
        log.info("实时语音回答: %s", answer)
        if ok:
            await status("idle")
    finally:
        mic.release()
        dialog_busy = False


async def broadcast_status(status: str) -> None:
    """broadcast_live_status 的协程版，给 speak_reply 当回调用。"""
    broadcast_live_status(status)


async def handle_wake_question(question: str) -> None:
    """处理唤醒词触发的提问：RAG 生成 + 朗读，全程不经过覆盖层。"""
    # 关麦到监听线程真正收手之间有个空档（它可能正卡在识别里），这一句有可能
    # 是游戏已经退出之后才交上来的，直接丢掉——没人在听了。
    if not game_active:
        log.info("游戏不在进行中，丢弃这次唤醒提问: %s", question)
        return

    # 整轮都挂起监听。放到朗读前才挂来不及：生成要好几秒，这段时间里麦克风
    # 还开着，玩家一句"小龟J"的余音或旁边的说话声就能再触发一轮，两轮的
    # 回答会抢同一个扬声器。
    wake_pause()
    ok = True
    try:
        # 先回显识别结果，玩家一眼能看出是不是听错了；listening/thinking
        # 已经由监听线程推过了，这里不重复
        broadcast_live_status("q:" + question)

        intent = parse_nearest_intent(question)
        ctx = build_pickup_context(intent[0], intent[1], latest_state) if intent else None
        if ctx is not None:
            await send_waypoint(None, ctx[1])
            question = f"{question}\n\n以下为游戏内实时扫描信息（供参考）：\n{ctx[0]}"

        try:
            answer, ok = await speak_reply(question, latest_state,
                                           broadcast_status)
        except Exception:
            log.exception("唤醒提问生成回复失败")
            await asyncio.to_thread(speech.say_sync, "出了点问题，看下服务端日志。")
            return
        log.info("唤醒回答: %s", answer)
    finally:
        wake_resume()
        # 被新一轮顶掉时不发 idle，那会把新一轮的 HUD 提示清掉
        if ok:
            broadcast_live_status("idle")


def make_wake_listener(loop: asyncio.AbstractEventLoop) -> voice.WakeListener:
    """建好唤醒监听，但不开麦。回调发生在监听线程，要切回事件循环再干活。"""

    def on_utterance(text: str) -> None:
        asyncio.run_coroutine_threadsafe(handle_wake_question(text), loop)

    def on_state(state: str) -> None:
        loop.call_soon_threadsafe(broadcast_live_status, state)

    return voice.WakeListener(on_utterance, on_state)


def wake_enabled() -> bool:
    """唤醒监听默认开着。麦克风常开 + 反复跑识别很吃 CPU，
    想省下来就设 SA_WAKE=0，此时仍可按鼠标侧键说话。"""
    return os.environ.get("SA_WAKE", "1").strip().lower() \
        not in ("0", "false", "off", "no", "")


def enable_wake(loop: asyncio.AbstractEventLoop) -> None:
    """模型就绪后把监听器建起来。失败不影响其他两条语音链路。

    这里只是建好待命，开麦要等游戏跑起来——判断都在 wake_sync 里。
    """
    global wake_listener
    if wake_listener is not None:
        return
    try:
        wake_listener = make_wake_listener(loop)
    except Exception as e:
        log.warning("唤醒监听准备失败（按住鼠标侧键说话仍可用）: %s", e)
        return
    log.info("唤醒监听已就绪，游戏开始后自动开麦，说「%s」唤醒",
             voice.WAKE_WORD_DISPLAY)
    # 服务端可能是在游戏跑起来之后才装好模型的，那就立刻开麦
    loop.create_task(wake_sync())


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    global latest_state, latest_pickups
    peer = writer.get_extra_info("peername")
    log.info("覆盖层已连接 %s", peer)

    # 最近一次收到的玩家状态，等下一个 Q 帧取用
    pending_state: dict | None = None
    mic = Mic()
    clients.add(writer)

    try:
        while True:
            try:
                frame = await read_frame(reader)
            except asyncio.IncompleteReadError:
                break  # 对端关闭

            ftype, payload = frame

            if ftype == FRAME_STATE:
                try:
                    pending_state = json.loads(payload) or None
                except json.JSONDecodeError:
                    log.warning("状态帧不是合法 JSON，忽略: %s", payload[:200])
                    pending_state = None
                # 唤醒提问不走 Q 帧，取不到 pending_state，另存一份
                latest_state = pending_state
                # 拿得到状态就说明玩家真的在游戏里，唤醒监听据此开麦
                if pending_state is not None:
                    await mark_game_active()
                continue

            if ftype == FRAME_PICKUPS:
                try:
                    items = json.loads(payload)
                except json.JSONDecodeError:
                    log.warning("拾取物帧不是合法 JSON，忽略")
                    continue
                if isinstance(items, list):
                    # 整包覆盖：清单反映的是当前场景，攒历史只会报出已被捡走的点
                    latest_pickups = items
                    log.info("拾取物清单已更新，共 %d 个点", len(items))
                continue

            if ftype == FRAME_VOICE:
                try:
                    await handle_voice(writer, payload.strip(), mic)
                except Exception:
                    # 麦克风被占用、模型下载失败等都会走到这里。
                    # E 帧要排在 X 帧之前：覆盖层收到 X 就结束本轮语音等待，
                    # 之后发出的帧会被留在缓冲区，串到下一轮问答里。
                    log.exception("语音处理失败")
                    mic.abort()
                    write_frame(writer, FRAME_ERROR, "语音识别失败，请查看服务端日志")
                    write_frame(writer, FRAME_TRANSCRIPT)
                    await writer.drain()
                continue

            if ftype == FRAME_LIVE:
                # 实时问答不占用 pending_state：玩家可能连问几轮，
                # 状态帧是覆盖层定期推的，这里读最近一份即可。
                try:
                    await handle_live(writer, payload.strip(), mic, pending_state)
                except Exception:
                    log.exception("实时语音问答失败")
                    mic.abort()
                    write_frame(writer, FRAME_LIVE_STATUS, "idle")
                    await writer.drain()
                continue

            if ftype != FRAME_QUESTION:
                log.warning("收到未知帧类型 %r，忽略", ftype)
                continue

            state, pending_state = pending_state, None  # 用完即弃，避免串轮次

            log.info("提问: %s", payload)
            if state:
                log.info("玩家状态: %s", state)
            try:
                async for chunk in generate_reply(payload, state):
                    write_frame(writer, FRAME_TOKEN, chunk)
                    await writer.drain()
                write_frame(writer, FRAME_DONE)
            except Exception:
                log.exception("生成回复失败")
                write_frame(writer, FRAME_ERROR, "生成回复失败，请查看服务端日志")
            await writer.drain()

    except (ConnectionResetError, BrokenPipeError):
        # 游戏进程被强杀时会走到这里；正常退出是干净的 EOF，不会到这
        log.info("连接被对端重置")
    except Exception:
        log.exception("连接处理异常")
    finally:
        log.info("覆盖层断开 %s", peer)
        # 别留在广播集合里，否则唤醒提示会一直往死连接上写
        clients.discard(writer)
        mic.abort()
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionResetError, BrokenPipeError):
            pass
        # 游戏关掉或崩了，麦克风不该还开着
        if not clients:
            await mark_game_idle("覆盖层已断开")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    # openai/httpx 每次请求都打一行 INFO，噪音太大
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # 唤醒监听每 0.7 秒扫一次，faster-whisper 每次识别都打两行 INFO
    #（Processing audio / VAD filter removed），会把真正有用的日志冲掉
    logging.getLogger("faster_whisper").setLevel(logging.WARNING)

    global pipeline
    load_env()
    try:
        pipeline = Pipeline()
    except Exception as e:
        log.error("初始化失败: %s", e)
        return

    try:
        server = await asyncio.start_server(handle_client, HOST, PORT)
    except OSError as e:
        if e.errno in (48, 98, 10048):  # 各平台的 EADDRINUSE
            log.error("端口 %d 已被占用——通常是上一个服务端还在运行。", PORT)
            log.error("先关掉那个窗口，或执行："
                      "Get-NetTCPConnection -LocalPort %d -State Listen", PORT)
        else:
            log.error("监听失败: %s", e)
        return

    log.info("SA Agent 服务已启动，监听 %s:%d", HOST, PORT)
    log.info("等待游戏内覆盖层连接……（Ctrl+C 退出）")

    # 合成后端建起来要几百毫秒，别摊在第一次回答里
    speech.prewarm()

    # 语音模型首次加载可能要几十秒，提前在后台加载，别让玩家第一次说话时干等。
    # 加载完也只是把唤醒监听建起来待命：麦克风要等游戏真的跑起来才开，
    # 见 mark_game_active。
    if wake_enabled():
        loop = asyncio.get_running_loop()
        log.info("语音识别模型加载中，加载完并进入游戏后即可用「%s」唤醒",
                 voice.WAKE_WORD_DISPLAY)
        voice.prewarm(lambda: loop.call_soon_threadsafe(enable_wake, loop))
    else:
        log.info("唤醒词监听已关闭（SA_WAKE=0），按住鼠标侧键说话仍可用")
        voice.prewarm()

    # 覆盖层断开能立刻察觉，退回主菜单/切到后台只能靠状态帧断流来判断
    watchdog = asyncio.create_task(game_watchdog())

    try:
        async with server:
            await server.serve_forever()
    finally:
        watchdog.cancel()
        if wake_listener is not None:
            wake_listener.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
