#include "client.h"
#include "log.h"

#include <winsock2.h>
#include <ws2tcpip.h>

#include <atomic>
#include <deque>
#include <mutex>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#pragma comment(lib, "ws2_32.lib")

namespace
{
constexpr char kHost[] = "127.0.0.1";
constexpr unsigned short kPort = 51678;
constexpr uint32_t kMaxFrame = 1u << 20;

constexpr char kFrameQuestion   = 'Q';
constexpr char kFrameState      = 'S';
constexpr char kFrameVoice      = 'V';
constexpr char kFrameToken      = 'T';
constexpr char kFrameDone       = 'D';
constexpr char kFrameError      = 'E';
constexpr char kFrameTranscript = 'X';
constexpr char kFrameLive       = 'L';
constexpr char kFrameLiveStatus = 'P';
// G 帧：覆盖层 -> 服务端，拾取物清单（JSON 数组）
constexpr char kFramePickups    = 'G';
// W 帧：服务端 -> 覆盖层，标点请求，载荷为 "x,y,z"
constexpr char kFrameWaypoint   = 'W';

std::atomic<client::State> g_state{client::State::Disconnected};
std::atomic<bool> g_running{false};
std::atomic<SOCKET> g_socket{INVALID_SOCKET};

std::thread g_thread;

// 受 g_mutex 保护的共享数据
std::mutex  g_mutex;
std::string g_reply;         // 当前累积的回复
std::string g_pending;       // 待发送的问题（空表示无）
std::string g_pendingState;  // 与待发送问题配套的状态 JSON
bool        g_hasPending = false;

// 待发送的语音指令。按住说话会连续产生 start/stop，用队列而不是单槽位，
// 否则快速点按时 stop 会覆盖掉还没发出的 start，录音就停不下来了。
std::deque<std::string> g_voiceQueue;
std::string g_transcript;
bool        g_hasTranscript = false;

// 实时语音问答：指令队列同上，理由一样。状态文字由服务端的 P 帧推来，
// 渲染线程只读，用于在游戏画面上显示当前进行到哪一步。
std::deque<std::pair<std::string, std::string>> g_liveQueue;
std::string g_liveStatus;

// 待发送的拾取物清单。整包较大（几百个点），只留最新一份，
// 攒多了没意义——服务端要的是当前状态。
std::string g_pendingPickups;
bool        g_hasPickups = false;

// 服务端下发的标点请求，等渲染线程取走。
struct WaypointReq { float x, y, z; };
std::deque<WaypointReq> g_waypointQueue;

bool SendAll(SOCKET s, const char* data, int len)
{
    int sent = 0;
    while (sent < len)
    {
        int n = ::send(s, data + sent, len - sent, 0);
        if (n == SOCKET_ERROR || n == 0)
            return false;
        sent += n;
    }
    return true;
}

// 阻塞读取指定字节数。返回 false 表示连接断开或出错。
bool RecvAll(SOCKET s, char* data, int len)
{
    int got = 0;
    while (got < len)
    {
        int n = ::recv(s, data + got, len - got, 0);
        if (n == SOCKET_ERROR || n == 0)
            return false;
        got += n;
    }
    return true;
}

bool SendFrame(SOCKET s, char type, const std::string& payload)
{
    uint32_t len = static_cast<uint32_t>(1 + payload.size());
    char header[4];
    memcpy(header, &len, 4);   // x86 本身小端，直接拷贝

    return SendAll(s, header, 4)
        && SendAll(s, &type, 1)
        && (payload.empty() || SendAll(s, payload.data(), static_cast<int>(payload.size())));
}

// 读取一帧。成功时填充 type/payload。
bool RecvFrame(SOCKET s, char* type, std::string* payload)
{
    char header[4];
    if (!RecvAll(s, header, 4))
        return false;

    uint32_t len;
    memcpy(&len, header, 4);
    if (len < 1 || len > kMaxFrame)
    {
        agentlog::Write("[client] 帧长度异常: %u", len);
        return false;
    }

    if (!RecvAll(s, type, 1))
        return false;

    payload->resize(len - 1);
    if (len > 1 && !RecvAll(s, &(*payload)[0], static_cast<int>(len - 1)))
        return false;

    return true;
}

SOCKET Connect()
{
    SOCKET s = ::socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (s == INVALID_SOCKET)
        return INVALID_SOCKET;

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = ::htons(kPort);
    ::inet_pton(AF_INET, kHost, &addr.sin_addr);

    if (::connect(s, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) == SOCKET_ERROR)
    {
        ::closesocket(s);
        return INVALID_SOCKET;
    }

    // 关闭 Nagle：回复是小片段流式下发，攒包会明显增加延迟
    BOOL nodelay = TRUE;
    ::setsockopt(s, IPPROTO_TCP, TCP_NODELAY,
                 reinterpret_cast<const char*>(&nodelay), sizeof(nodelay));

    return s;
}

// 取出待发送的问题与配套状态；无则返回 false
bool TakePending(std::string* out, std::string* state)
{
    std::lock_guard<std::mutex> lock(g_mutex);
    if (!g_hasPending)
        return false;
    *out = std::move(g_pending);
    *state = std::move(g_pendingState);
    g_pending.clear();
    g_pendingState.clear();
    g_hasPending = false;
    return true;
}

// 取出待发送的语音指令；无则返回 false
bool TakeVoice(std::string* out)
{
    std::lock_guard<std::mutex> lock(g_mutex);
    if (g_voiceQueue.empty())
        return false;
    *out = std::move(g_voiceQueue.front());
    g_voiceQueue.pop_front();
    return true;
}

bool TakeLive(std::string* cmd, std::string* state)
{
    std::lock_guard<std::mutex> lock(g_mutex);
    if (g_liveQueue.empty())
        return false;
    *cmd = std::move(g_liveQueue.front().first);
    *state = std::move(g_liveQueue.front().second);
    g_liveQueue.pop_front();
    return true;
}

// 记下服务端推来的阶段提示。idle 是一轮的收尾标记，落到界面上就是清空。
void SetLiveStatus(const std::string& payload)
{
    std::lock_guard<std::mutex> lock(g_mutex);
    g_liveStatus = (payload == "idle") ? std::string() : payload;
}

// 收下一条标点请求。载荷是 "x,y,z"，解析不出来就丢掉。
void QueueWaypoint(const std::string& payload)
{
    float x = 0.0f, y = 0.0f, z = 0.0f;
    if (::sscanf_s(payload.c_str(), "%f,%f,%f", &x, &y, &z) != 3)
    {
        agentlog::Write("[client] 标点载荷格式错误: %s", payload.c_str());
        return;
    }
    std::lock_guard<std::mutex> lock(g_mutex);
    g_waypointQueue.push_back({x, y, z});
}

// 取出待发送的拾取物清单；无则返回 false
bool TakePickups(std::string* out)
{
    std::lock_guard<std::mutex> lock(g_mutex);
    if (!g_hasPickups)
        return false;
    *out = std::move(g_pendingPickups);
    g_pendingPickups.clear();
    g_hasPickups = false;
    return true;
}

// 空闲时顺手收一下 socket 里的 P 帧。
//
// 唤醒词那一路是服务端自己触发的，覆盖层这边没有请求在等回帧，不主动读
// 就永远读不到，HUD 也就不会亮。用 select 探一下可读再收，别把会话循环
// 堵住。一次最多收几帧，剩下的下一轮再说。
bool PollStatus(SOCKET s)
{
    for (int i = 0; i < 8; ++i)
    {
        fd_set rd;
        FD_ZERO(&rd);
        FD_SET(s, &rd);
        timeval tv{0, 0};

        if (::select(0, &rd, nullptr, nullptr, &tv) <= 0)
            return true;

        char type = 0;
        std::string payload;
        if (!RecvFrame(s, &type, &payload))
            return false;

        if (type == kFrameLiveStatus)
            SetLiveStatus(payload);
        else if (type == kFrameWaypoint)
            QueueWaypoint(payload);
        else
            agentlog::Write("[client] 空闲时收到帧 '%c'，忽略", type);
    }
    return true;
}

void NetworkThread()
{
    WSADATA wsa;
    if (::WSAStartup(MAKEWORD(2, 2), &wsa) != 0)
    {
        agentlog::Write("[client] WSAStartup 失败");
        return;
    }

    bool loggedFailure = false;

    while (g_running.load())
    {
        g_state.store(client::State::Connecting);
        SOCKET s = Connect();

        if (s == INVALID_SOCKET)
        {
            // 服务未启动是常见情况，只记一次日志，避免刷屏
            if (!loggedFailure)
            {
                agentlog::Write("[client] 无法连接 %s:%d，将持续重试（请确认 server.py 已运行）",
                           kHost, kPort);
                loggedFailure = true;
            }
            g_state.store(client::State::Disconnected);

            for (int i = 0; i < 10 && g_running.load(); ++i)
                ::Sleep(100);
            continue;
        }

        agentlog::Write("[client] 已连接到 Agent 服务");
        loggedFailure = false;
        g_socket.store(s);
        g_state.store(client::State::Connected);

        // 会话循环：等待问题或语音指令 -> 发送 -> 接收回复
        while (g_running.load())
        {
            // 语音优先。它只是启停录音，处理很快，不该被排在问答后面。
            std::string voiceCmd;
            if (TakeVoice(&voiceCmd))
            {
                const bool isStop = (voiceCmd == "stop");
                if (!isStop)
                    g_state.store(client::State::Voice);

                if (!SendFrame(s, kFrameVoice, voiceCmd))
                {
                    agentlog::Write("[client] 发送语音指令失败，连接已断开");
                    break;
                }

                // start 只是让服务端开麦，没有回帧；stop 之后要等识别结果
                if (!isStop)
                    continue;

                bool alive = true;
                while (g_running.load())
                {
                    char type = 0;
                    std::string payload;
                    if (!RecvFrame(s, &type, &payload))
                    {
                        alive = false;
                        break;
                    }

                    if (type == kFrameTranscript)
                    {
                        std::lock_guard<std::mutex> lock(g_mutex);
                        g_transcript = payload;
                        g_hasTranscript = true;
                        break;
                    }
                    if (type == kFrameError)
                    {
                        std::lock_guard<std::mutex> lock(g_mutex);
                        g_reply += "\n[服务端错误] " + payload;
                    }
                    else if (type == kFrameLiveStatus)
                    {
                        // 唤醒问答和按住说话共用一条连接，这里可能夹进 P 帧
                        SetLiveStatus(payload);
                    }
                    else
                    {
                        agentlog::Write("[client] 语音等待中收到帧 '%c'，忽略", type);
                    }
                }

                if (!alive)
                {
                    agentlog::Write("[client] 语音接收中断，连接已断开");
                    break;
                }

                g_state.store(client::State::Connected);
                continue;
            }

            // 实时语音问答。与上面那段的区别是：stop 之后服务端会先后发来
            // 若干 P 帧（recording / thinking / q:… / speaking），最后以
            // idle 收尾。答案在服务端直接念出来，不回传文本，所以没有 T 帧。
            std::string liveCmd, liveState;
            if (TakeLive(&liveCmd, &liveState))
            {
                // 心跳只是把状态送出去，服务端不回帧，也不该动连接状态：
                // 置成 Voice 会让覆盖层以为正在问答，挡住后面的提问。
                if (liveCmd == "heartbeat")
                {
                    if (!liveState.empty() && !SendFrame(s, kFrameState, liveState))
                    {
                        agentlog::Write("[client] 发送心跳状态失败，连接已断开");
                        break;
                    }
                    if (!SendFrame(s, kFrameLive, liveCmd))
                    {
                        agentlog::Write("[client] 发送心跳失败，连接已断开");
                        break;
                    }
                    continue;
                }

                const bool isStop = (liveCmd == "stop");
                if (!isStop)
                {
                    g_state.store(client::State::Voice);
                    if (!liveState.empty() && !SendFrame(s, kFrameState, liveState))
                    {
                        agentlog::Write("[client] 发送实时语音状态失败，连接已断开");
                        break;
                    }
                }

                if (!SendFrame(s, kFrameLive, liveCmd))
                {
                    agentlog::Write("[client] 发送实时语音指令失败，连接已断开");
                    break;
                }

                if (!isStop)
                    continue;

                bool alive = true;
                while (g_running.load())
                {
                    char type = 0;
                    std::string payload;
                    if (!RecvFrame(s, &type, &payload))
                    {
                        alive = false;
                        break;
                    }

                    if (type == kFrameLiveStatus)
                    {
                        SetLiveStatus(payload);
                        // 只有 idle 表示这一轮不再回帧了。speaking 之后还有
                        // 一个 idle 等着——提前跳出会把它留在缓冲区里，串到
                        // 下一轮去。
                        if (payload == "idle")
                            break;
                        continue;
                    }
                    if (type == kFrameError)
                    {
                        agentlog::Write("[client] 实时语音出错: %s", payload.c_str());
                        SetLiveStatus("idle");
                        break;
                    }
                    if (type == kFrameWaypoint)
                    {
                        // 问"最近的防弹衣在哪"时，服务端会在念答案的同时
                        // 把坐标下发过来，交给渲染线程去插旗
                        QueueWaypoint(payload);
                        continue;
                    }

                    agentlog::Write("[client] 实时语音等待中收到帧 '%c'，忽略", type);
                }

                if (!alive)
                {
                    agentlog::Write("[client] 实时语音接收中断，连接已断开");
                    break;
                }

                g_state.store(client::State::Connected);
                continue;
            }

            // 拾取物清单。渲染线程定期扫描推进来，网络线程负责送出去。
            // 没有回帧，不动连接状态。
            std::string pickups;
            if (TakePickups(&pickups))
            {
                if (!SendFrame(s, kFramePickups, pickups))
                {
                    agentlog::Write("[client] 发送拾取物清单失败，连接已断开");
                    break;
                }
                continue;
            }

            std::string question;
            std::string state;
            if (!TakePending(&question, &state))
            {
                // 手上没活的时候把服务端主动推来的 P 帧收掉（唤醒词问答）
                if (!PollStatus(s))
                {
                    agentlog::Write("[client] 空闲轮询中断，连接已断开");
                    break;
                }
                ::Sleep(16);   // 约一帧，足够灵敏且几乎不占 CPU
                continue;
            }

            {
                std::lock_guard<std::mutex> lock(g_mutex);
                g_reply.clear();
            }
            g_state.store(client::State::Waiting);

            // 状态帧先发。服务端把它暂存，等下一个 Q 帧到达时一并处理。
            // 状态不可用时（未进存档、版本不符）跳过，服务端按无状态处理。
            if (state != "{}" && !SendFrame(s, kFrameState, state))
            {
                agentlog::Write("[client] 发送状态失败，连接已断开");
                break;
            }

            if (!SendFrame(s, kFrameQuestion, question))
            {
                agentlog::Write("[client] 发送失败，连接已断开");
                break;
            }

            // 接收直到 D（结束）或 E（错误）
            bool alive = true;
            while (g_running.load())
            {
                char type = 0;
                std::string payload;
                if (!RecvFrame(s, &type, &payload))
                {
                    alive = false;
                    break;
                }

                if (type == kFrameToken)
                {
                    std::lock_guard<std::mutex> lock(g_mutex);
                    g_reply += payload;
                }
                else if (type == kFrameDone)
                {
                    break;
                }
                else if (type == kFrameError)
                {
                    std::lock_guard<std::mutex> lock(g_mutex);
                    g_reply += "\n[服务端错误] " + payload;
                    break;
                }
                else if (type == kFrameLiveStatus)
                {
                    // 打字提问的同时唤醒问答也可能在跑，P 帧会夹进来
                    SetLiveStatus(payload);
                }
                else
                {
                    agentlog::Write("[client] 未知帧类型 '%c'，忽略", type);
                }
            }

            if (!alive)
            {
                agentlog::Write("[client] 接收中断，连接已断开");
                break;
            }

            g_state.store(client::State::Connected);
        }

        g_socket.store(INVALID_SOCKET);
        ::closesocket(s);
        g_state.store(client::State::Disconnected);

        // 断线时若界面正等着识别结果，投一个空结果解除等待，
        // 顺便丢掉没发出去的语音指令，避免重连后补发一个孤立的 stop。
        {
            std::lock_guard<std::mutex> lock(g_mutex);
            g_voiceQueue.clear();
            g_liveQueue.clear();
            g_liveStatus.clear();
            g_waypointQueue.clear();
            g_pendingPickups.clear();
            g_hasPickups = false;
            if (!g_hasTranscript)
            {
                g_transcript.clear();
                g_hasTranscript = true;
            }
        }
    }

    ::WSACleanup();
}
}

namespace client
{
void Start()
{
    if (g_running.exchange(true))
        return;
    g_thread = std::thread(&NetworkThread);
}

void ShutdownSocket()
{
    g_running.store(false);

    // SD_SEND 发出 FIN，服务端会读到干净的 EOF 而非 RST。
    // 只是一次系统调用，进程退出时也可安全执行。
    SOCKET s = g_socket.load();
    if (s != INVALID_SOCKET)
        ::shutdown(s, SD_SEND);
}

void Stop()
{
    if (!g_running.exchange(false))
        return;

    // 唤醒可能阻塞在 recv 上的线程
    SOCKET s = g_socket.load();
    if (s != INVALID_SOCKET)
        ::shutdown(s, SD_BOTH);

    if (g_thread.joinable())
        g_thread.join();
}

bool Send(const std::string& question, const std::string& state)
{
    if (g_state.load() != State::Connected)
        return false;

    std::lock_guard<std::mutex> lock(g_mutex);
    g_pending = question;
    g_pendingState = state;
    g_hasPending = true;
    return true;
}

bool SendVoice(const std::string& command)
{
    const State st = g_state.load();
    if (st != State::Connected && st != State::Voice)
        return false;

    std::lock_guard<std::mutex> lock(g_mutex);
    g_voiceQueue.push_back(command);
    return true;
}

bool SendLive(const std::string& command, const std::string& state)
{
    const State st = g_state.load();
    if (st != State::Connected && st != State::Voice)
        return false;

    std::lock_guard<std::mutex> lock(g_mutex);
    g_liveQueue.emplace_back(command, state);
    return true;
}

void LiveStatus(std::string* out)
{
    std::lock_guard<std::mutex> lock(g_mutex);
    *out = g_liveStatus;
}

State GetState()
{
    return g_state.load();
}

void Snapshot(std::string* out, bool* streaming)
{
    {
        std::lock_guard<std::mutex> lock(g_mutex);
        *out = g_reply;
    }
    const State st = g_state.load();
    *streaming = (st == State::Waiting);

    // 流式接收时，末尾可能是一个被切断的多字节 UTF-8 字符
    // （LLM 的 token 边界不保证落在字符边界上）。
    // 截掉不完整的尾部，否则 ImGui 会渲染出乱码方块。
    if (!out->empty())
    {
        size_t i = out->size();
        size_t trailing = 0;
        // 向前跳过续字节 10xxxxxx
        while (i > 0 && (static_cast<unsigned char>((*out)[i - 1]) & 0xC0) == 0x80)
        {
            --i;
            ++trailing;
        }
        if (i > 0)
        {
            const unsigned char lead = static_cast<unsigned char>((*out)[i - 1]);
            size_t need = (lead & 0x80) == 0x00 ? 1
                        : (lead & 0xE0) == 0xC0 ? 2
                        : (lead & 0xF0) == 0xE0 ? 3
                        : (lead & 0xF8) == 0xF0 ? 4
                                                : 1;
            // 该字符需要 need 字节，实际只有 1 + trailing 字节 -> 不完整
            if (1 + trailing < need)
                out->resize(i - 1);
        }
    }
}

bool TakeTranscript(std::string* out)
{
    std::lock_guard<std::mutex> lock(g_mutex);
    if (!g_hasTranscript)
        return false;
    *out = std::move(g_transcript);
    g_transcript.clear();
    g_hasTranscript = false;
    return true;
}

bool SendPickups(const std::string& json)
{
    const State st = g_state.load();
    if (st != State::Connected && st != State::Voice)
        return false;

    std::lock_guard<std::mutex> lock(g_mutex);
    // 只留最新一份；攒多了没意义，服务端要的是当前状态
    g_pendingPickups = json;
    g_hasPickups = true;
    return true;
}

bool TakeWaypoint(float* x, float* y, float* z)
{
    std::lock_guard<std::mutex> lock(g_mutex);
    if (g_waypointQueue.empty())
        return false;
    const WaypointReq& req = g_waypointQueue.front();
    *x = req.x;
    *y = req.y;
    *z = req.z;
    g_waypointQueue.pop_front();
    return true;
}

const char* StateText()
{
    switch (g_state.load())
    {
    case State::Disconnected: return u8"未连接（请启动 server.py）";
    case State::Connecting:   return u8"连接中…";
    case State::Connected:    return u8"已连接";
    case State::Waiting:      return u8"生成中…";
    case State::Voice:        return u8"识别中…";
    }
    return u8"未知";
}
}
