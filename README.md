# RemoteCodex Agent

仓库地址：[https://github.com/qmdq/remote-codex-backend](https://github.com/qmdq/remote-codex-backend)  
配套手机端：[https://github.com/qmdq/remote-codex-app](https://github.com/qmdq/remote-codex-app)

PC 端 Python Agent，是手机端 Codex Agent 工具的远程执行与安全中枢。手机端提供 Codex Agent 的移动操作界面；Codex 调用、命令执行、文件修改、系统指标、终端和屏幕控制都由这个 Agent 在 PC 上完成。它同时管理设备配对、授权项目、事件流、审批和访问边界。默认只监听 `127.0.0.1`，局域网手机直连时需要使用显式开放监听地址的配置。

## 能做什么

- **设备与接入**：生成配对码、审批/拒绝/吊销设备，只保存设备 Token 的 SHA-256 哈希。
- **项目与会话**：创建/导入项目、同步 Codex 本地项目、恢复 thread、同步 PC 聊天记录、支持多会话事件回放。
- **Codex 调用**：转发任务到本机 Codex SDK，支持模型选择、沙箱模式、中断和实时事件转发。
- **文件能力**：在授权目录内提供文件浏览、读取、保存、图片/HTML 辅助预览。
- **运维控制**：CPU/内存/磁盘/网络指标、PTY 终端、屏幕帧订阅和鼠标键盘基础输入。

## 预览

<div align="center">
  <img src="docs/screenshots/screenshot-1.png" width="49%" alt="RemoteCodex 聊天界面">
  <img src="docs/screenshots/screenshot-2.png" width="49%" alt="RemoteCodex 会话与任务状态">
  <br>
  <img src="docs/screenshots/screenshot-3.png" width="49%" alt="RemoteCodex 文件与预览">
  <img src="docs/screenshots/screenshot-4.png" width="49%" alt="RemoteCodex 远程屏幕">
</div>

## 快速启动

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[monitor,desktop,terminal]"
.\.venv\Scripts\python.exe -m app serve --config config.local.json
```

也可以双击 `start.bat` 启动。它会自动创建 venv、安装依赖、后台运行 Agent 并打开配置页；双击 `stop.bat` 停止。

复制 `config.local.example.json` 为 `config.local.json` 并修改授权目录后启动。该配置监听 `0.0.0.0:7800`，数据库在 `backend\runtime\remote-codex.sqlite`。手机填 `ws://<电脑局域网IP>:7800`；用 `ipconfig` 查看电脑的 IPv4 地址。

启动后会输出 `admin console: http://127.0.0.1:7801/admin?token=...`，并写入 `%TEMP%\RemoteCodex\admin-console.txt`。浏览器打开这个地址即可生成配对码、批准/拒绝手机、吊销设备，并查看后台配置；不需要再用配对命令。

默认配置使用当前工作目录作为唯一 allowed root，数据库在 `%LOCALAPPDATA%\RemoteCodex\agent.db3`。可以用 `--config` 指定 JSON 配置：

```powershell
remote-codex-agent serve --config config.example.json
```

## 设备配对

在 PC 浏览器打开控制台，点「生成配对码」；手机 App 在「设备」页点「开始配对」，进入独立配对页后填写服务器地址和 6 位配对码。控制台会显示待处理请求，点「批准」后手机自动保存 Token 并连接。

WebSocket 连接后，认证第一帧发送：

```json
{
  "v": 1,
  "type": "hello",
  "id": "1",
  "payload": {
    "device_token": "YOUR_DEVICE_TOKEN"
  }
}
```

配对前的第一帧使用 `pair.request`，载荷为 `{"code":"123456","device_name":"iPhone"}`。

## 协议

所有消息都是 JSON envelope：

```json
{
  "v": 1,
  "id": "req-1",
  "type": "project.create",
  "payload": {
    "name": "remoteAi",
    "path": "D:\\work\\code\\remoteAi"
  }
}
```

支持：

* `project.list` / `project.create` / `project.select`
* `codex.project.list` / `codex.project.import` (discover local Codex session directories and import allowed projects)
* `project.model`
* `codex.history`
* `file.list` / `file.read`
* `turn.start` / `turn.interrupt` / `thread.resume`
* `event.replay`
* `metrics.subscribe` / `metrics.unsubscribe`
* `screen.subscribe` / `screen.unsubscribe` / `screen.input`
* `terminal.start` / `terminal.input` / `terminal.resize` / `terminal.close`
* `ping`

服务端响应包括 `ready`、`ok`、`error`、`project.snapshot`、`codex.history.snapshot`、`file.list.snapshot`、`file.read.snapshot`、`codex.event`、`agent.event`、`event.synced`、`metrics`、`screen.frame`、`terminal.ready`、`terminal.output`、`terminal.exit`。

终端会话绑定当前 WebSocket 连接，只允许在已导入项目的工作目录中启动。Windows 安装可选依赖 `pywinpty` 后使用 PTY；未安装时降级到 `cmd.exe` 管道模式，`ready.capabilities.terminal_pty` 会返回 `false`。

Codex SDK 事件在 `codex.event.event` 字段中原样转发。

触摸控制依赖桌面控制扩展：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[monitor,desktop,terminal]"
```

## 安全模型

* Agent 只绑定 loopback；公网暴露由 frp 负责。
* 设备令牌随机生成，数据库只保存 SHA-256 哈希。
* 配对码短时效、限次尝试，并支持 PC 本地审批。
* allowed roots 之外的目录不能创建项目。
* turn 默认使用 Codex `workspace-write` 沙箱；SDK 或平台不可用时按配置降级为 `read-only`。
* 连接断开不会终止 turn，重连后可通过 seq replay 补齐事件。

## 开发验证

```powershell
python -m unittest discover -s tests -v
python -m app doctor
```

默认连接真实 `openai-codex` SDK。配置 `"codex": {"mode": "fake"}` 时使用内置 `FakeCodexGateway`，用于无模型调用的协议和集成验证。真实 SDK adapter 会延迟导入 `codex` 包，并在启动时输出可诊断的导入错误。

## License

GPL-3.0-or-later。商业闭源集成或分发请联系作者取得商业授权。
