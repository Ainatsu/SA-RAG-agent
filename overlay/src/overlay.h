#pragma once

namespace overlay
{
// 在游戏渲染线程首次拿到设备后调用；内部自行保证只初始化一次。
void Start();

// 卸载：还原 hook、释放 ImGui。
void Stop();
}
