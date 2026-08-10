#include <windows.h>
#include "overlay.h"
#include "client.h"
#include "log.h"

// 本 DLL 以 .asi 形式发布，由游戏目录下已有的 ASI 加载器
// （汉化补丁的 vorbisFile.dll -> vorbisHooked.dll）从 scripts\ 目录加载。
BOOL APIENTRY DllMain(HMODULE module, DWORD reason, LPVOID reserved)
{
    switch (reason)
    {
    case DLL_PROCESS_ATTACH:
        ::DisableThreadLibraryCalls(module);
        agentlog::Init();
        agentlog::Write("=== SAAgentOverlay 已被加载 (pid=%lu) ===", GetCurrentProcessId());
        // 此刻 D3D9 设备尚未创建，Start() 内部会起线程等待就绪后再装钩子。
        overlay::Start();
        break;

    case DLL_PROCESS_DETACH:
        // reserved 非空表示进程正在退出。此时不能做完整清理：
        // 其他线程已被强制终止，且我们持有加载器锁，join 线程必然死锁。
        // 但可以安全地关闭 socket，让服务端看到正常断开而非连接重置。
        if (reserved == nullptr)
            overlay::Stop();
        else
            client::ShutdownSocket();
        break;

    default:
        break;
    }
    return TRUE;
}
