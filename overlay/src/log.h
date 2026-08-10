#pragma once
#include <windows.h>

// 排障用日志。写到 gta_sa.exe 同目录的 sa_agent_overlay.log。
// 命名空间不能叫 log —— 会与 <cmath> 的 ::log 函数冲突。
namespace agentlog
{
void Init();
void Write(const char* fmt, ...);
}
