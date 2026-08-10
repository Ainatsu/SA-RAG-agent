#include "log.h"

#include <cstdio>
#include <cstdarg>
#include <cstring>

namespace agentlog
{
static FILE* g_file = nullptr;

void Init()
{
    if (g_file != nullptr)
        return;

    char path[MAX_PATH];
    GetModuleFileNameA(nullptr, path, MAX_PATH);   // 游戏 exe 全路径
    char* slash = strrchr(path, '\\');
    if (slash != nullptr)
        strcpy(slash + 1, "sa_agent_overlay.log");
    else
        return;

    g_file = fopen(path, "a");
}

void Write(const char* fmt, ...)
{
    if (g_file == nullptr)
        return;

    va_list args;
    va_start(args, fmt);
    vfprintf(g_file, fmt, args);
    va_end(args);
    fputc('\n', g_file);
    fflush(g_file);
}
}
