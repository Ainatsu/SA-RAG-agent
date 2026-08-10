# 圣安地列斯 Agent 事件/字段设计表

> 字段命名建议采用 `snake_case`，事件通过 JSON 消息推送给 agent，例如：
> `{"event": "health_low", "value": 18, "timestamp": 1723000000}`

---

## 1. 生命/战斗状态

| 字段名 | 类型 | 数据来源(内存/接口) | 触发条件 | 示例语音文案 |
|---|---|---|---|---|
| `health` | float (0-100) | 玩家结构体 Health | 持续上报，供内部判断 | — |
| `health_low` | event | health < 30 | 跨越阈值时触发一次 | "血量告急，先找地方补血" |
| `health_critical` | event | health < 10 | 跨越阈值 | "命悬一线了，赶紧撤退！" |
| `armor` | float (0-100) | Armor 字段 | 持续上报 | — |
| `armor_depleted` | event | armor 从>0变为0 | 边沿触发 | "护甲没了，小心点" |
| `armor_pickup` | event | armor 突增 | 边沿触发 | "补到护甲了" |
| `current_weapon` | enum | Weapon slot | 切换武器时 | 可选：报武器名 |
| `weapon_ammo` | int | 当前弹匣/备用弹药 | 持续上报 | — |
| `ammo_empty` | event | ammo == 0 且非近战 | 边沿触发 | "没子弹了，换武器！" |
| `weapon_switch` | event | 武器slot变化 | 边沿触发 | 特殊武器可加评论，如火箭筒："小心别炸到自己" |
| `hit_taken` | event | 受到伤害瞬间 | 每次伤害/或短时间聚合 | 连续被同一来源命中："背后有人！" |
| `headshot_taken` | event | 受到爆头伤害 | 边沿触发 | "刚才差点被爆头" |
| `player_wasted` | event | 死亡状态位 | 边沿触发 | 播报死因(若可获取)+死亡次数统计 |
| `player_busted` | event | 被捕状态位 | 边沿触发 | "被抓了，装备清空" |

---

## 2. 任务状态

| 字段名 | 类型 | 数据来源 | 触发条件 | 示例语音文案 |
|---|---|---|---|---|
| `mission_id` / `mission_name` | string | 任务脚本变量 | 任务开始 | "任务开始：{name}" |
| `mission_status` | enum(active/success/fail) | 任务结果标志 | 状态变化 | — |
| `mission_success` | event | status→success | 边沿触发 | "任务完成！" |
| `mission_fail` | event | status→fail | 边沿触发 | 附带原因（若可读取），如"载具损毁，任务失败" |
| `mission_timer_remaining` | int(秒) | 任务内计时器地址 | 持续上报 | 剩余<10秒时口播倒计时 |
| `checkpoint_reached` | event | Checkpoint计数变化 | 边沿触发 | "阶段目标达成，继续" |
| `mission_objective_text` | string | 当前目标提示文本 | 目标变化时 | 复述当前目标 |
| `sidemission_available` | bool | 地图支线标记状态 | 玩家进入区域附近 | "这附近还有支线任务没做" |

---

## 3. 角色属性（SA RPG系统）

| 字段名 | 类型 | 数据来源 | 触发条件 | 示例语音文案 |
|---|---|---|---|---|
| `stat_driving` | int(0-1000) | Skill stats数组 | 数值提升 | "驾驶技能提升了" |
| `stat_shooting` | int | 同上 | 数值提升 | "枪法变准了" |
| `stat_stamina`（肺活量） | int | 同上 | 数值提升/耐力耗尽 | 疾跑/游泳耐力见底提示 |
| `stat_muscle` | int | 同上 | 明显变化 | — |
| `stat_fat` | int | 同上 | 明显上升 | "该去健身房了" |
| `stat_sexappeal` | int | 同上 | 变化 | 可选彩蛋反馈 |
| `stat_respect`（帮派声望） | int | 同上 | 提升/下降 | "你在帮派里的地位提高了" |

---

## 4. 载具相关

| 字段名 | 类型 | 数据来源 | 触发条件 | 示例语音文案 |
|---|---|---|---|---|
| `vehicle_health` | float | Vehicle结构体 | 持续上报 | — |
| `vehicle_on_fire` | event | 状态位 | 边沿触发 | "车着火了，快下车！" |
| `vehicle_flipped` | event | 姿态角判断(翻车) | 边沿触发 | "车翻了" |
| `tire_burst` | event | 轮胎状态数组 | 某轮位破损 | "爆胎了，注意操控" |
| `vehicle_type` | enum | 当前载具类别 | 上车/换车 | 匹配语气(自行车vs跑车vs飞机) |
| `vehicle_speed` | float | 速度矢量计算 | 持续上报 | 超速+被通缉时联动提醒 |
| `player_ejected` | event | 脱离载具/被甩出 | 边沿触发 | "你被甩出车外了" |

---

## 5. 通缉等级 & 警察系统

| 字段名 | 类型 | 数据来源 | 触发条件 | 示例语音文案 |
|---|---|---|---|---|
| `wanted_level` | int(0-6) | Wanted stars 变量 | 数值变化 | 升星："警察盯上你了" |
| `wanted_increased` | event | 边沿(增) | 边沿触发 | "通缉等级上升到{n}星" |
| `wanted_cleared` | event | 边沿(降为0) | 边沿触发 | "已经甩掉警察了" |
| `nearby_police` | bool/int | 附近警车/警员计数(需实体扫描) | 数量变化 | "附近有警车" |
| `police_helicopter_nearby` | bool | 实体类型扫描 | 出现 | "天上有直升机在追你" |

---

## 6. 环境/世界状态

| 字段名 | 类型 | 数据来源 | 触发条件 | 示例语音文案 |
|---|---|---|---|---|
| `game_time` | string(HH:MM) | 游戏内时钟变量 | 定时上报 | 可选播报 |
| `weather_id` | enum | 天气变量 | 变化时 | "开始下雨了" |
| `current_zone` | string | Zone查表(按坐标) | 进入新区域 | "进入Grove Street" |
| `gang_territory` | enum | 帮派地盘数据表+坐标 | 进入不同帮派地盘 | "这是敌对帮派地盘，小心" |
| `nearby_poi` | list | 预置兴趣点坐标库 | 距离<阈值 | "附近有健身房/枪店" |

---

## 7. 经济/资产

| 字段名 | 类型 | 数据来源 | 触发条件 | 示例语音文案 |
|---|---|---|---|---|
| `player_money` | int | Money变量 | 持续上报 | — |
| `money_change` | event | 变化量计算 | 变化超过阈值 | 大额增加/减少时播报 |
| `safehouse_owned` | list | 存档点/房产标志数组 | 购买时 | "买下了新的安全屋" |
| `outfit_changed` | event | 服装ID变化 | 边沿触发 | 可选吐槽新造型 |

---

## 8. 收集品 & 地图标点（对应你说的"标点"功能）

| 字段名 | 类型 | 数据来源 | 触发条件 | 示例语音文案 |
|---|---|---|---|---|
| `player_position` | vec3 | 坐标寄存器 | 持续上报(高频) | 用于所有距离判断 |
| `collectible_db` | 预置数据集 | 静态坐标表(319 tags/50马蹄铁/50牡蛎/50拍照点/特技跳跃点等) | 加载一次 | — |
| `collectible_nearby` | event | 玩家坐标与collectible_db距离<N米 且未完成 | 边沿触发 | "附近有一个喷漆涂鸦还没喷" |
| `collectible_completed` | event | 对应完成计数增加 | 边沿触发 | "拿到了，还差{n}个马蹄铁" |
| `collection_progress` | dict | 完成计数/总数 | 查询时或阶段性播报 | "涂鸦完成度：45/100" |

---

## 9. 社交/剧情（进阶，可选）

| 字段名 | 类型 | 数据来源 | 触发条件 | 示例语音文案 |
|---|---|---|---|---|
| `girlfriend_affection` | int | 好感度变量(每个女友独立) | 明显变化 | 提示约会时间到 |
| `key_npc_trigger` | event | 剧情脚本标志位 | 特定剧情点 | 播报剧情提示或彩蛋评论 |

---

## 数据管线建议

```
游戏进程内存/CLEO脚本
        │  (读取上述字段，按频率轮询或事件回调)
        ▼
本地数据采集层 (CLEO -> Pipe/Socket -> 本地程序)
        │  (打包成JSON事件流)
        ▼
Agent 事件总线 (规则引擎: 阈值/边沿检测 + 去重/节流)
        │
        ├─► 规则触发 → 直接播报固定文案（低延迟，如"没子弹了"）
        └─► 复杂场景 → 丢给LLM生成个性化反馈 → TTS输出
```

**节流建议**：`health`、`player_position`、`vehicle_speed` 等高频字段只用于内部判断，不直接触发语音；语音只由**边沿事件**（`_low`、`_empty`、`_nearby`等）触发，避免刷屏式播报。

---

需要我针对某几类（比如收集品坐标库、或CLEO内存读取的具体字段地址）再往下细化吗？
