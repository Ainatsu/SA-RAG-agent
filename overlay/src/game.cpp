#include "game.h"

#include <cstdio>
#include <cstring>

namespace
{
// ---- CPed 结构偏移（plugin-sdk CPed.h 的 VALIDATE_OFFSET）----
// CPed 继承 CPhysical <- CEntity <- CPlaceable。
// CPlaceable::m_matrix 在 0x14，其中 CMatrix::pos 在 +0x30。
constexpr size_t kPedMatrix       = 0x14;
constexpr size_t kMatrixPos       = 0x30;   // CMatrix::pos (CVector, 3 float)
constexpr size_t kPedPlacementPos = 0x04;   // CPlaceable::m_placement.m_vPosn，
                                            // m_matrix 为空时的退化坐标

constexpr size_t kPedHealth       = 0x540;
constexpr size_t kPedMaxHealth    = 0x544;
constexpr size_t kPedArmour       = 0x548;
constexpr size_t kPedHeading      = 0x558;  // float m_fHeadingCurrent，弧度
constexpr size_t kPedVehicle      = 0x58C;  // CVehicle*，非空表示在载具内
constexpr size_t kPedWeapons      = 0x5A0;  // CWeapon[13]
constexpr size_t kPedSelectedSlot = 0x718;  // unsigned char
constexpr size_t kPedLastWeaponDamage = 0x760;  // eWeaponType m_nLastWeaponDamage
constexpr size_t kPedLastEntityDamage = 0x764;  // CEntity* m_pLastEntityDamage

constexpr size_t kWeaponSize      = 0x1C;   // VALIDATE_SIZE(CWeapon, 0x1C)
constexpr size_t kWeaponType      = 0x00;
constexpr size_t kWeaponAmmoClip  = 0x08;
constexpr size_t kWeaponAmmoTotal = 0x0C;
constexpr int    kWeaponSlots     = 13;

// ---- CPlayerInfo 偏移 ----
constexpr size_t kInfoPed        = 0x00;   // CPlayerPed*
constexpr size_t kInfoPlayerData = 0x04;   // CPlayerData（内联，非指针）
constexpr size_t kInfoMoney      = 0xB8;

// CPlayerData::m_pWanted 在 CPlayerData 起始处（+0x0），
// 故相对 CPlayerInfo 也是 0x04。
constexpr size_t kWantedLevel = 0x2C;      // CWanted::m_nWantedLevel

// SA 的地址空间里，游戏自身的数据都在 4MB 以上、2GB 以下。
// 用它挡掉未初始化的野指针，避免解引用到不可读页面。
bool PlausiblePointer(uintptr_t p)
{
    return p >= 0x00010000 && p < 0x80000000;
}

template <typename T>
T Read(uintptr_t base, size_t offset)
{
    return *reinterpret_cast<T*>(base + offset);
}

float ReadStatFloat(int index)
{
    return *reinterpret_cast<float*>(game::addr::StatTypesFloat + index * 4);
}

int ReadStatInt(int index)
{
    // 整型统计量从索引 82 开始，数组偏移 = (index - 82)
    if (index < 82)
        return 0;
    return *reinterpret_cast<int*>(game::addr::StatTypesInt + (index - 82) * 4);
}

// ---- zone 表 ----
// info.zon / map.zon 的矩形都是轴对齐包围盒，判定即三轴区间包含。
struct Zone
{
    float x1, y1, z1;
    float x2, y2, z2;
    const char* name;
};

#include "zones.inc"

bool Inside(const Zone& z, float x, float y, float zz)
{
    return x >= z.x1 && x <= z.x2 &&
           y >= z.y1 && y <= z.y2 &&
           zz >= z.z1 && zz <= z.z2;
}

// 街区 zone 存在嵌套（如某个院落套在更大的街区里），
// 游戏本身取的是最后一个匹配项，这里保持一致。
const char* FindZone(const Zone* table, size_t count, float x, float y, float z)
{
    const char* found = nullptr;
    for (size_t i = 0; i < count; ++i)
    {
        if (Inside(table[i], x, y, z))
            found = table[i].name;
    }
    return found;
}

// ---- CRunningScript 偏移（plugin-sdk CRunningScript.h 的 VALIDATE_OFFSET）----
constexpr size_t kScriptNext      = 0x00;   // CRunningScript* m_pNext
constexpr size_t kScriptName      = 0x08;   // char m_szName[8]
constexpr size_t kScriptIsMission = 0xDC;   // bool m_bIsMission

struct MissionEntry
{
    const char* script;
    const char* title;
};

#include "missions.inc"

}

namespace game
{
IDirect3DDevice9* GetDevice()
{
    return *reinterpret_cast<IDirect3DDevice9**>(addr::RwD3D9Device);
}

HWND GetWindow()
{
    return *reinterpret_cast<HWND*>(addr::WindowHandle);
}

void SetUserPause(bool paused)
{
    *reinterpret_cast<bool*>(addr::UserPause) = paused;
}

bool GetUserPause()
{
    return *reinterpret_cast<bool*>(addr::UserPause);
}

bool IsSupportedVersion()
{
    // 用指令特征码判版本，而不是比对文件大小——后者容易被重打包蒙混。
    // 这段特征同时验证了 addr::Players 的正确性：指令里就编码着该地址。
    // 只做一次，结果缓存。
    static int cached = -1;
    if (cached >= 0)
        return cached != 0;

    cached = 0;
    __try
    {
        // CWorld::FindPlayerSlotWithPedPointer (0x563FA0) 开头为
        // 8B 54 24 04    mov edx, [esp+4]
        // 33 C0          xor eax, eax
        // B9 98 CD B7 00 mov ecx, offset CWorld::Players
        const unsigned char* p = reinterpret_cast<const unsigned char*>(0x563FA0);
        const unsigned char expect[] = {
            0x8B, 0x54, 0x24, 0x04, 0x33, 0xC0, 0xB9, 0x98, 0xCD, 0xB7, 0x00
        };
        bool ok = true;
        for (size_t i = 0; i < sizeof(expect); ++i)
        {
            if (p[i] != expect[i])
            {
                ok = false;
                break;
            }
        }
        cached = ok ? 1 : 0;
    }
    __except (EXCEPTION_EXECUTE_HANDLER)
    {
        cached = 0;
    }
    return cached != 0;
}

PlayerState ReadPlayerState()
{
    PlayerState s;

    if (!IsSupportedVersion())
        return s;

    __try
    {
        const uintptr_t info = addr::Players;   // CPlayerInfo[0]

        const uintptr_t ped = Read<uintptr_t>(info, kInfoPed);
        if (!PlausiblePointer(ped))
            return s;   // 尚未进入存档 / 正在读盘

        s.health    = Read<float>(ped, kPedHealth);
        s.maxHealth = Read<float>(ped, kPedMaxHealth);
        s.armour    = Read<float>(ped, kPedArmour);

        // 坐标优先取矩阵；ped 未分配矩阵时退回 m_placement 里的位移
        const uintptr_t matrix = Read<uintptr_t>(ped, kPedMatrix);
        const uintptr_t posBase = PlausiblePointer(matrix)
                                    ? matrix + kMatrixPos
                                    : ped + kPedPlacementPos;
        s.x = Read<float>(posBase, 0);
        s.y = Read<float>(posBase, 4);
        s.z = Read<float>(posBase, 8);

        s.money     = Read<int>(info, kInfoMoney);
        s.inVehicle = PlausiblePointer(Read<uintptr_t>(ped, kPedVehicle));

        const uintptr_t wanted = Read<uintptr_t>(info, kInfoPlayerData);
        if (PlausiblePointer(wanted))
            s.wantedLevel = static_cast<int>(Read<unsigned int>(wanted, kWantedLevel));

        // 当前手持武器
        const int slot = Read<unsigned char>(ped, kPedSelectedSlot);
        if (slot >= 0 && slot < kWeaponSlots)
        {
            const uintptr_t w = ped + kPedWeapons + slot * kWeaponSize;
            s.weaponType = Read<int>(w, kWeaponType);
            s.ammoInClip = Read<int>(w, kWeaponAmmoClip);
            s.ammoTotal  = Read<int>(w, kWeaponAmmoTotal);
        }

        // 朝向与最近一次伤害来源（受击来源 / 死亡复盘用）。
        // m_pLastEntityDamage 可能悬空（攻击者已被回收），坐标读取单独包一层
        // __try：失败只丢方向信息，不影响整帧状态。
        s.heading = Read<float>(ped, kPedHeading);
        s.lastDamageWeapon = Read<int>(ped, kPedLastWeaponDamage);

        const uintptr_t lastEntity = Read<uintptr_t>(ped, kPedLastEntityDamage);
        if (PlausiblePointer(lastEntity))
        {
            __try
            {
                const uintptr_t ematrix = Read<uintptr_t>(lastEntity, kPedMatrix);
                const uintptr_t eposBase = PlausiblePointer(ematrix)
                                            ? ematrix + kMatrixPos
                                            : lastEntity + kPedPlacementPos;
                s.lastDamageX = Read<float>(eposBase, 0);
                s.lastDamageY = Read<float>(eposBase, 4);
                s.lastDamageZ = Read<float>(eposBase, 8);
            }
            __except (EXCEPTION_EXECUTE_HANDLER)
            {
                // 攻击者指针已失效，忽略坐标
            }
        }

        s.onMission = *reinterpret_cast<int*>(addr::OnMission) != 0;

        s.zone = FindZone(kZones, sizeof(kZones) / sizeof(kZones[0]), s.x, s.y, s.z);
        s.city = FindZone(kCities, sizeof(kCities) / sizeof(kCities[0]), s.x, s.y, s.z);

        // 当前任务：遍历活动脚本链表，取 m_bIsMission 为真的那个。
        // 同一时刻至多一个任务脚本在跑（CTheScripts::bAlreadyRunningAMissionScript
        // 保证了这点），所以找到即可停。
        if (s.onMission)
        {
            uintptr_t script = *reinterpret_cast<uintptr_t*>(addr::ActiveScripts);
            for (int guard = 0; guard < 128 && PlausiblePointer(script); ++guard)
            {
                if (Read<bool>(script, kScriptIsMission))
                {
                    const char* name = reinterpret_cast<const char*>(script + kScriptName);
                    // m_szName 是定长 8 字节，未必以 NUL 结尾，须手动截断
                    size_t i = 0;
                    for (; i < 8 && name[i]; ++i)
                        s.missionScript[i] = name[i];
                    s.missionScript[i] = '\0';
                    break;
                }
                script = Read<uintptr_t>(script, kScriptNext);
            }
        }

        // 玩家属性
        s.fat       = ReadStatFloat(stat::Fat);
        s.stamina   = ReadStatFloat(stat::Stamina);
        s.muscle    = ReadStatFloat(stat::Muscle);
        s.respect   = ReadStatFloat(stat::Respect);
        s.sexAppeal = ReadStatFloat(stat::SexAppeal);

        // 游戏进度
        s.progressMade  = ReadStatFloat(stat::ProgressMade);
        s.totalProgress = ReadStatFloat(stat::TotalProgress);

        // 行驶距离
        s.distanceOnFoot = ReadStatFloat(stat::DistanceOnFoot);
        s.distanceByCar  = ReadStatFloat(stat::DistanceByCar);
        s.distanceByBike = ReadStatFloat(stat::DistanceByBike);
        s.distanceByBoat = ReadStatFloat(stat::DistanceByBoat);
        s.distanceByHelicopter = ReadStatFloat(stat::DistanceByHelicopter);
        s.distanceByPlane = ReadStatFloat(stat::DistanceByPlane);

        // 整型统计
        s.peopleKilled   = ReadStatInt(stat::PeopleKilled);
        s.timesWasted    = ReadStatInt(stat::TimesWasted);
        s.daysPassed     = ReadStatInt(stat::DaysPassed);
        s.missionsPassed = ReadStatInt(stat::MissionsPassed);

        s.valid = true;
    }
    __except (EXCEPTION_EXECUTE_HANDLER)
    {
        // 读盘/切存档瞬间指针可能失效，放弃本次采样即可
        s.valid = false;
    }

    return s;
}

const char* WeaponName(int weaponType)
{
    switch (weaponType)
    {
    case 0:  return u8"拳头";
    case 1:  return u8"指虎";
    case 2:  return u8"高尔夫球杆";
    case 3:  return u8"警棍";
    case 4:  return u8"小刀";
    case 5:  return u8"棒球棍";
    case 6:  return u8"铁锹";
    case 7:  return u8"台球杆";
    case 8:  return u8"武士刀";
    case 9:  return u8"电锯";
    case 10: return u8"紫色假阳具";
    case 11: return u8"假阳具";
    case 12: return u8"震动棒";
    case 13: return u8"银色震动棒";
    case 14: return u8"花束";
    case 15: return u8"手杖";
    case 16: return u8"手雷";
    case 17: return u8"催泪瓦斯";
    case 18: return u8"燃烧瓶";
    case 22: return u8"手枪";
    case 23: return u8"消音手枪";
    case 24: return u8"沙漠之鹰";
    case 25: return u8"霰弹枪";
    case 26: return u8"短管霰弹枪";
    case 27: return u8"战斗霰弹枪";
    case 28: return u8"Micro Uzi";
    case 29: return u8"MP5";
    case 30: return u8"AK-47";
    case 31: return u8"M4";
    case 32: return u8"Tec-9";
    case 33: return u8"步枪";
    case 34: return u8"狙击步枪";
    case 35: return u8"火箭筒";
    case 36: return u8"热追踪火箭筒";
    case 37: return u8"火焰喷射器";
    case 38: return u8"迷你机枪";
    case 39: return u8"遥控炸弹";
    case 40: return u8"引爆器";
    case 41: return u8"喷漆罐";
    case 42: return u8"灭火器";
    case 43: return u8"相机";
    case 44: return u8"夜视仪";
    case 45: return u8"热成像仪";
    case 46: return u8"降落伞";
    default: return u8"未知武器";
    }
}

const char* DamageSourceName(int weaponType)
{
    // 数值与 gta-reversed eWeaponType.h 一致（49-58 段为非武器伤害来源）
    switch (weaponType)
    {
    case 49: return u8"车辆撞击";
    case 50: return u8"车辆碾压";
    case 51: return u8"爆炸";
    case 52: return u8"驾车枪击";
    case 53: return u8"溺水";
    case 54: return u8"高处坠落";
    case 55: return u8"不明原因";
    default: return WeaponName(weaponType);
    }
}

const char* MissionTitle(const char* script)
{
    if (!script || !script[0])
        return nullptr;

    // 二分查找。kMissionTitles 已按脚本代号排序（gen_mission_titles.py 用 sorted()）。
    int lo = 0;
    int hi = sizeof(kMissionTitles) / sizeof(kMissionTitles[0]) - 1;
    while (lo <= hi)
    {
        int mid = lo + (hi - lo) / 2;
        int cmp = std::strcmp(script, kMissionTitles[mid].script);
        if (cmp < 0)
            hi = mid - 1;
        else if (cmp > 0)
            lo = mid + 1;
        else
            return kMissionTitles[mid].title;
    }
    return nullptr;
}

std::string ToJson(const PlayerState& s)
{
    if (!s.valid)
        return "{}";

    char buf[2300];
    // 坐标保留一位小数即可：Agent 只需知道大致方位，
    // 过高精度反而增加 token 消耗。
    // 距离统计换算成公里，原始单位是米，数值太大不好念。
    _snprintf_s(buf, sizeof(buf), _TRUNCATE,
        "{\"health\":%.0f,\"max_health\":%.0f,\"armour\":%.0f,"
        "\"x\":%.1f,\"y\":%.1f,\"z\":%.1f,"
        "\"zone\":\"%s\",\"city\":\"%s\","
        "\"money\":%d,\"wanted\":%d,"
        "\"in_vehicle\":%s,\"on_mission\":%s,\"mission_script\":\"%s\","
        "\"weapon\":\"%s\",\"weapon_type\":%d,\"ammo_clip\":%d,\"ammo_total\":%d,"
        "\"heading\":%.2f,"
        "\"last_damage_weapon\":%d,\"last_damage_name\":\"%s\","
        "\"last_damage_x\":%.1f,\"last_damage_y\":%.1f,\"last_damage_z\":%.1f,"
        "\"fat\":%.0f,\"stamina\":%.0f,\"muscle\":%.0f,"
        "\"respect\":%.0f,\"sex_appeal\":%.0f,"
        "\"progress\":%.0f,\"total_progress\":%.0f,"
        "\"km_on_foot\":%.1f,\"km_by_car\":%.1f,\"km_by_bike\":%.1f,"
        "\"km_by_boat\":%.1f,\"km_by_heli\":%.1f,\"km_by_plane\":%.1f,"
        "\"people_killed\":%d,\"times_wasted\":%d,"
        "\"days_passed\":%d,\"missions_passed\":%d}",
        s.health, s.maxHealth, s.armour,
        s.x, s.y, s.z,
        s.zone ? s.zone : "", s.city ? s.city : "",
        s.money, s.wantedLevel,
        s.inVehicle ? "true" : "false",
        s.onMission ? "true" : "false",
        s.missionScript,
        WeaponName(s.weaponType), s.weaponType, s.ammoInClip, s.ammoTotal,
        s.heading,
        s.lastDamageWeapon, DamageSourceName(s.lastDamageWeapon),
        s.lastDamageX, s.lastDamageY, s.lastDamageZ,
        s.fat, s.stamina, s.muscle,
        s.respect, s.sexAppeal,
        s.progressMade, s.totalProgress,
        s.distanceOnFoot / 1000.0f, s.distanceByCar / 1000.0f,
        s.distanceByBike / 1000.0f, s.distanceByBoat / 1000.0f,
        s.distanceByHelicopter / 1000.0f, s.distanceByPlane / 1000.0f,
        s.peopleKilled, s.timesWasted,
        s.daysPassed, s.missionsPassed);

    return std::string(buf);
}

// ---- 拾取物扫描与地图标点 ----

namespace
{
// 武器拾取物的模型 ID 连续排布：321..372 对应 eWeaponType 1..46 中的枪械段。
// 与其硬编码全表，不如按 CPickups::ModelForWeapon 的反函数来查：
// 武器模型 ID 与武器类型的对应关系在 SA 里是一张静态表，这里只列出
// 我们会主动推荐的那些（近战和杂项没有寻路价值，一律不报）。
struct WeaponModel
{
    short modelId;
    int   weaponType;
};

constexpr WeaponModel kWeaponModels[] = {
    {331,  1},  // 指虎
    {333,  2},  // 高尔夫球杆
    {334,  3},  // 警棍
    {335,  4},  // 小刀
    {336,  5},  // 棒球棍
    {337,  6},  // 铁锹
    {338,  7},  // 台球杆
    {339,  8},  // 武士刀
    {341,  9},  // 电锯
    {342, 16},  // 手雷
    {343, 17},  // 催泪瓦斯
    {344, 18},  // 燃烧瓶
    {346, 22},  // 手枪
    {347, 23},  // 消音手枪
    {348, 24},  // 沙漠之鹰
    {349, 25},  // 霰弹枪
    {350, 26},  // 短管霰弹枪
    {351, 27},  // 战斗霰弹枪
    {352, 28},  // Micro Uzi
    {353, 29},  // MP5
    {355, 30},  // AK-47
    {356, 31},  // M4
    {372, 32},  // Tec-9
    {357, 33},  // 步枪
    {358, 34},  // 狙击步枪
    {359, 35},  // 火箭筒
    {360, 36},  // 热追踪火箭筒
    {361, 37},  // 火焰喷射器
    {362, 38},  // 迷你机枪
    {363, 39},  // 遥控炸弹
    {365, 41},  // 喷漆罐
    {366, 42},  // 灭火器
    {367, 43},  // 相机
    {368, 44},  // 夜视仪
    {369, 45},  // 热成像仪
    {371, 46},  // 降落伞
};

// 护甲和血包是固定模型，不属于武器表
constexpr short kModelArmour = 1242;
constexpr short kModelHealth = 1240;

int WeaponTypeForModel(short modelId)
{
    for (const WeaponModel& m : kWeaponModels)
        if (m.modelId == modelId)
            return m.weaponType;
    return -1;
}
}

std::string ScanPickups()
{
    if (!IsSupportedVersion())
        return "[]";

    const Pickup* arr = reinterpret_cast<const Pickup*>(addr::Pickups);

    std::string out = "[";
    char item[192];
    int count = 0;

    for (int i = 0; i < addr::MaxPickups; ++i)
    {
        const Pickup& p = arr[i];

        // 槽位空闲或正在等待重生的一律跳过：报一个已经被捡走的点
        // 比不报更糟，玩家跑过去会扑空。
        if (p.pickupType == 0 || p.IsDisabled())
            continue;

        const char* kind = nullptr;
        int weapon = 0;

        if (p.modelIndex == kModelArmour)
        {
            kind = "armor";
        }
        else if (p.modelIndex == kModelHealth)
        {
            kind = "health";
        }
        else
        {
            weapon = WeaponTypeForModel(p.modelIndex);
            if (weapon < 0)
                continue;   // 钱、收集品、房产图标等，寻路用不上
            kind = "weapon";
        }

        _snprintf_s(item, sizeof(item), _TRUNCATE,
            "%s{\"kind\":\"%s\",\"x\":%.1f,\"y\":%.1f,\"z\":%.1f,"
            "\"model\":%d,\"weapon\":%d,\"name\":\"%s\",\"ammo\":%u,"
            "\"zone\":\"%s\"}",
            count ? "," : "", kind,
            p.GetX(), p.GetY(), p.GetZ(),
            static_cast<int>(p.modelIndex), weapon,
            weapon > 0 ? WeaponName(weapon) : "",
            p.ammo,
            FindZone(kZones, sizeof(kZones) / sizeof(kZones[0]),
                     p.GetX(), p.GetY(), p.GetZ()));

        out += item;
        ++count;
    }

    out += "]";
    return out;
}

int SetWaypoint(float x, float y, float z)
{
    if (!IsSupportedVersion())
        return -1;

    // CVector 按值传参，在 __cdecl 下是三个 float 依次压栈
    using SetCoordBlipFn = int(__cdecl*)(int, float, float, float,
                                         unsigned int, int, const char*);
    auto setCoordBlip = reinterpret_cast<SetCoordBlipFn>(addr::RadarSetCoordBlip);

    // BLIP_COORD = 4，BLIP_DISPLAY_BOTH = 3（雷达和大地图都画）
    const int handle = setCoordBlip(4, x, y, z, 0, 3, nullptr);
    if (handle <= 0)
        return -1;

    // RADAR_SPRITE_WAYPOINT = 41，画成和玩家自己标的路点一个样子，
    // 玩家一眼就知道那是"去这里"而不是任务点。
    using SetBlipSpriteFn = void(__cdecl*)(int, int);
    reinterpret_cast<SetBlipSpriteFn>(addr::RadarSetBlipSprite)(handle, 41);

    return handle;
}

void ClearWaypoint(int blipHandle)
{
    if (blipHandle <= 0 || !IsSupportedVersion())
        return;

    using ClearBlipFn = void(__cdecl*)(int);
    reinterpret_cast<ClearBlipFn>(addr::RadarClearBlip)(blipHandle);
}
}
