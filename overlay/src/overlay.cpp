#include "overlay.h"
#include "game.h"
#include "log.h"
#include "client.h"

#include <d3d9.h>
#include <MinHook.h>
#include <imm.h>

#include <imgui.h>
#include <imgui_impl_dx9.h>
#include <imgui_impl_win32.h>

#include <string>

extern IMGUI_IMPL_API LRESULT ImGui_ImplWin32_WndProcHandler(HWND, UINT, WPARAM, LPARAM);

namespace
{
// IDirect3DDevice9 vtable 下标（IUnknown 3 项 + 设备方法顺序）
constexpr size_t kIdxReset    = 16;
constexpr size_t kIdxEndScene = 42;

using ResetFn    = HRESULT(__stdcall*)(IDirect3DDevice9*, D3DPRESENT_PARAMETERS*);
using EndSceneFn = HRESULT(__stdcall*)(IDirect3DDevice9*);

ResetFn    g_realReset    = nullptr;
EndSceneFn g_realEndScene = nullptr;

IDirect3DDevice9* g_device = nullptr;
HWND              g_window = nullptr;
WNDPROC           g_realWndProc = nullptr;

bool g_imguiReady    = false;
bool g_overlayOpen   = false;
bool g_pausedByUs    = false;
bool g_focusOnInput  = false;   // 打开当帧把焦点给输入框
bool g_showState     = true;    // 玩家状态面板是否展开（F1 切换）
bool g_recording     = false;   // 正在录音（左 Ctrl 按住说话）
bool g_liveTalking   = false;   // 实时语音问答录音中（鼠标侧键按住说话）

// 状态心跳：每隔一段时间主动推一帧玩家状态给服务端，供事件检测使用
DWORD g_lastHeartbeat = 0;
constexpr DWORD kHeartbeatInterval = 3000;  // 3 秒推一次

// 拾取物清单：整包几百个点，且武器/防弹衣刷新很慢，不必跟心跳同频
DWORD g_lastPickupScan = 0;
constexpr DWORD kPickupScanInterval = 15000;  // 15 秒扫一次

// 上一次插的标点。同一时刻只留一个，插新的之前先把旧的清掉，
// 否则雷达上会越积越多。
int g_waypointBlip = -1;

char g_inputBuf[1024] = "";

// 待插入输入框的语音识别结果。InputText 激活时有自己的内部缓冲，帧末会把它
// 写回 g_inputBuf，所以不能直接改 g_inputBuf——那样会被覆盖掉。只能借
// CallbackAlways 在控件内部插入。
std::string g_pendingVoiceText;

int InputTextCallback(ImGuiInputTextCallbackData* data)
{
    if (data->EventFlag == ImGuiInputTextFlags_CallbackAlways && !g_pendingVoiceText.empty())
    {
        data->InsertChars(data->CursorPos, g_pendingVoiceText.c_str());
        g_pendingVoiceText.clear();
    }
    return 0;
}

// 把语音识别结果追加到输入框。追加而不是覆盖，玩家可以先打一半再补一句话，
// 也能连说两段拼成一个问题。
void AppendToInput(const std::string& text)
{
    if (text.empty())
        return;

    g_pendingVoiceText += text;
}

// 游戏启动时通常调用 ImmAssociateContext(hwnd, nullptr) 禁用输入法，
// 否则玩家打字会误触发游戏操作。覆盖层需要输入中文，所以打开时恢复
// 输入法上下文，关闭时再摘掉，保持游戏原有行为。
HIMC g_savedImc = nullptr;
bool g_imeRestored = false;

// 取 COM 对象 vtable 中第 index 个方法的函数地址。
// 注意必须返回函数本身的地址，而不是槽位地址——MinHook 要的是前者。
void* VTableEntry(void* obj, size_t index)
{
    void** vtbl = *reinterpret_cast<void***>(obj);
    return vtbl[index];
}

void OpenOverlay()
{
    if (g_overlayOpen)
        return;
    g_overlayOpen  = true;
    g_focusOnInput = true;

    // 暂停游戏逻辑。游戏仍会继续出帧，所以覆盖层可见可交互。
    // 只有当游戏原本没在暂停时才由我们接管，避免关闭时误清玩家自己的暂停。
    if (!game::GetUserPause())
    {
        game::SetUserPause(true);
        g_pausedByUs = true;
    }

    // 恢复输入法。游戏可能已解除窗口与输入法上下文的关联，
    // 此时 ImmGetContext 返回 null，需要新建一个上下文挂上去。
    if (g_window && !g_imeRestored)
    {
        HIMC existing = ::ImmGetContext(g_window);
        if (existing == nullptr)
        {
            if (g_savedImc == nullptr)
                g_savedImc = ::ImmCreateContext();
            if (g_savedImc)
            {
                ::ImmAssociateContext(g_window, g_savedImc);
                g_imeRestored = true;
            }
        }
        else
        {
            ::ImmReleaseContext(g_window, existing);
        }
    }
}

void CloseOverlay()
{
    if (!g_overlayOpen)
        return;
    g_overlayOpen = false;

    // 录音期间关闭覆盖层时补一个 stop，否则服务端的麦克风会一直开着。
    // DrawUI 不再执行，松开 Ctrl 也不会被察觉。
    if (g_recording)
    {
        client::SendVoice("stop");
        g_recording = false;
    }

    if (g_pausedByUs)
    {
        game::SetUserPause(false);
        g_pausedByUs = false;
    }

    // 关闭时摘掉输入法上下文，恢复游戏原有行为
    if (g_window && g_imeRestored)
    {
        ::ImmAssociateContext(g_window, nullptr);
        g_imeRestored = false;
    }
}

// 实时语音问答的按键轮询。必须放在渲染回调里而不是 WndProc：
// SA 用 DirectInput 独占鼠标，WM_XBUTTON* 根本到不了窗口过程。
// 这里同时也是游戏主线程，满足 ReadPlayerState 的调用约束。
void PollLiveKey()
{
    // 侧键 1、侧键 2 都认，哪个顺手用哪个
    const bool down = (::GetAsyncKeyState(VK_XBUTTON1) & 0x8000) != 0
                   || (::GetAsyncKeyState(VK_XBUTTON2) & 0x8000) != 0;

    if (down && !g_liveTalking && !g_overlayOpen)
    {
        game::PlayerState ps = game::ReadPlayerState();
        if (client::SendLive("start", game::ToJson(ps)))
            g_liveTalking = true;
    }
    else if (!down && g_liveTalking)
    {
        g_liveTalking = false;
        client::SendLive("stop", "");
    }

    // 状态心跳：定期推送玩家状态供服务端事件检测
    DWORD now = ::GetTickCount();
    if (now - g_lastHeartbeat >= kHeartbeatInterval)
    {
        g_lastHeartbeat = now;
        game::PlayerState ps = game::ReadPlayerState();
        if (ps.valid)
            client::SendLive("heartbeat", game::ToJson(ps));
    }

    // 拾取物清单：定期扫描并推给服务端，供"最近的防弹衣在哪"查询。
    // 扫描本身读内存，必须在游戏主线程做；推送只是投队列，立即返回。
    if (now - g_lastPickupScan >= kPickupScanInterval)
    {
        g_lastPickupScan = now;
        std::string json = game::ScanPickups();
        if (!json.empty())
            client::SendPickups(json);
    }

    // 标点请求：服务端听到"最近的防弹衣在哪"会算出坐标并下发 W 帧，
    // 渲染线程取出来调 SetWaypoint，把旗子插到雷达上。
    float x = 0.0f, y = 0.0f, z = 0.0f;
    if (client::TakeWaypoint(&x, &y, &z))
    {
        // 先清掉上一个，同一时刻只留一个标点
        if (g_waypointBlip >= 0)
        {
            game::ClearWaypoint(g_waypointBlip);
            g_waypointBlip = -1;
        }
        g_waypointBlip = game::SetWaypoint(x, y, z);
        if (g_waypointBlip < 0)
            agentlog::Write("[overlay] 标点失败: (%.1f, %.1f, %.1f)", x, y, z);
    }
}

// 实时语音问答的画面提示。不依赖覆盖层，游戏正常跑着的时候也要能看见，
// 所以单独画一个无边框、不吃输入的小窗口贴在屏幕上方。
//
// 状态取值见 server.py 的协议说明。说唤醒词触发的那一路也走这里——那时
// g_liveTalking 是 false，提示全靠服务端主动推来的 P 帧。
void DrawLiveHud()
{
    std::string status;
    client::LiveStatus(&status);

    // 识别出的问题只推一次（q: 帧），但要显示到这一轮结束：玩家听着回答的
    // 时候才最需要对照它到底听成了什么。新一轮一开口就清掉——idle 之后
    // 整个 HUD 都不再绘制，只靠状态清空的话上一轮的问题会留到下一轮。
    static std::string s_heard;
    if (status.empty() || status == "recording" || status == "listening")
        s_heard.clear();
    else if (status.rfind("q:", 0) == 0)
        s_heard = status.substr(2);

    const char* text = nullptr;
    ImVec4 color;

    if (g_liveTalking || status == "recording" || status == "listening")
    {
        text  = u8"\u25CF \u542C\u7740\u5462\u2026";       // ● 听着呢…
        color = ImVec4(1.0f, 0.35f, 0.35f, 1.0f);
    }
    else if (status == "thinking" || status.rfind("q:", 0) == 0)
    {
        text  = u8"\u601D\u8003\u4E2D\u2026";              // 思考中…
        color = ImVec4(1.0f, 0.85f, 0.4f, 1.0f);
    }
    else if (status == "speaking")
    {
        text  = u8"\u56DE\u7B54\u4E2D\u2026";              // 回答中…
        color = ImVec4(0.5f, 0.9f, 1.0f, 1.0f);
    }

    if (!text)
        return;

    const ImGuiViewport* vp = ImGui::GetMainViewport();
    const float maxWidth = vp->WorkSize.x * 0.6f;

    ImGui::SetNextWindowPos(
        ImVec2(vp->WorkPos.x + vp->WorkSize.x * 0.5f, vp->WorkPos.y + 60.0f),
        ImGuiCond_Always, ImVec2(0.5f, 0.0f));
    ImGui::SetNextWindowBgAlpha(0.55f);

    const ImGuiWindowFlags flags =
        ImGuiWindowFlags_NoDecoration | ImGuiWindowFlags_NoInputs |
        ImGuiWindowFlags_NoNav | ImGuiWindowFlags_NoSavedSettings |
        ImGuiWindowFlags_AlwaysAutoResize | ImGuiWindowFlags_NoFocusOnAppearing;

    if (ImGui::Begin("##sa_live_hud", nullptr, flags))
    {
        // 问题可能很长，限宽换行，别横着糊满整个屏幕
        ImGui::PushTextWrapPos(maxWidth);

        ImGui::PushStyleColor(ImGuiCol_Text, color);
        ImGui::TextUnformatted(text);
        ImGui::PopStyleColor();

        // 识别结果放暗一点的第二行，和阶段提示主次分明
        if (!s_heard.empty())
        {
            const std::string echo = u8"听到：" + s_heard;  // 听到：
            ImGui::TextDisabled("%s", echo.c_str());
        }

        ImGui::PopTextWrapPos();
    }
    ImGui::End();
}

void DrawUI()
{
    // 鼠标不可用，窗口无法拖动或缩放，因此固定居中并禁用相关交互。
    const ImVec2 screen = ImGui::GetIO().DisplaySize;
    ImGui::SetNextWindowPos(ImVec2(screen.x * 0.5f, screen.y * 0.5f),
                            ImGuiCond_Always, ImVec2(0.5f, 0.5f));
    ImGui::SetNextWindowSize(ImVec2(660, 420), ImGuiCond_Always);
    ImGui::SetNextWindowBgAlpha(0.92f);
    ImGui::Begin(u8"SA Agent - 攻略助手", nullptr,
                 ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoSavedSettings |
                 ImGuiWindowFlags_NoMove | ImGuiWindowFlags_NoResize);

    // --- 顶部状态栏 ---
    const client::State st = client::GetState();
    const ImVec4 color = (st == client::State::Connected) ? ImVec4(0.4f, 0.85f, 0.4f, 1.0f)
                       : (st == client::State::Waiting ||
                          st == client::State::Voice)     ? ImVec4(0.95f, 0.8f, 0.3f, 1.0f)
                                                          : ImVec4(0.9f, 0.45f, 0.45f, 1.0f);
    ImGui::TextDisabled(u8"服务:");
    ImGui::SameLine();
    ImGui::TextColored(color, "%s", client::StateText());
    ImGui::SameLine();
    ImGui::TextDisabled(u8"| 游戏: %s", game::GetUserPause() ? u8"已暂停" : u8"运行中");

    // --- 键盘操作 ---
    // 游戏用 DirectInput 独占鼠标并每帧把光标锁回屏幕中心，覆盖层里鼠标不可用，
    // 所以所有交互都走键盘。
    if (ImGui::IsKeyPressed(ImGuiKey_F1, false))
        g_showState = !g_showState;

    // --- 按住左 Ctrl 说话 ---
    // 录音在服务端进行，这里只负责发启停指令。用左 Ctrl 是因为修饰键不产生
    // 字符，按住时不会往输入框里打字。
    const bool ctrlDown = ImGui::IsKeyDown(ImGuiKey_LeftCtrl);
    if (ctrlDown && !g_recording && st == client::State::Connected)
    {
        if (client::SendVoice("start"))
            g_recording = true;
    }
    else if (!ctrlDown && g_recording)
    {
        client::SendVoice("stop");
        g_recording = false;
    }

    // 识别结果回来后填进输入框，并把焦点还给它，玩家可以直接改或按 Enter 发送
    std::string transcript;
    if (client::TakeTranscript(&transcript))
    {
        AppendToInput(transcript);
        g_focusOnInput = true;
    }

    // --- 玩家状态面板（F1 开合）---
    // 覆盖层打开时游戏已暂停，现读即可，不必降频采样。
    const game::PlayerState ps = game::ReadPlayerState();
    if (g_showState)
    {
        ImGui::Separator();
        if (!ps.valid)
        {
            ImGui::TextDisabled(u8"状态读取失败：游戏版本不匹配，或尚未进入存档。");
        }
        else
        {
            ImGui::Text(u8"血量 %.0f / %.0f    护甲 %.0f",
                        ps.health, ps.maxHealth, ps.armour);
            ImGui::Text(u8"位置 %s%s%s  (%.0f, %.0f, %.0f)",
                        ps.zone ? ps.zone : u8"未知区域",
                        (ps.zone && ps.city) ? u8" / " : u8"",
                        ps.city ? ps.city : u8"",
                        ps.x, ps.y, ps.z);
            ImGui::Text(u8"金钱 $%d    通缉 %d 星    %s    %s",
                        ps.money, ps.wantedLevel,
                        ps.inVehicle ? u8"车内" : u8"步行",
                        ps.onMission ? u8"任务中" : u8"自由活动");
            if (ps.onMission)
            {
                const char* title = game::MissionTitle(ps.missionScript);
                if (title)
                    ImGui::Text(u8"任务 %s", title);
                else if (ps.missionScript[0])
                    ImGui::Text(u8"任务脚本 %s（未识别）", ps.missionScript);
                else
                    ImGui::Text(u8"任务中（脚本未识别）");
            }
            ImGui::Text(u8"武器 %s    弹药 %d / %d",
                        game::WeaponName(ps.weaponType), ps.ammoInClip, ps.ammoTotal);
            ImGui::Text(u8"肌肉 %.0f  肥胖 %.0f  耐力 %.0f  尊敬 %.0f  性感 %.0f",
                        ps.muscle, ps.fat, ps.stamina, ps.respect, ps.sexAppeal);
        }
    }
    ImGui::Separator();

    // --- 回复区（可滚动，流式追加时自动贴底）---
    std::string reply;
    bool streaming = false;
    client::Snapshot(&reply, &streaming);

    // 流式结束的瞬间把焦点还给输入框，不需要用户手动点击
    static bool s_wasStreaming = false;
    if (s_wasStreaming && !streaming)
        g_focusOnInput = true;
    s_wasStreaming = streaming;

    const float footer = ImGui::GetFrameHeightWithSpacing() * 2.0f + 8.0f;
    ImGui::BeginChild("##reply", ImVec2(0, -footer), false,
                      ImGuiWindowFlags_HorizontalScrollbar);

    if (reply.empty())
    {
        ImGui::TextDisabled(streaming ? u8"等待回复…"
                                      : u8"输入问题后按 Enter，回复会显示在这里。");
    }
    else
    {
        ImGui::TextWrapped("%s", reply.c_str());
    }

    // 回复区滚动。焦点常驻输入框，方向键和 Home/End 要留给文本编辑，
    // 所以只用 PageUp/PageDown 翻页——单行输入框不使用这两个键，不会冲突。
    const float page = ImGui::GetWindowHeight() * 0.85f;
    if (ImGui::IsKeyPressed(ImGuiKey_PageUp, true))
        ImGui::SetScrollY(ImGui::GetScrollY() - page);
    if (ImGui::IsKeyPressed(ImGuiKey_PageDown, true))
        ImGui::SetScrollY(ImGui::GetScrollY() + page);

    // 内容增长时保持贴底；用户手动上滚后不强制拉回
    if (streaming && ImGui::GetScrollY() >= ImGui::GetScrollMaxY() - 1.0f)
        ImGui::SetScrollHereY(1.0f);

    ImGui::EndChild();
    ImGui::Separator();

    // --- 输入区 ---
    // 鼠标不可用，焦点必须始终在输入框上，否则键盘输入无处可去。
    // 生成回复时禁用输入；录音/识别期间仍可编辑，方便边说边改。
    const bool busy = (st != client::State::Connected && st != client::State::Voice);
    if (g_focusOnInput || (!busy && !ImGui::IsAnyItemActive()))
    {
        ImGui::SetKeyboardFocusHere();
        g_focusOnInput = false;
    }

    ImGui::BeginDisabled(busy);

    ImGui::PushItemWidth(-1.0f);
    bool submitted = ImGui::InputText("##question", g_inputBuf, sizeof(g_inputBuf),
                                      ImGuiInputTextFlags_EnterReturnsTrue |
                                          ImGuiInputTextFlags_CallbackAlways,
                                      InputTextCallback);
    ImGui::PopItemWidth();

    // 回调只在控件处于激活（编辑）状态时触发。若识别结果回来时输入框恰好没被
    // 激活，回调不会跑，这里补一次直接追加，避免识别结果丢失。
    if (!g_pendingVoiceText.empty())
    {
        const size_t used = strlen(g_inputBuf);
        const size_t room = sizeof(g_inputBuf) - used - 1;
        if (room > 0)
            strncat_s(g_inputBuf, sizeof(g_inputBuf), g_pendingVoiceText.c_str(),
                      g_pendingVoiceText.size() < room ? g_pendingVoiceText.size() : room);
        g_pendingVoiceText.clear();
    }

    ImGui::EndDisabled();

    if (g_recording)
        ImGui::TextColored(ImVec4(0.95f, 0.4f, 0.4f, 1.0f),
                           u8"● 录音中…松开 Ctrl 结束");
    else if (st == client::State::Voice)
        ImGui::TextColored(ImVec4(0.95f, 0.8f, 0.3f, 1.0f), u8"识别中…");
    else
        ImGui::TextDisabled(
            u8"Enter 发送   按住左 Ctrl 说话   PgUp/PgDn 翻页   F1 状态面板   ` 或 Esc 关闭");

    if (submitted && g_inputBuf[0] != '\0' && !busy)
    {
        // 状态随问题一起发出去，Python 侧用它做查询改写和回答上下文
        if (client::Send(g_inputBuf, game::ToJson(ps)))
            g_inputBuf[0] = '\0';
        g_focusOnInput = true;
    }

    ImGui::End();
}

LRESULT __stdcall HookedWndProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp)
{
    // 热键：` 切换；Esc 在覆盖层打开时关闭
    if (msg == WM_KEYDOWN && !(lp & (1 << 30)))  // 忽略自动重复
    {
        if (wp == VK_OEM_3)                      // ` / ~
        {
            g_overlayOpen ? CloseOverlay() : OpenOverlay();
            return 0;                            // 不传给游戏
        }
        if (wp == VK_ESCAPE && g_overlayOpen)
        {
            CloseOverlay();
            return 0;
        }
    }

    if (g_overlayOpen && g_imguiReady)
    {
        // WM_CHAR 需要特殊处理：SA 是 MBCS 编译，WM_CHAR 传来的是 GBK 字节，
        // 不是 Unicode 码点。直接交给 ImGui_ImplWin32_WndProcHandler 会把
        // GBK 字节当 Unicode 处理，中文全变问号。
        // 解决方案：自己把 GBK 转 UTF-32 再喂给 ImGui，跳过后端的 WM_CHAR 处理。
        if (msg == WM_CHAR)
        {
            // GBK 双字节字符：高字节先到，低字节后到。
            // 用静态变量暂存高字节，等低字节到来后合并转换。
            static BYTE s_leadByte = 0;
            BYTE ch = static_cast<BYTE>(wp);

            if (s_leadByte != 0)
            {
                // 已有高字节，当前是低字节
                char mbcs[3] = { static_cast<char>(s_leadByte),
                                 static_cast<char>(ch), 0 };
                wchar_t wc = 0;
                if (::MultiByteToWideChar(CP_ACP, 0, mbcs, 2, &wc, 1) == 1)
                    ImGui::GetIO().AddInputCharacter(static_cast<unsigned int>(wc));
                s_leadByte = 0;
            }
            else if (::IsDBCSLeadByte(ch))
            {
                // GBK 高字节，等低字节
                s_leadByte = ch;
            }
            else
            {
                // 普通 ASCII 字符，直接传
                ImGui::GetIO().AddInputCharacter(static_cast<unsigned int>(ch));
            }
            return 0;   // 吞掉，不传给游戏
        }

        // 输入法上屏走这条路径。ANSI 窗口下 wParam 里是完整的 GBK 双字节
        // （高字节在前），一条消息一个汉字。
        if (msg == WM_IME_CHAR)
        {
            const WORD code = static_cast<WORD>(wp);
            wchar_t wc = 0;

            if (code > 0xFF)
            {
                char mbcs[3] = { static_cast<char>((code >> 8) & 0xFF),
                                 static_cast<char>(code & 0xFF), 0 };
                if (::MultiByteToWideChar(CP_ACP, 0, mbcs, 2, &wc, 1) != 1)
                    wc = 0;
            }
            else
            {
                char mbcs[2] = { static_cast<char>(code & 0xFF), 0 };
                if (::MultiByteToWideChar(CP_ACP, 0, mbcs, 1, &wc, 1) != 1)
                    wc = 0;
            }

            if (wc != 0)
                ImGui::GetIO().AddInputCharacter(static_cast<unsigned int>(wc));
            return 0;
        }

        // 其余消息交给 ImGui Win32 后端处理（键盘导航、鼠标等）
        ImGui_ImplWin32_WndProcHandler(hwnd, msg, wp, lp);

        // 覆盖层打开时吞掉所有键鼠输入，避免打字时角色乱跑、视角乱转。
        // 放行非输入类消息（绘制、焦点、系统通知等），否则游戏窗口会异常。
        switch (msg)
        {
        case WM_KEYDOWN:      case WM_KEYUP:
        case WM_SYSKEYDOWN:   case WM_SYSKEYUP:
        case WM_CHAR:         case WM_SYSCHAR:
        case WM_DEADCHAR:     case WM_UNICHAR:
        case WM_MOUSEMOVE:    case WM_MOUSEWHEEL:  case WM_MOUSEHWHEEL:
        case WM_LBUTTONDOWN:  case WM_LBUTTONUP:   case WM_LBUTTONDBLCLK:
        case WM_RBUTTONDOWN:  case WM_RBUTTONUP:   case WM_RBUTTONDBLCLK:
        case WM_MBUTTONDOWN:  case WM_MBUTTONUP:   case WM_MBUTTONDBLCLK:
        case WM_XBUTTONDOWN:  case WM_XBUTTONUP:   case WM_XBUTTONDBLCLK:
        case WM_INPUT:
            return 0;
        default:
            break;
        }
    }

    return ::CallWindowProc(g_realWndProc, hwnd, msg, wp, lp);
}

void InitImGui(IDirect3DDevice9* device)
{
    ImGui::CreateContext();
    ImGui::StyleColorsDark();

    ImGuiIO& io = ImGui::GetIO();
    io.IniFilename = nullptr;   // 不往游戏目录写 imgui.ini

    // 游戏独占 DirectInput 鼠标且每帧把光标锁回中心，覆盖层拿不到可用的鼠标，
    // 交互全部走键盘。关掉鼠标光标绘制，避免屏幕上留一个不动的箭头。
    io.MouseDrawCursor = false;
    io.ConfigFlags |= ImGuiConfigFlags_NoMouse;

    // 默认字体没有中日韩字形，界面中文会显示成方块。
    // 载入系统雅黑并附带中文字形范围；缺字体则回退到默认字体。
    ImFontConfig cfg;
    cfg.MergeMode = false;
    if (!io.Fonts->AddFontFromFileTTF("C:\\Windows\\Fonts\\msyh.ttc", 18.0f, &cfg,
                                      io.Fonts->GetGlyphRangesChineseFull()))
    {
        io.Fonts->AddFontDefault();
    }

    ImGui_ImplWin32_Init(g_window);
    ImGui_ImplDX9_Init(device);
    g_imguiReady = true;
}

HRESULT __stdcall HookedEndScene(IDirect3DDevice9* device)
{
    if (device == g_device)
    {
        if (!g_imguiReady)
        {
            agentlog::Write("[render] 首次 EndScene，tid=%lu，开始初始化 ImGui",
                       GetCurrentThreadId());
            InitImGui(device);
            agentlog::Write("[render] ImGui 初始化完成");
        }

        // 轮询要在渲染判断之前：空闲帧不渲染，但按键仍然要能被察觉
        PollLiveKey();

        // 覆盖层关着时也要渲染：实时语音问答的提示要能浮在游戏画面上。
        // 但两者都不需要显示时就整帧跳过，免得白付 ImGui 的开销。
        std::string liveStatus;
        client::LiveStatus(&liveStatus);
        const bool needLiveHud = g_liveTalking || !liveStatus.empty();

        if (g_imguiReady && (g_overlayOpen || needLiveHud))
        {
            ImGui_ImplDX9_NewFrame();
            ImGui_ImplWin32_NewFrame();
            ImGui::NewFrame();
            if (g_overlayOpen)
                DrawUI();
            DrawLiveHud();
            ImGui::EndFrame();
            ImGui::Render();
            ImGui_ImplDX9_RenderDrawData(ImGui::GetDrawData());
        }
    }
    return g_realEndScene(device);
}

HRESULT __stdcall HookedReset(IDirect3DDevice9* device, D3DPRESENT_PARAMETERS* params)
{
    if (device == g_device && g_imguiReady)
        ImGui_ImplDX9_InvalidateDeviceObjects();

    HRESULT hr = g_realReset(device, params);

    if (device == g_device && g_imguiReady && SUCCEEDED(hr))
        ImGui_ImplDX9_CreateDeviceObjects();

    return hr;
}

// 后台线程：等游戏把 D3D9 设备和窗口都建好，再装钩子。
// 直接在 DllMain 里装会因为设备尚未创建而失败。
DWORD WINAPI InitThread(LPVOID)
{
    agentlog::Init();
    agentlog::Write("[init] thread start, tid=%lu", GetCurrentThreadId());

    if (!game::IsSupportedVersion())
    {
        agentlog::Write("[init] 警告: 未能识别为 GTA:SA 1.0 US，"
                        "玩家状态读取将被禁用（问答功能不受影响）");
    }

    int waited = 0;
    for (int i = 0; i < 600; ++i)   // 最多等 60 秒
    {
        IDirect3DDevice9* dev = game::GetDevice();
        HWND wnd = game::GetWindow();
        if (dev != nullptr && wnd != nullptr && ::IsWindow(wnd))
        {
            g_device = dev;
            g_window = wnd;
            waited = i;
            break;
        }
        ::Sleep(100);
    }

    if (g_device == nullptr)
    {
        agentlog::Write("[init] FAIL: 等待 60 秒后仍未取到 D3D9 设备/窗口 "
                   "(device=%p window=%p)",
                   game::GetDevice(), game::GetWindow());
        return 0;
    }

    agentlog::Write("[init] device=%p window=%p (等待 %d00 ms)", g_device, g_window, waited);

    MH_STATUS st = MH_Initialize();
    if (st != MH_OK && st != MH_ERROR_ALREADY_INITIALIZED)
    {
        agentlog::Write("[init] FAIL: MH_Initialize -> %d", st);
        return 0;
    }

    void* pEndScene = VTableEntry(g_device, kIdxEndScene);
    void* pReset    = VTableEntry(g_device, kIdxReset);
    agentlog::Write("[init] EndScene=%p Reset=%p", pEndScene, pReset);

    st = MH_CreateHook(pEndScene, reinterpret_cast<void*>(&HookedEndScene),
                       reinterpret_cast<void**>(&g_realEndScene));
    if (st != MH_OK)
    {
        agentlog::Write("[init] FAIL: hook EndScene -> %d", st);
        return 0;
    }

    st = MH_CreateHook(pReset, reinterpret_cast<void*>(&HookedReset),
                       reinterpret_cast<void**>(&g_realReset));
    if (st != MH_OK)
    {
        agentlog::Write("[init] FAIL: hook Reset -> %d", st);
        return 0;
    }

    st = MH_EnableHook(MH_ALL_HOOKS);
    if (st != MH_OK)
    {
        agentlog::Write("[init] FAIL: MH_EnableHook -> %d", st);
        return 0;
    }
    agentlog::Write("[init] D3D9 钩子已启用");

    // 子类化窗口过程，接管键鼠输入
    g_realWndProc = reinterpret_cast<WNDPROC>(
        ::SetWindowLongPtr(g_window, GWLP_WNDPROC,
                           reinterpret_cast<LONG_PTR>(&HookedWndProc)));

    if (g_realWndProc == nullptr)
        agentlog::Write("[init] FAIL: SetWindowLongPtr -> err=%lu", GetLastError());
    else
        agentlog::Write("[init] WndProc 已子类化，原过程=%p。初始化完成，按 ` 呼出",
                   g_realWndProc);

    return 0;
}
}

namespace overlay
{
void Start()
{
    client::Start();

    HANDLE h = ::CreateThread(nullptr, 0, &InitThread, nullptr, 0, nullptr);
    if (h != nullptr)
        ::CloseHandle(h);
}

void Stop()
{
    if (g_realWndProc != nullptr && g_window != nullptr && ::IsWindow(g_window))
    {
        ::SetWindowLongPtr(g_window, GWLP_WNDPROC,
                           reinterpret_cast<LONG_PTR>(g_realWndProc));
        g_realWndProc = nullptr;
    }

    client::Stop();

    CloseOverlay();

    MH_DisableHook(MH_ALL_HOOKS);

    if (g_imguiReady)
    {
        ImGui_ImplDX9_Shutdown();
        ImGui_ImplWin32_Shutdown();
        ImGui::DestroyContext();
        g_imguiReady = false;
    }

    MH_Uninitialize();
    g_device = nullptr;
}
}
