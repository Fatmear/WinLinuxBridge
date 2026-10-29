<div align="center">

# WinLinuxBridge

**在 Linux 终端里操作 Windows 桌面 —— 隧道由 Windows 自己发起。**

用 Linux 上的自动化脚本驱动 Windows 的应用程序、图形界面、文件与本机服务：
不开入站端口、不做端口映射、两端都不要管理员权限、不依赖任何第三方中转。

**中文** | [English](README.en.md)

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11%20%7C%20Linux-lightgrey.svg)](#环境要求)
[![Python](https://img.shields.io/badge/python-3.13%20内置-informational.svg)](#开箱即用)
[![Version](https://img.shields.io/badge/version-1.3.0-success.svg)](CHANGELOG.md)

</div>

---

## 为什么需要它

Linux 服务器是跑自动化和 AI 代理的天然位置。但那些真正握着硬件的 Windows 机器 —
串口、烧录工具、厂商上位机、内网接口 — 通常从 Linux 根本够不着：没有入站端口、
没有管理员权限，而端口映射或公网中转要么被禁止，要么一碰就断。

WinLinuxBridge 把方向反过来：**Windows 主动连 Linux**，用 SSH 建立反向隧道；
Linux 只要访问本机 `127.0.0.1` 就能抵达跑在 Windows 上的控制代理。
两端都没有对外监听，也不需要往系统里装任何东西。

```
   Linux 主机                                         Windows 桌面
   ──────────                                         ────────────
   ~/WinLinuxBridge/  ────── 唯一交付目录 ──────  %USERPROFILE%\WinLinuxBridge\
   ├── win/            源码、图形界面、代理
   ├── linux/          wl 命令行
   ├── tools/          验证套件
   ├── docs/           手册
   └── shared/         三条同步车道

   wl run / wl job ──► 127.0.0.1:18080 ══ 反向 SSH 隧道 ══► 127.0.0.1:8787 代理
                      （由 Windows 发起）                    │
                                        subprocess · Win32 · urllib
                                                               ▼
                                                  程序 · 图形界面 · 本机 HTTP
```

## 核心特性

**为真实网络设计的执行语义**

- 每条命令都成为落在 Windows 磁盘上的**持久化任务**：状态、PID、时间戳、退出码、
  输出文件在客户端超时、隧道断开、代理重启之后依然可查。
- **三个超时严格分离**：连接、执行、客户端等待。客户端等不到时只报"状态未知/仍在运行"，
  绝不谎报失败。
- **重试安全**：`request_id` 相同则返回原任务，不会把板子烧两遍。
- **取消有边界**：只杀目标任务自己的进程树，并回报清理结果。

**尊重上下文窗口的输出策略**

- 完整输出流式落盘（32 MB 上限，运行中持续施加），对外只返回有界摘要 + 字节数 + SHA-256。
- 支持按字节游标增量读取（`--since`）。
- 厂商工具的多余字段原样保留 —— 桥接绝不改写工具结果。

**操作 Windows 的能力**

- 结构化执行 `exe + argv[] + cwd + env`（绕开所有 shell 引号层），也支持 PowerShell / cmd。
- 截图（GDI + 标准库 zlib 生成 PNG，不依赖 Pillow）、鼠标、键盘、剪贴板、
  窗口列举/置前/关闭，以及反向代理访问 Windows 本机 HTTP 服务。
- 文件读写限定在用户目录白名单内，越界返回 `403`。

**零安装、零提权**

- 内置便携 CPython（含可用的 Tk）与一次性密码登录用的 `plink`。
- Windows 不需要管理员权限；Linux 只用到登录用户的 `$HOME`。
- 只有一个交付目录；双击启动 + 图形界面完成首次配置。

## 环境要求

| 侧 | 需要 |
| --- | --- |
| Windows 10/11 | 系统自带 OpenSSH 客户端（`ssh.exe`、`scp.exe`）。不需要管理员，不需要装 Python。 |
| Linux | `bash`、`curl` 或 `python3`、一个 SSH 账号。不需要 root，不装系统包。 |
| 网络 | Windows 能 SSH 到 Linux。Linux **不需要**任何到 Windows 的入站通路。 |

## 快速开始

**1. Windows：双击 `win\start-gui.cmd`**

填写 Linux 地址、账号、密码，点「安装并启动」。首次会先显示服务器主机指纹让你确认，
随后自动完成：生成密钥 → 装公钥 → 上传程序 → 配置令牌 → 注册自启 → 启动。

密码默认不落盘；勾选「记住密码」才保存，且用 Windows DPAPI 加密，绑定当前
Windows 账户与本机。

命令行等价做法：

```powershell
$env:WL_LINUX_PASSWORD = '<只在本地会话输入，不落盘>'
powershell -ExecutionPolicy Bypass -File win\install-win.ps1 -NonInteractive
```

**2. Linux：直接开始用**

```bash
wl status                 # 一屏看清隧道、代理、同步车道
wl run "ipconfig"         # 在 Windows 上执行命令
wl doctor                 # 版本、可用 shell、各层超时、路径、任务统计
```

## 命令一览

| 命令 | 用途 |
| --- | --- |
| `wl status` / `wl ping` / `wl doctor` | 健康检查、代理身份、完整环境报告 |
| `wl run [选项] -- <命令>` | 执行并等待；底层是持久化任务 |
| `wl job start\|status\|logs\|result\|cancel\|list` | 完整任务控制 |
| `wl batch --file plan.json` | 多步计划 + 资源键互斥 |
| `wl ps` / `wl focus` / `wl shot` | 窗口列举、置前、截图 |
| `wl click` / `wl move` / `wl type` / `wl key` | 鼠标与键盘 |
| `wl copy` / `wl paste` | 剪贴板 |
| `wl http <url>` | 访问 Windows 本机 HTTP 服务 |
| `wl ls` / `wl get` / `wl put` / `wl rm` | 用户目录内的文件传输 |
| `wl start <路径\|URL>` / `wl lanes` | 在 Windows 打开、查看同步车道 |

`wl run` 选项：`--shell`、`--timeout`、`--wait`、`--no-wait`、`--json`、
`--output text|json|summary`、`--request-id`、`--resource`、`--cwd`、`--env`、`--exe`。

## 任务模型

```
        启动                 运行                  结束
客户端 ──► /job/start ──► QUEUED ──► RUNNING ──► SUCCEEDED | FAILED | TIMEOUT
                            │            │                | CANCELLED | LOST
                            │            └── cancel ──────┘
                            └── 资源键被其它任务占用，排队
```

**状态**：`QUEUED`、`RUNNING`、`SUCCEEDED`、`FAILED`、`TIMEOUT`、`CANCELLED`、
`LOST`、`SPAWN_ERROR`。

**错误码**：`PROCESS_EXIT_NONZERO`、`TIMEOUT_KILLED`、`CANCELLED_BY_REQUEST`、
`SPAWN_FAILED`、`JOB_LOST`、`CLIENT_WAIT_TIMEOUT`、`RESOURCE_WAIT_TIMEOUT`、
`BRIDGE_UNREACHABLE`。

**退出码**

| 码 | 含义 |
| --- | --- |
| `0` | 任务成功 |
| `1` | 程序跑完但返回非零 —— 真实的业务/测试失败 |
| `124` | 客户端等待超时；**状态未知，不是失败** |
| `125` | 桥接不可达 |
| `126` | 程序无法启动 |
| `130` | 任务被取消 |

判定顺序永远是：`transport_ok` → `state` → `error_code` → `exit_code`。
**桥接调用成功，不代表程序自己的测试通过。**

任务落盘在 `%USERPROFILE%\.winlinuxbridge\jobs\<job_id>\`（`job.json`、`stdout.log`、
`stderr.log`），默认保留 7 天（`WL_JOB_KEEP_DAYS`）。代理重启时会把随它一起死掉的
`RUNNING` 标为 `LOST`，不会留下让客户端永远干等的假运行态。

## 三个超时

| 超时 | 由谁执行 | 默认值 | 结果 |
| --- | --- | --- | --- |
| 连接 | Linux 客户端（`WL_CONNECT_TIMEOUT`） | 5 秒 | 退出码 125，`BRIDGE_UNREACHABLE` |
| 执行 | Windows 代理（`--timeout`） | 120 秒 | 状态 `TIMEOUT`，杀进程树，`TIMEOUT_KILLED` |
| 客户端等待 | Linux 客户端（`--wait`） | 执行超时 + `WL_WAIT_MARGIN`（30 秒） | 退出码 124，仍在运行 |

## 共享目录

一个交付目录，两端层级完全一致。三条车道让冲突不可能发生：

| 车道 | 写入方 | 读取方 | 同步方式 |
| --- | --- | --- | --- |
| `shared/from_win` | Windows | Linux | 每 30 秒推送 |
| `shared/to_win` | Linux | Windows | 每 30 秒拉取 |
| `shared/shared` | 两边 | 两边 | 拉取 → 较新者胜合并 → 推回 |

**不传播删除**：要真删请用 `wl rm`。需要立刻生效而不等 30 秒？`wl put` / `wl get`
走同一条隧道即时传输。

## 安全模型

- 代理只监听 `127.0.0.1`，从 Linux 只能经 Windows 主动建立的反向隧道抵达。
- 每个请求都带 `Authorization: Bearer <令牌>`。令牌在安装时生成，存于用户级环境变量与
  Windows 上受 ACL 限制的文件，以及 Linux 上 `0600` 的文件。
- 两把专用密钥。隧道密钥为
  `restrict,port-forwarding,permitlisten="127.0.0.1:18080"` —— 即使泄露也只能顶开一个
  回环端口，拿不到 shell。同步密钥为 `no-pty,no-port-forwarding`。
- Linux 密码只用于一次性追加公钥，来自本地会话环境变量或交互输入，不写入任何文件与日志。
- 文件接口限制在交付目录与 `%USERPROFILE%` 组成的白名单内，越界返回 `403`。
- `wl doctor` 只报告版本、shell、限额与路径，绝不包含凭据。

## 开箱即用

发布压缩包自带全部依赖：

| 组件 | 版本 | 许可 | 用途 |
| --- | --- | --- | --- |
| CPython（embeddable） | 3.13 | PSF | 便携解释器 |
| Tcl/Tk | 8.6 | Tcl/Tk | 配置界面 |
| PuTTY `plink` | 0.77 | MIT | 装公钥时的一次性密码登录 |

从公开源重新构建运行时：

```powershell
powershell -ExecutionPolicy Bypass -File tools\build-runtime.ps1
```

脚本会下载官方 embeddable 包，并补上它**故意不含**的 Tcl/Tk 与 `_tkinter.pyd`，
最后用真实的 `Tk()` 窗口做自检。

## 从源码构建（本仓库）

本仓库只放**源码** —— `win/runtime/` 与 `win/vendor/*.exe` 由构建脚本产出，
放在发布压缩包里，不进 git。

```powershell
powershell -ExecutionPolicy Bypass -File tools\build-runtime.ps1   # 便携 Python + Tk
powershell -ExecutionPolicy Bypass -File tools\package-release.ps1  # 生成发布压缩包
```

## 验证

上文的每一条能力都由脚本在真实隧道上验证过。

| 套件 | 覆盖内容 | 项数 |
| --- | --- | --- |
| `tools/verify.ps1` | 静态检查、代理端点、任务引擎、取消隔离、输出限额 | 38 |
| `tools/verify.sh` | Linux 布局、权限、隧道、往返执行 | 20 |
| `tools/verify_jobs.sh` | 超时语义、去重、游标、批处理、doctor | 17 |
| `tools/verify_cancel.sh` | 取消边界与桥接存活 | 6 |

```powershell
powershell -ExecutionPolicy Bypass -File tools\verify.ps1
```

```bash
~/WinLinuxBridge/tools/verify.sh
~/WinLinuxBridge/tools/verify_jobs.sh
~/WinLinuxBridge/tools/verify_cancel.sh
```

## 已知限制

- 图形界面操作要求 Windows 会话处于解锁状态；锁屏时截图会是黑的。
- 自启在登录时触发，因此重启后需有人登录，桥接才会恢复。
- 同步是周期性全量比对，只适合中小目录；大文件请用 `wl put` / `wl get`。
- 隧道只绑回环，局域网内其它主机无法借用（这是设计意图）。
- 一个桥接实例对应一台 Windows 桌面；多个目标请分别部署。

## 文档

- [`docs/USAGE.md`](docs/USAGE.md) —— 部署、配置、排障
- [`docs/AI-AGENT.md`](docs/AI-AGENT.md) —— 写给自动化与 AI 代理的操作手册
- [`CHANGELOG.md`](CHANGELOG.md) —— 版本历史

## 参与贡献

欢迎 Issue 与 PR。提交前请跑完四套验证，保持 shell 脚本为 LF
（`python tools/normalize-lf.py`），并且**不要**提交凭据、主机名或账号名。

## 许可

[MIT](LICENSE)。随包第三方组件保留各自许可，原文见 `win/vendor/`.

```powershell
powershell -ExecutionPolicy Bypass -File tools\fetch-vendor.ps1        # plink + 许可证
powershell -ExecutionPolicy Bypass -File tools\build-runtime.ps1       # 便携 Python + Tk
python tools\package_release.py                                        # 发布压缩包
```
