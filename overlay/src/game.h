#pragma once
#include <windows.h>
#include <d3d9.h>

#include <string>

// 与 GTA:SA 1.0 US (HOODLUM) 的内存交互。
// 下列地址已针对本机 gta_sa.exe（MD5 170B3A9108687B26DA2D8901C6948A18,
// 14383616 字节）静态校验：均落在 .data 段且存在合理数量的代码引用。
// 换游戏版本必须重新核对。
namespace game
{
namespace addr
{
    // RenderWare 的 D3D9 设备指针（RwD3D9Device），371 处代码引用
    constexpr uintptr_t RwD3D9Device = 0x00C97C28;
    // 主窗口 HWND（PSGLOBAL.window），39 处引用
    constexpr uintptr_t WindowHandle = 0x00C97C1C;
    // CTimer::m_UserPause，42 处引用
    constexpr uintptr_t UserPause = 0x00B7CB49;

    // CWorld::Players（CPlayerInfo[2]），307 处代码引用。
    // 另经指令级确认：CWorld::FindPlayerSlotWithPedPointer(0x563FA0) 开头
    // 的 `B9 98 CD B7 00` 即 mov ecx, 0xB7CD98。
    constexpr uintptr_t Players = 0x00B7CD98;
    // CStats::StatTypesFloat，浮点统计量数组（eStats 索引 * 4）
    constexpr uintptr_t StatTypesFloat = 0x00B79380;
    // CStats::StatTypesInt，整型统计量数组（(eStats 索引 - 82) * 4）
    constexpr uintptr_t StatTypesInt = 0x00B79000;
    // CTheScripts::ScriptSpace，脚本全局变量区。
    // $ONMISSION 是全局变量 #409，故其地址为 base + 409*4 = 0xA49FC4。
    constexpr uintptr_t ScriptSpace = 0x00A49960;
    constexpr uintptr_t OnMission   = ScriptSpace + 409 * 4;
    // CTheScripts::pActiveScripts，活动脚本链表头（CRunningScript*）
    constexpr uintptr_t ActiveScripts = 0x00A8B42C;

    // CPickups::aPickUps，拾取物数组（CPickup[620]，每项 0x20 字节）。
    // 武器、护甲、血包等地面可拾取物都在这里，含坐标与是否已被拿走。
    constexpr uintptr_t Pickups = 0x009788C0;
    constexpr int       MaxPickups = 620;

    // CRadar::SetCoordBlip(eBlipType, CVector, uint, eBlipDisplay, char*)
    // __cdecl，返回 blip 句柄。这是全项目唯一一处会改动游戏状态的调用。
    constexpr uintptr_t RadarSetCoordBlip = 0x00583820;
    // CRadar::SetBlipSprite(int blipIndex, int spriteId)
    constexpr uintptr_t RadarSetBlipSprite = 0x00583D70;
    // CRadar::ClearBlip(int blipIndex)
    constexpr uintptr_t RadarClearBlip = 0x00587CE0;
}

// eStats 中我们关心的统计量索引（plugin-sdk eStats.h）
// 0-81 为浮点统计量，82+ 为整型统计量
namespace stat
{
    // 游戏进度
    constexpr int ProgressMade      = 0;
    constexpr int TotalProgress     = 1;
    
    // 行驶距离（浮点，单位：米）
    constexpr int DistanceOnFoot    = 3;
    constexpr int DistanceByCar     = 4;
    constexpr int DistanceByBike    = 5;
    constexpr int DistanceByBoat    = 6;
    constexpr int DistanceByHelicopter = 8;
    constexpr int DistanceByPlane   = 9;
    
    // 玩家属性
    constexpr int Fat        = 21;
    constexpr int Stamina    = 22;
    constexpr int Muscle     = 23;
    constexpr int MaxHealth  = 24;
    constexpr int SexAppeal  = 25;
    constexpr int Respect    = 64;
    
    // 整型统计量（需要用 StatTypesInt 数组读取，索引为 stat_id - 82）
    constexpr int PeopleKilled      = 121;  // 整型偏移 39
    constexpr int TimesWasted       = 135;  // 整型偏移 53
    constexpr int DaysPassed        = 134;  // 整型偏移 52
    constexpr int MissionsPassed    = 147;  // 整型偏移 65
}

// 取游戏当前的 D3D9 设备。未初始化时返回 nullptr。
IDirect3DDevice9* GetDevice();

// 取游戏主窗口。未创建时返回 nullptr。
HWND GetWindow();

// 读写用户暂停标志。写入后游戏逻辑停止推进，但仍继续出帧，
// 所以覆盖层依然可见、可交互。
void SetUserPause(bool paused);
bool GetUserPause();

// 校验当前进程确实是 1.0 US 版。
// 静态地址只对该版本有效，版本不符时必须停用所有状态读取——
// 读出垃圾数据喂给模型比没有数据更糟。
bool IsSupportedVersion();

// 玩家状态快照。valid 为 false 时其余字段无意义
//（尚未进入存档、正在读盘、或版本不符）。
struct PlayerState
{
    bool  valid = false;

    float health = 0.0f;
    float maxHealth = 0.0f;
    float armour = 0.0f;

    float x = 0.0f, y = 0.0f, z = 0.0f;

    // 所在地。取自 info.zon / map.zon，英文原名（与 wiki 语料一致，便于检索）。
    // 查不到时为空串。
    const char* zone = nullptr;   // 街区，如 "Ganton"
    const char* city = nullptr;   // 城市，如 "洛圣都"

    int   money = 0;
    int   wantedLevel = 0;

    bool  inVehicle = false;
    bool  onMission = false;

    // 当前任务脚本代号（如 "sweet1"），仅在任务中且识别成功时非空。
    // 这是 main.scm 里的内部名，Python 侧再映射到任务正式名。
    char  missionScript[9] = {0};

    int   weaponType = 0;     // eWeaponType
    int   ammoInClip = 0;
    int   ammoTotal = 0;

    // 朝向与最近一次伤害来源（受击来源 / 死亡复盘用）
    float heading = 0.0f;            // m_fHeadingCurrent，弧度，0 = 北、顺时针
    int   lastDamageWeapon = 0;      // m_nLastWeaponDamage（eWeaponType）
    float lastDamageX = 0.0f;        // m_pLastEntityDamage 的坐标，读不到时为 0
    float lastDamageY = 0.0f;
    float lastDamageZ = 0.0f;

    float fat = 0.0f;
    float stamina = 0.0f;
    float muscle = 0.0f;
    float respect = 0.0f;
    float sexAppeal = 0.0f;

    // 游戏进度
    float progressMade = 0.0f;      // 当前完成进度
    float totalProgress = 0.0f;     // 总进度（通常是 100.0）

    // 行驶距离（米）
    float distanceOnFoot = 0.0f;
    float distanceByCar = 0.0f;
    float distanceByBike = 0.0f;
    float distanceByBoat = 0.0f;
    float distanceByHelicopter = 0.0f;
    float distanceByPlane = 0.0f;

    // 整型统计
    int peopleKilled = 0;
    int timesWasted = 0;            // 死亡次数
    int daysPassed = 0;
    int missionsPassed = 0;
};

// 读取当前玩家状态。只读内存，不修改游戏任何数据。
// 必须在游戏主线程（渲染回调）内调用：SA 的逻辑与渲染同帧串行，
// 此时读取不会与逻辑更新竞争。
PlayerState ReadPlayerState();

// 武器 ID 转中文名。未知 ID 返回 "未知武器"。
const char* WeaponName(int weaponType);

// eWeaponType 中"非武器"的伤害来源转中文名（如 49 车撞 / 53 溺水 / 54 坠落），
// 用于死亡复盘播报。0-46 直接走 WeaponName()。
const char* DamageSourceName(int weaponType);

// 任务脚本代号转标题。未知代号返回 nullptr。
const char* MissionTitle(const char* script);

// 序列化为紧凑 JSON，用于随问题发给 Python 侧。
std::string ToJson(const PlayerState& s);

// ---- 拾取物扫描（地理层数据）----

// CPickup 结构体（plugin-sdk，0x20 字节）
struct Pickup
{
    float            revenueValue;        // +0x00
    void*            pObject;             // +0x04 CObject*
    unsigned int     ammo;                // +0x08
    unsigned int     regenerationTime;    // +0x0C
    // CompressedVector（6 字节 short[3]，坐标 * 8.0f）
    short            x;                   // +0x10
    short            y;                   // +0x12
    short            z;                   // +0x14
    unsigned short   moneyPerDay;         // +0x16
    short            modelIndex;          // +0x18
    short            referenceIndex;      // +0x1A
    unsigned char    pickupType;          // +0x1C
    unsigned char    flags;               // +0x1D（位域：disabled/empty/visible 等）
    char             _pad[2];             // +0x1E

    // 解压坐标
    float GetX() const { return static_cast<float>(x) / 8.0f; }
    float GetY() const { return static_cast<float>(y) / 8.0f; }
    float GetZ() const { return static_cast<float>(z) / 8.0f; }
    bool IsDisabled() const { return (flags & 0x01) != 0; }
    bool IsVisible() const { return (flags & 0x08) != 0; }
};
static_assert(sizeof(Pickup) == 0x20, "CPickup size must be 0x20");

// 拾取物类型（只关心武器、护甲、血包）
enum class PickupKind
{
    Unknown,
    Weapon,
    Armor,
    Health
};

// 拾取物信息（用于 JSON 序列化传给 Python）
struct PickupInfo
{
    PickupKind kind = PickupKind::Unknown;
    float x = 0.0f, y = 0.0f, z = 0.0f;
    int modelId = 0;
    int weaponType = 0;   // 仅武器有效
};

// 扫描当前游戏内所有可见拾取物，按类型筛选。
// 返回 JSON 数组字符串："[{\"kind\":\"weapon\",\"x\":...},...]"
std::string ScanPickups();

// 在地图上设置一个坐标 blip（黄点）。返回 blip 句柄（-1 表示失败）。
// 这是唯一一处会改动游戏状态的调用，需谨慎。
int SetWaypoint(float x, float y, float z);

// 清除指定 blip
void ClearWaypoint(int blipHandle);
}
