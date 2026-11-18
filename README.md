# GetEggs

Local-First 的 AI Agent 工具每日自动签到与积分追踪助手（Python 核心引擎 + 零构建本地 Web 控制台）。

当前阶段已完成：
- **核心调度引擎**：单实例防重文件锁、随机抖动错峰、幂等前置预检、SQLite WAL 账本、失效告警去重熔断、Provider 级异常隔离与部分失败通知。
- **首发适配器**：`WorkBuddy`（本地已登录前提下自动读取桌面端登录态 `workbuddy-desktop.info`，支持只读体检、令牌内存刷新、状态预检与每日签到；签到状态接口 404/5xx 时安全中止，不误提交）。
- **通知渠道**：Windows 10/11 原生系统弹窗通知（`winotify` + PowerShell WinRT 回退），并记录通知审计。
- **本地 Web 控制台**：浅色现代简约界面（FastAPI + 零外部 CDN 静态页），支持多工具勾选与批量签到/预检、总览看板、近 30 天趋势图、SSE 实时日志流、本月打卡日历、运行批次/通知历史、Provider 级抖动与最早时间、Windows 计划任务一键安装/卸载，严格仅绑定 `127.0.0.1` 并附带一次性启动令牌认证。

---

## 环境准备（Windows）

若系统尚未安装 `uv` 与 Python 3.12+，可先安装 `uv`：

```powershell
winget install --id=astral-sh.uv -e
uv python install 3.12
```

在项目根目录同步虚拟环境与依赖：

```powershell
uv sync --extra dev
```

---

## 快速上手

1. **检查本机客户端与登录态**（只读，不发送签到请求）：
   ```powershell
   uv run geteggs doctor
   ```

2. **首次受控浏览器授权**（当新版 WorkBuddy 桌面端启用 `$wbEncrypted` 加密时，运行一次在 Edge 中登录授权即可）：
   ```powershell
   uv run geteggs auth workbuddy
   ```

3. **预检试跑（Dry-Run）与正式签到**：
   ```powershell
   uv run geteggs run --dry-run
   uv run geteggs run
   ```

3. **测试 Windows 系统弹窗通知**：
   ```powershell
   uv run geteggs notify-test
   ```

4. **打开本地 Web 控制台**：
   ```powershell
   uv run geteggs ui
   ```
   仅监听 `127.0.0.1`。默认端口被占用时会自动选择空闲端口；如果已有控制台在运行，再次执行会直接提示已有 URL，不会重复启动。

5. **注册每天自动签到与登录漏签补跑（Windows 计划任务）**：
   ```powershell
   uv run geteggs schedule install --time 10:15
   ```
   也可在 Web 控制台的「调度与设置」页一键安装或卸载。

---

## 运行测试

```powershell
uv run pytest
```

---

## 数据存储与安全说明

- 所有签到流水、运行批次、通知审计与设置均存储在本地 `%LOCALAPPDATA%\GetEggs\geteggs.db`（可通过环境变量 `GETEGGS_HOME` 覆盖）。
- 受控浏览器会话 `workbuddy_browser_state.json` 在 Windows 下使用当前用户的 DPAPI 加密存储；兼容历史明文文件，并在下次写入时自动迁移为加密格式。
- 程序**不存储明文密码**，也**不回传原始 Token 到前端 Web 页面**；遇到登录态失效、加密封装凭证或人机验证时会自动停止重试并弹窗提示手动处理。
