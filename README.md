# GetEggs

Local-First 的 AI Agent 工具每日自动签到助手（Python 核心引擎 + 零构建本地 Web 控制台）。

当前覆盖：

- **WorkBuddy**：读取桌面端登录态或受控浏览器会话，只读体检、状态预检与每日签到；签到接口 404/5xx 时安全中止，不误提交。
- **Qoder CN**：使用 PAT 兑换短期访问令牌，查询每日活动并完成签到（预检时只读，不提交）；每日活动以 **10:00 为界**，Qoder 的业务日固定为 `10:00`。
- **Trae 国内版（Trae CN）**：只读读取本地 Trae CN / Trae SOLO CN 客户端加密登录态与真实设备号，调用官方客户端同款接口完成每日签到；按自然日 `00:00` 重置，一台设备每天只能签一个账号。

---

## 核心能力

- **多工具调度**：单实例运行锁、随机抖动错峰、幂等预检、Provider 级异常隔离、部分失败通知。
- **业务日模型**：每个工具按各自的业务日统计；Qoder 固定 10:00 重置，避免这类工具在凌晨被误记为当天已完成。
- **本地账本**：SQLite WAL 本地账本、打卡日历、签到流水、执行批次与通知审计。
- **通知渠道**：Windows 10/11 原生系统弹窗（`winotify` + PowerShell WinRT 回退），并记录通知审计。
- **本地 Web 控制台**：浅色现代简约界面（FastAPI + 零外部 CDN 静态页），支持多工具勾选与批量签到/预检、总览看板、SSE 实时日志、打卡日历、运行批次/通知历史。
- **工具级配置**：自动调度开关、业务日起始时间、最早可执行时间、独立抖动、凭据与路径覆盖。
- **Windows 计划任务**：Web 或 CLI 一键安装/卸载；非管理员运行时自动请求 UAC 提权；执行时限按抖动预算自动计算；登录时补跑漏签。
- **无终端启动**：双击 `GetEggs.lnk` 后台启动并打开浏览器，先显示启动加载页，服务就绪后自动跳转；重复启动不会产生第二个实例。
- **安全**：仅监听 `127.0.0.1`，一次性启动令牌 + HttpOnly 会话 Cookie；PAT 与浏览器会话使用 Windows DPAPI 加密，不写入数据库或日志。

---

## 环境准备（Windows）

若系统尚未安装 `uv` 与 Python 3.12+：

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

2. **首次 WorkBuddy 受控浏览器授权**（桌面端启用 `$wbEncrypted` 加密时需要）：
   ```powershell
   uv run geteggs auth workbuddy
   ```

3. **打开本地 Web 控制台**（推荐直接双击 `GetEggs.lnk`）：
   ```powershell
   uv run geteggs open
   ```
   - 后台启动、不弹终端；服务就绪后自动打开浏览器；
   - 仅监听 `127.0.0.1`，默认端口 `18765`，被占用时自动选择空闲端口；
   - 已有控制台运行时会直接打开已有地址，不会重复启动；
   - 若本机没有 `GetEggs.lnk`，执行上述命令启动，或自行创建指向 `pythonw -m geteggs open` 的快捷方式。

4. **配置工具**：
   - WorkBuddy：确认登录态正常并开启「自动调度」；
   - Qoder：在「工具与设置」→ Qoder 卡片粘贴 PAT 并保存，再开启「自动调度」（Qoder 默认停用）；
   - Trae 国内版：安装并登录 Trae CN 客户端，在 Trae 卡片点击「立即体检」确认本地登录态，再开启「自动调度」（Trae 默认停用）。

5. **预检与正式签到**：
   ```powershell
   uv run geteggs run --dry-run
   uv run geteggs run
   ```

6. **测试 Windows 系统弹窗通知**：
   ```powershell
   uv run geteggs notify-test
   ```

7. **安装每天自动签到与登录漏签补跑**：
   ```powershell
   uv run geteggs schedule install --time 10:15
   ```
   也可在 Web 控制台「工具与设置」页一键安装或卸载；如当前非管理员，会请求一次 UAC 授权。
   默认 `10:15` 位于 Qoder 的 10:00 业务日重置之后；若把计划时间改到 10:00 之前，Qoder 会按上一个业务日判断。

更完整的界面说明与操作流程见 [USER_GUIDE.md](USER_GUIDE.md)。

---

## 运行测试

```powershell
uv run pytest
```

---

## 数据存储与安全说明

- 所有签到流水、运行批次、通知审计与设置均存储在本地 `%LOCALAPPDATA%\GetEggs\geteggs.db`（可通过环境变量 `GETEGGS_HOME` 覆盖）。
- `qoder_pat.json`、`workbuddy_browser_state.json` 与 `trae_auth.json` 在 Windows 下使用当前用户的 DPAPI 加密存储；兼容历史明文文件，并在下次写入时自动迁移为加密格式。
- Trae 国内版默认**只读**读取 `%APPDATA%\Trae CN\User\globalStorage\storage.json`（自动兼容 `TRAE SOLO CN` 客户端，也可在高级配置中指定路径），不会修改客户端文件；真实设备号、accessToken、refreshToken 与设备私钥不会以明文写入日志或返回前端。
- `ui-state.json` 保存当前 Web 服务地址与一次性启动令牌，仅用于本机打开页面；服务重启后令牌会自动更新。
- 程序**不存储明文密码**，也**不回传原始 Token 到前端 Web 页面**；遇到登录态失效、加密封装凭证或人机验证时会停止重试并提示手动处理。
- Qoder CN 与 Trae CN 使用非官方接口，接口字段或策略可能变化；GetEggs 会把异常隔离为单个工具的结果，不影响其他工具与历史数据。
- Trae CN 在 accessToken 过期后会按需续期；续期轮换 refreshToken，可能导致 Trae 客户端需要重新登录。Trae 卡片高级配置中的「允许自动续期」默认开启；如不希望自动续期，可关闭该开关，并在 accessToken 过期前打开客户端刷新登录态。
