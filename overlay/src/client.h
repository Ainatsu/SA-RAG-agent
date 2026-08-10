#pragma once
#include <string>

// 与本地 Python Agent 服务的通信。
//
// 所有网络 I/O 都在独立线程完成，绝不阻塞游戏渲染线程。
// 渲染线程只通过 Send() 投递问题、用 Snapshot() 取当前回复文本。
namespace client
{
enum class State
{
    Disconnected,   // 未连接（服务未启动或已断开）
    Connecting,     // 正在重连
    Connected,      // 已连接，空闲
    Waiting,        // 已发送问题，等待/接收回复中
    Voice,          // 语音录制或识别中
};

// 启动后台网络线程，自动连接并断线重连。
void Start();
void Stop();

// 仅优雅关闭 socket，不等待线程结束。
// 用于进程退出路径：此时不能 join 线程（会死锁），但发一个 FIN
// 能让服务端看到正常关闭而不是连接重置。
void ShutdownSocket();

// 投递一个问题（渲染线程调用，立即返回）。
// state 是玩家状态 JSON（game::ToJson 的结果），会在问题帧之前
// 作为独立的 S 帧发出；传 "{}" 表示本次没有可用状态。
// 未连接时返回 false。
bool Send(const std::string& question, const std::string& state);

// 发送语音控制帧（渲染线程调用，立即返回）。
// command 为 "start" 或 "stop"。服务端录音完成后以 X 帧回传识别文本。
// 未连接时返回 false。
bool SendVoice(const std::string& command);

// 发送实时语音问答控制帧（渲染线程调用，立即返回）。
// command 为 "start" 或 "stop"。与 SendVoice 不同，这一路不回填输入框：
// 服务端识别后直接生成答案并朗读，覆盖层可以是关着的。
// state 是玩家状态 JSON，随 start 一起发出，让答案能结合当前处境。
// 未连接时返回 false。
bool SendLive(const std::string& command, const std::string& state);

// 取实时语音问答的当前阶段提示（渲染线程调用，线程安全）。
// 用于在游戏画面上显示"听着…""想想…"之类的字样。空串表示空闲。
void LiveStatus(std::string* out);

// 推送拾取物清单（渲染线程调用，立即返回）。
// json 是 game::ScanPickups() 的结果，服务端缓存下来用于"最近的防弹衣在哪"
// 这类空间查询。扫描本身必须在游戏主线程做，所以由渲染线程投递。
bool SendPickups(const std::string& json);

// 取服务端下发的标点请求（渲染线程调用）。
// 有待处理的请求时写入坐标并返回 true。设 blip 会调用游戏函数，
// 只能在游戏主线程做，所以网络线程只负责收下来排队。
bool TakeWaypoint(float* x, float* y, float* z);

// 取当前状态与回复文本快照（渲染线程调用，线程安全）。
State GetState();

// 把当前累积的回复文本拷贝到 out。streaming 表示是否仍在接收中。
void Snapshot(std::string* out, bool* streaming);

// 取最近一次的语音识别结果。有新结果时写入 out 并返回 true，之后清空
// 内部缓存。识别不出内容时结果为空串，但仍返回 true——调用方据此结束
// "识别中"的界面状态。
bool TakeTranscript(std::string* out);

// 状态文字描述，用于界面显示。
const char* StateText();
}
