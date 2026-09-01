# SA Agent

一个运行在《Grand Theft Auto: San Andreas》游戏内的中文攻略与语音助手。

SA Agent 由两部分组成：

- `overlay/`：C++ ASI 插件，在游戏内绘制 ImGui 面板、读取玩家状态，并与服务端通信。
- `service/`：Python 服务端，负责 RAG 检索、DeepSeek 问答、Whisper 语音识别和 Windows SAPI 语音播报。

项目的目标是让玩家在游戏过程中直接提问“这关怎么过”“最近的防弹衣在哪”等问题，同时在血量骤降、护甲破碎、通缉升级、任务完成、死亡等事件发生时进行语音提醒。

## 功能

- 游戏内中文文字问答，支持流式显示回答
- 读取玩家状态：血量、护甲、武器、弹药、位置、区域、任务、通缉等级、金钱和部分统计数据
- GTA Wiki 英文资料的 BM25 + 向量混合检索
- DeepSeek 查询改写与回答生成，针对中文口语和语音识别错字做了别名处理
- 按住左 `Ctrl` 说话，识别结果回填到游戏内输入框
- 按住鼠标侧键进行实时语音问答，回答直接朗读
- 说出唤醒词“**小龟J**”发起语音提问
- 事件播报：低血量、护甲耗尽、受击方向、通缉变化、弹药耗尽、任务完成、死亡复盘等
- 查询附近武器、防弹衣和血包，并在游戏雷达上标记最近位置
- 无麦克风、关闭唤醒词或向量索引不可用时，仍可使用文字问答或 BM25 检索

## 环境要求

- Windows
- 适用于 **GTA: San Andreas 1.0 US (HOODLUM)** 的 `gta_sa.exe`
- 已安装 ASI Loader（插件以 `.asi` 形式加载）
- Python 3.10+（建议使用 64 位 Python；游戏插件本身必须编译为 Win32/x86）
- Visual Studio 2022，包含 C++ 桌面开发、MSVC x86 工具链、CMake 和 Ninja
- 可用的麦克风和扬声器（语音功能需要）
- DeepSeek API Key（问答和查询改写需要）

> 重要：覆盖层内存地址只针对 GTA:SA 1.0 US (HOODLUM)。其他版本会被版本检查拦截，状态读取和唤醒词不会工作。请勿将本项目用于盗版游戏或绕过正版验证。

## 安装

### 1. 获取 Python 服务

```bat
cd sa-agent
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
```

在项目根目录创建 `.env`，写入 DeepSeek 密钥：

```dotenv
DEEPSEEK_API_KEY=你的密钥
```

`.env` 已被 `.gitignore` 忽略，请不要提交到 GitHub。

### 2. 构建覆盖层

使用 **x86 Native Tools Command Prompt for VS**，或确保当前终端能够找到 x86 MSVC 工具链：

```bat
cd sa-agent\overlay
cmake -S . -B build -G Ninja
cmake --build build --config Release
```

也可以直接运行 `overlay\build.bat`。该脚本中的 Visual Studio 路径是本机配置，换电脑时请先修改 `VSROOT`。

构建产物为：

```text
overlay\build\SAAgentOverlay.asi
```

将 `SAAgentOverlay.asi` 放入 GTA:SA 游戏目录中 ASI Loader 会扫描的目录（通常是游戏根目录或 `scripts\`，以你的 ASI Loader 配置为准）。

### 3. 启动服务

先启动 Python 服务，再启动游戏：

```bat
cd sa-agent
.venv\Scripts\python.exe service\server.py
```

或者运行：

```bat
service\start.bat
```

服务默认监听 `127.0.0.1:51678`。看到“等待游戏内覆盖层连接”后即可启动游戏。

首次启动可能需要下载 Whisper 和 FastEmbed 模型，请耐心等待。模型会缓存在本机，后续启动会更快。

## 游戏内操作

覆盖层默认使用纯键盘操作，因为 GTA:SA 的 DirectInput 独占鼠标可能导致鼠标消息不可用。

| 操作 | 功能 |
| --- | --- |
| `` ` `` | 打开或关闭问答面板 |
| `Enter` | 发送问题 |
| 按住左 `Ctrl` | 录音，松开后将识别文本填入输入框 |
| `F1` | 展开或收起玩家状态面板 |
| `PageUp` / `PageDown` | 翻阅回答记录 |
| `Esc` | 关闭覆盖层 |
| 鼠标侧键 | 按住进行实时语音问答 |
| 说“**小龟J**” | 唤醒语音助手（仅游戏进入存档后启用） |

## 配置

所有配置通过环境变量设置，也可以写在项目根目录 `.env` 中：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | 无 | DeepSeek API 密钥 |
| `SA_AGENT_ORCHESTRATOR` | `legacy` | 问答编排器，可设为 `langgraph` 启用 LangGraph 图 |
| `SA_WAKE` | `1` | 设为 `0` 关闭“小龟J”唤醒监听 |
| `SA_WHISPER_MODEL` | `small` | 提问语音识别模型 |
| `SA_WHISPER_DEVICE` | `cpu` | Whisper 推理设备 |
| `SA_WAKE_MODEL` | `tiny` | 唤醒词扫描模型；识别不准时可改为 `small` |
| `SA_WAKE_LOG` | `1` | 是否输出唤醒扫描日志 |
| `SA_VAD_RMS` | `0.012` | 静音检测阈值，按麦克风增益调整 |

## 数据与 RAG

`data/` 中已包含项目运行所需的索引和自定义知识：

- `chunks.jsonl`：清洗后的 GTA Wiki 文本块
- `vectors.npy` / `vectors.meta.json`：FastEmbed 向量索引
- `custom.jsonl`：项目手写的攻略、位置和补充资料

如需重新抓取或构建知识库，可按以下顺序运行：

```bat
python rag\fetch_wiki.py
python rag\clean_wiki.py
python rag\embed.py
```

抓取 Wiki 需要网络连接；向量模型首次运行也会下载模型。详细的数据字段和架构说明见 `docs/`。

## 项目结构

```text
sa-agent/
├─ overlay/       C++ ASI 覆盖层、游戏内存读取、ImGui UI
├─ service/       TCP 服务端、语音识别、语音播报
├─ rag/           Wiki 清洗、检索、向量索引和回答管线
├─ data/          Wiki 索引与自定义知识
├─ tools/         区域和任务名称等数据生成工具
├─ docs/          架构、事件字段和功能规划
└─ test_*.py      语音、会话和服务链路测试脚本
```

## 测试

测试脚本是独立的端到端检查工具：

```bat
python test_speech.py
python test_voice.py
python test_live.py
python test_wake.py
python test_session.py
```

其中部分测试需要麦克风、扬声器或已启动的服务端；离线环境下可优先运行 `test_speech.py` 和 `test_session.py`。

## 已知限制

- 仅支持 GTA:SA 1.0 US (HOODLUM)，其他版本需要重新核对内存地址和版本特征码。
- 仅支持 Windows；TTS 使用 Windows SAPI/COM，中文输入还依赖游戏的 GBK/MBCS 行为。
- DeepSeek API 不可用时，LLM 问答和查询改写不可用；本地事件播报不依赖 LLM。
- 唤醒词监听只在覆盖层上报有效游戏状态、且真正进入存档后开启。
- 本项目目前不提供完整任务分步导航、自动寻路、载具故障告警或多人格对话。

## 贡献

欢迎提交 Issue 和 Pull Request。涉及游戏内存地址、ASI Hook、协议帧或线程同步的修改，请同时说明适用的游戏版本、复现步骤和测试结果。

## 许可与第三方组件

仓库当前未声明统一的项目许可证。提交代码前请确认你有权使用相关游戏数据和资源。项目内包含 Dear ImGui 与 MinHook 等第三方组件，它们分别遵循各自目录中的许可证文件。
