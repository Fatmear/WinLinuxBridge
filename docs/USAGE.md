# 使用手册

## 0. 原理（一句话）

Windows 主动用 `ssh -R` 连到 Linux，把 `127.0.0.1:18080`（Linux 侧）转发到
`127.0.0.1:8787`（Windows 侧的控制代理）。于是 Linux 访问本机 18080 就能操作 Windows；
Windows 同时用 `scp` 把这个交付目录镜像到本地并跑三车道同步。没有任何入站监听，
没有第三方穿透服务。

```
Linux wl ──► 127.0.0.1:18080 ══ SSH 隧道（Windows 发起）══► 127.0.0.1:8787 winbridge_agent.py
                                                              │
                                      subprocess / Win32 API / urllib ──► Windows 应用、GUI、本机接口
```

## 1. 组件

### 图形界面（推荐给普通用户）

| 文件 | 作用 |
| --- | --- |
| `win\start-gui.cmd` | **双击入口**。无 Python 也能跑 |
| `win\WinBridgeGUI.pyw` | 图形界面：配置、部署、启停、状态、日志 |
| `win\runtime\` | 便携 CPython 3.13 + Tcl/Tk（含 tkinter） |
| `win\vendor\plink.exe` | 首次密码登录装公钥（PuTTY 0.77, MIT） |

界面上的四块：

1. **服务器连接** — 地址、账号、SSH 端口、密码、远端/本地目录。
   「测试连接」会检查 TCP 可达性并显示服务器主机指纹。
2. **一键部署** — 「安装并启动」= 生成密钥 + 装公钥 + 上传 + 令牌 + 自启 + 启动。
   「仅更新程序与配置」= 换代码后用，不动密钥与令牌。
3. **运行状态** — 代理 / 隧道 / 共享目录三盏灯，另有启动、停止、刷新。
4. **工具** — 打开本地目录、日志目录、使用手册、跑自检。

保存位置：

| 内容 | 位置 | 是否可带走 |
| --- | --- | --- |
| 非密钥设置 | `%USERPROFILE%\.winlinuxbridge\bridge.conf` | 可以（明文，无秘密） |
| 记住的密码 | `%USERPROFILE%\.winlinuxbridge\credentials.dat` | **不行**（DPAPI 绑本机本账号） |
| 令牌 | 用户环境变量 `WL_AGENT_TOKEN` + `agent.token` | 不行（安装时生成） |
| 日志 | `%USERPROFILE%\.winlinuxbridge\logs\` | 可以（只含诊断信息） |

### 命令行与后台组件

| 文件 | 作用 |
| --- | --- |
| `win/winbridge_agent.py` | 控制代理，只绑 `127.0.0.1:8787`，Bearer 令牌鉴权 |
| `win/winbridge_jobs.py` | 持久化任务引擎：状态机、去重、取消、输出落盘、资源互斥 |
| `win/winbridge_supervisor.ps1` | 常驻循环：守护代理 + 隧道 + 同步 |
| `win/winbridge_sync.ps1` | 三车道 + 程序文件刷新（只用 `scp`） |
| `win/winbridge_common.ps1` | 配置/进程/日志/API 公共函数 |
| `win/install-win.ps1` | 一次性安装（密钥、上传、令牌、自启） |
| `win/start-win.cmd` | 入口：`start-win.cmd` 后台 / `start-win.cmd console` 前台 |
| `linux/wl` | Linux 命令行 |
| `linux/install-linux.sh` | Linux 目录与权限初始化 |
| `tools/verify.ps1` / `tools/verify.sh` | 两端自检 |
| `tools/build-runtime.ps1` | 重建 `win\runtime`（便携 Python + tkinter） |

### 依赖：全部随包，目标机器零安装

| 需要什么 | 从哪来 | 缺失时 |
| --- | --- | --- |
| Python 3.13 + tkinter | `win\runtime\`（随包） | `start-gui.cmd` 回退系统 Python；再没有就提示重建 |
| 密码登录 SSH | `win\vendor\plink.exe`（随包） | 提示需要 plink，或改用免密方式 |
| `ssh.exe` / `scp.exe` | Windows 10/11 自带 OpenSSH | 提示启用「可选功能 → OpenSSH 客户端」 |
| PowerShell | Windows 自带 | 不支持（必装组件） |

重建运行时（维护者用，需要一台装有 CPython 3.13 的机器）：

```powershell
powershell -ExecutionPolicy Bypass -File tools\build-runtime.ps1
```

脚本会自动下载 python.org 的 embeddable 包，并从本机 CPython 取 tcl/tk 与
`_tkinter.pyd`（embeddable 包故意不含 tkinter），最后跑一次 `Tk()` 自检。

## 2. 文件与配置放在哪

| 内容 | Windows | Linux |
| --- | --- | --- |
| 程序（交付目录） | `%USERPROFILE%\WinLinuxBridge`（镜像） | `~/WinLinuxBridge`（权威） |
| 本机设置 | `%USERPROFILE%\.winlinuxbridge\bridge.conf` | `~/WinLinuxBridge/config/bridge.env` |
| 令牌 | `%USERPROFILE%\.winlinuxbridge\agent.token` + 用户环境变量 `WL_AGENT_TOKEN` | 同一变量名，`bridge.env`（0600） |
| 日志 | `%USERPROFILE%\.winlinuxbridge\logs\` | `~/WinLinuxBridge/logs/` |
| 截图 | `%USERPROFILE%\.winlinuxbridge\shots\` | 由 `wl shot` 直接取回本地 |

设置项（`bridge.conf`，键值对，`#` 开头为注释）：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `WL_LINUX_HOST` | （空，首次填写） | Linux 地址 |
| `WL_LINUX_USER` | （空，首次填写） | Linux 账户 |
| `WL_SSH_PORT` | `22` | SSH 端口 |
| `WL_REMOTE_ROOT` | 空 → `/home/<账号>/WinLinuxBridge` | Linux 交付目录 |
| `WL_LOCAL_ROOT` | `%USERPROFILE%\WinLinuxBridge` | Windows 镜像 |
| `WL_AGENT_PORT` | `8787` | 代理端口（只用回环） |
| `WL_TUNNEL_PORT` | `18080` | 隧道在 Linux 回环上占用的端口 |
| `WL_SYNC_INTERVAL` | `30` | 三车道间隔（秒） |
| `WL_PROGRAM_REFRESH` | `900` | 程序文件刷新间隔（秒） |

> `win\bridge.conf.example` 只是示例。真正的配置放在 `%USERPROFILE%\.winlinuxbridge`,
> 因为 `win\` 目录会被 Linux 版本覆盖。

## 3. 常用命令

### Windows（图形界面）

```
双击 win\start-gui.cmd            打开控制面板（无控制台窗口）
win\start-gui.cmd console         同前，但保留控制台便于看诊断信息
```

### Windows（命令行）

```
install-win.ps1            一次性安装（-NonInteractive 供界面调用）
install-win.ps1 -SkipKeys  只刷新程序与配置，不动密钥与令牌
start-win.cmd              后台启动（守护代理 + 隧道 + 同步）
start-win.cmd console      前台启动，看实时日志
winbridge_sync.ps1 -Mode program|lanes|fromwin|towin|shared|all
tools\verify.ps1           自检（不需要隧道）
```

### Linux：任务与超时

`wl run` 会创建**持久化任务**，因此超时不再等于失败。

```bash
wl run [--shell auto|powershell|cmd|none] [--timeout S] [--wait S]
       [--json] [--output text|json|summary] [--request-id ID] [--resource KEY]
       [--no-wait] [--cwd P] [--env K=V] [--exe PROG] -- <命令或参数...>

wl job start  <同上>            # 只启动，立即返回 job id
wl job status <id> [--wait S]    # 排队/运行/终态
wl job logs   <id> --since N [--stream stderr] [-f] [--json]
wl job result <id> [--output summary|json|text]
wl job cancel <id>              # 只杀该任务的进程树
wl job list [--limit N] [--request-id ID]
wl batch --file plan.json       # 多步 + 资源互斥
wl doctor [--json]              # 版本/shell/超时/路径/任务统计
```

三个超时的关系：

| 名称 | 在哪里生效 | 默认 | 触发后 |
| --- | --- | --- | --- |
| 连接 `WL_CONNECT_TIMEOUT` | Linux 客户端 | 5 s | 退出码 125 `BRIDGE_UNREACHABLE` |
| 执行 `--timeout` | Windows 代理 | 120 s | 状态 `TIMEOUT`，`taskkill /T`，`TIMEOUT_KILLED` |
| 客户端等待 `--wait` | Linux 客户端 | 执行超时 + `WL_WAIT_MARGIN`(30s) | 退出码 124，状态未知/仍在运行 |

任务状态：`QUEUED` `RUNNING` `SUCCEEDED` `FAILED` `TIMEOUT` `CANCELLED` `LOST` `SPAWN_ERROR`。
错误码：`PROCESS_EXIT_NONZERO` `TIMEOUT_KILLED` `CANCELLED_BY_REQUEST` `SPAWN_FAILED`
`JOB_LOST` `CLIENT_WAIT_TIMEOUT` `RESOURCE_WAIT_TIMEOUT` `BRIDGE_UNREACHABLE`。

任务落盘：`%USERPROFILE%\.winlinuxbridge\jobs\<job_id>\`
（`job.json` 元数据、`stdout.log`、`stderr.log`；默认保留 7 天，`WL_JOB_KEEP_DAYS` 可调）。
代理重启时会把随它一起死掉的 `RUNNING` 任务标成 `LOST`，不会留下永远等不到的假状态。

### Windows（图形界面）

```
双击 win\start-gui.cmd            打开控制面板（无控制台窗口）
win\start-gui.cmd console         同前，但保留控制台便于看诊断信息
```

### Windows（命令行）

```
install-win.ps1            一次性安装（-NonInteractive 供界面调用）
install-win.ps1 -SkipKeys  只刷新程序与配置，不动密钥与令牌
start-win.cmd              后台启动（守护代理 + 隧道 + 同步）
start-win.cmd console      前台启动，看实时日志
winbridge_sync.ps1 -Mode program|lanes|fromwin|towin|shared|all
tools\verify.ps1           自检（不需要隧道）
```

### Linux（`wl`）

```
wl status                      隧道/代理/三车道一屏
wl ping                        代理身份（主机、用户、分辨率、python）
wl run [--shell auto|powershell|cmd|none] [--timeout S] [--cwd P] <命令>
wl ps                          可见窗口列表
wl focus <标题片段>            置前
wl shot [out.png]              截图到本地
wl click <x> <y> [--double|--right|--middle]
wl move <x> <y>
wl type <文本>                 输入文本（支持中文）
wl key CTRL+S                  组合键
wl copy <文本> / wl paste      剪贴板
wl start <路径或 URL>          在 Windows 上打开
wl http <url> [-X M] [-d BODY] [-H 'K: V']
wl ls [路径]                   共享目录/用户目录列表
wl get <win 路径> [本地]       下载
wl put <本地文件> [win 路径]   上传
wl rm <win 路径> [--dir]
wl lanes                       三车道结构
```

路径规则：`ls/get/put/rm` 的相对路径基于 Windows 的
`%USERPROFILE%\WinLinuxBridge`；绝对路径必须落在 `%USERPROFILE%` 内，否则返回 403。

### 任务管理

```powershell
schtasks /Query /TN WinLinuxBridge-Watchdog
schtasks /Run  /TN WinLinuxBridge-Watchdog
Get-Content "$env:USERPROFILE\.winlinuxbridge\logs\supervisor.log" -Tail 40
Get-Content "$env:USERPROFILE\.winlinuxbridge\logs\tunnel.log" -Tail 40
```

## 4. 共享目录

| 车道 | 谁写 | 谁读 | 同步方式 |
| --- | --- | --- | --- |
| `shared/from_win` | Windows | Linux | Windows 每 30s `scp` 推 |
| `shared/to_win` | Linux | Windows | Windows 每 30s `scp` 拉 |
| `shared/shared` | 两边 | 两边 | 拉回→按修改时间较新者合并→推回 |

- **不传播删除**：删掉一端文件，另一端仍保留。需要真删就两端都删，
  或用 `wl rm` 直接删 Windows 那一份。
- `shared/shared` 同一文件两端同时改动时，修改时间较新的胜出。
- 需要“立刻生效”而不是等 30s，用 `wl put` / `wl get`。

## 5. 安全模型

1. 代理只监听 `127.0.0.1`，从 Linux 只能经隧道到达。
2. 每个请求都要 `Authorization: Bearer <token>`；令牌随机 48 位十六进制，
   只存在于 Windows 用户环境变量/ACL 文件与 Linux 0600 文件。
3. 两把专用密钥：
   - `wlbridge_tunnel`：`restrict,port-forwarding,permitlisten="127.0.0.1:18080"`
     —— 即使泄露，也只能顶开那一个回环端口，拿不到 shell。
   - `wlbridge_sync`：`no-pty,no-port-forwarding,no-agent-forwarding,no-x11-forwarding`
     —— 只用于 `scp`/只读命令。
4. 安装时的 Linux 密码只用于一次 `authorized_keys` 追加：
   - 可选放在**本机会话环境变量** `WL_LINUX_PASSWORD`（不落盘），否则交互输入；
   - 用完立即 `Remove-Variable`；不写日志、不写文件、不写仓库。

## 6. 已知限制

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 锁屏后截图全黑 / 点击无效 | Windows 会话隔离 | 保持会话解锁（见 Q-2） |
| 重启 Windows 后要等登录才恢复 | 自启是“登录触发”，存密码需凭据 | 登录后 10~30s 内恢复；可配自动登录 |
| `scp` 每 30s 全量推 `shared/shared` | 不引入 rclone 等额外二进制 | 该目录只放中小文件；大文件走 `wl put/get` |
| 首连 `plink` 报 host key | 主机指纹变化 | `install-win.ps1 -HostKey SHA256:...` |
| 代理起不来 `cannot bind` | 端口被旧进程占用 | 任务管理器结束 `python.exe`（winbridge_agent），或改 `WL_AGENT_PORT` |

## 7. 排障顺序

```bash
# Linux
wl status                     # 隧道是否监听？令牌是否 present？
ss -tln | grep 18080
tail -20 ~/WinLinuxBridge/logs/*.log 2>/dev/null
```
```powershell
# Windows
Get-Content "$env:USERPROFILE\.winlinuxbridge\logs\supervisor.log" -Tail 30
Get-Content "$env:USERPROFILE\.winlinuxbridge\logs\tunnel.log" -Tail 30
Get-Process ssh, python | Select-Object Id, ProcessName
Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" | Select-Object -Expand CommandLine
```
判断顺序：**代理活着 → 隧道进程在 → Linux 回环在听 → `wl ping` 通**。
哪一步断，就从那一步往上看日志.
