cmake_minimum_required(VERSION 3.20)
project(sa_agent_overlay LANGUAGES C CXX)

# 游戏是 32 位的，插件必须同为 x86
if(NOT CMAKE_SIZEOF_VOID_P EQUAL 4)
    message(FATAL_ERROR "必须以 x86 (Win32) 配置构建，当前指针宽度为 ${CMAKE_SIZEOF_VOID_P} 字节")
endif()

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")

set(TP "${CMAKE_CURRENT_SOURCE_DIR}/third_party")

# --- MinHook（静态库，C 源码）---
add_library(minhook STATIC
    ${TP}/minhook/src/buffer.c
    ${TP}/minhook/src/hook.c
    ${TP}/minhook/src/trampoline.c
    ${TP}/minhook/src/hde/hde32.c
    ${TP}/minhook/src/hde/hde64.c
)
target_include_directories(minhook PUBLIC ${TP}/minhook/include)

# --- Dear ImGui + DX9/Win32 后端 ---
add_library(imgui STATIC
    ${TP}/imgui/imgui.cpp
    ${TP}/imgui/imgui_draw.cpp
    ${TP}/imgui/imgui_tables.cpp
    ${TP}/imgui/imgui_widgets.cpp
    ${TP}/imgui/backends/imgui_impl_dx9.cpp
    ${TP}/imgui/backends/imgui_impl_win32.cpp
)
target_include_directories(imgui PUBLIC ${TP}/imgui ${TP}/imgui/backends)
target_link_libraries(imgui PUBLIC d3d9)

# --- 插件本体，产物为 .asi（本质是 DLL）---
add_library(SAAgentOverlay SHARED
    src/dllmain.cpp
    src/overlay.cpp
    src/game.cpp
    src/log.cpp
    src/client.cpp
)
target_include_directories(SAAgentOverlay PRIVATE src)
# imm32：游戏通常禁用输入法，覆盖层需要重新启用才能打中文
target_link_libraries(SAAgentOverlay PRIVATE imgui minhook d3d9 imm32)

set_target_properties(SAAgentOverlay PROPERTIES
    SUFFIX ".asi"
    PREFIX ""
)

# UTF-8 源码，且关闭对 D3D9 弃用告警的噪音
target_compile_options(SAAgentOverlay PRIVATE /utf-8 /W3)
target_compile_definitions(SAAgentOverlay PRIVATE
    WIN32_LEAN_AND_MEAN
    NOMINMAX
    _CRT_SECURE_NO_WARNINGS
)
