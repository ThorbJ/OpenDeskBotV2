# Open Desk Bot V2 · Thorb fork（非官方 / unofficial）

**这是 Thorb 维护的 fork，不是官方版本。** 原项目是 [ai-maker-lab/OpenDeskBotV2](https://github.com/ai-maker-lab/OpenDeskBotV2)，
原作者没有参与、审核或认可这个构建。问题请报到 [ThorbJ/OpenDeskBotV2](https://github.com/ThorbJ/OpenDeskBotV2/issues)，不要打扰原作者。

**This is a fork maintained by Thorb, not an official release.** The original project is
[ai-maker-lab/OpenDeskBotV2](https://github.com/ai-maker-lab/OpenDeskBotV2); its authors did not build,
review or endorse these files. Please report problems to the fork, not to the original authors.

## 和官方版的区别 / What differs from upstream

只多了一项可选功能：**虚拟设备连接**。在浏览器里运行的虚拟小歪（同一份固件跑在虚拟硬件上）可以经本机
WebSocket 连到这台电脑上的服务，连上后和插 USB 的小歪一样使用。默认关闭，在控制台「设备连接 → 虚拟设备连接」
点「开启」；只监听本机 `127.0.0.1:9022`，只接受允许的网页来源（默认 `https://thorb.com` 和本机开发地址）。
插 USB 的行为和官方版相同。这项改动已整理成给上游的合并请求；上游合并后请改用官方版。

One optional feature only: a **virtual device connection**. A virtual robot running in a web page (the same
firmware on virtual hardware) can connect to this service over a local WebSocket and is then used exactly like
a robot plugged in over USB. Off by default; switch it on under Console → 设备连接 → 虚拟设备连接. It listens on
`127.0.0.1:9022` only and accepts only allowed web origins (default `https://thorb.com` and local development
addresses). USB behaviour is unchanged. The change has been prepared as a pull request to upstream; once merged,
please use the official release instead.

## 下载 / Downloads

| 文件 | 系统 |
|------|------|
| `OpenDeskBotV2-Thorb-unofficial-Windows-Setup.exe` | Windows 10/11 x64（需要 WebView2 运行时，系统通常已带） |
| `OpenDeskBotV2-Thorb-unofficial-macOS-arm64.dmg` | macOS 13+，Apple 芯片（M1 及以后） |

每个文件附 `.sha256` 校验值。Each file has a `.sha256` checksum next to it.

### 安装时的系统提示 / Security prompts

这些安装包**没有代码签名**（和官方 Windows 版一样），系统会提示：

- **Windows**：SmartScreen 显示「Windows 已保护你的电脑」时，点「更多信息」→「仍要运行」。
- **macOS**：把 OpenDeskBotV2 拖进「应用程序」后，第一次打开会提示「无法验证开发者」。在「应用程序」里
  **右键（或按住 Control 点按）OpenDeskBotV2 → 打开 → 打开**；或在「系统设置 → 隐私与安全性」底部点「仍要打开」。

These installers are **not code-signed**. On Windows choose *More info → Run anyway* in SmartScreen. On macOS,
right-click (Control-click) the app in Applications → *Open* → *Open*, or use *System Settings → Privacy & Security
→ Open Anyway*.

macOS 也可以按官方说明从源码用脚本安装（`service/start.sh`），本 fork 的源码在同一仓库的 `thorb-release` 分支。
On macOS you can also install from source with the upstream script (`service/start.sh`) using this fork's
`thorb-release` branch.

## 许可 / License

GPL-3.0，与上游相同；完整源码在本仓库。GPL-3.0, same as upstream; the complete source is in this repository.
