# 给 Linux 上的 AI / 自动化用的操作手册

你可以把这台机器（Linux）当成主控，通过 `wl` 直接操作那台 Windows。
不需要图形界面、不需要人在旁边。

## 0. 三条铁律（先读完再动手）

1. **`transport_ok` 为 true 只说明"桥接通了"**，不代表测试通过。
   判定顺序永远是：`transport_ok` → `state` → `error_code` → `exit_code`。
2. **退出码 124 不是失败**。它表示"我等到极限了，任务还在跑，状态未知"。
   这时**不要重跑**，用 `wl job status/logs` 去看。
3. **要重试就先给 `--request-id`**。同一个 id 只会得到同一个任务，
   不会把烧录/重启/串口测试跑第二遍。

## 1. 开工三件事

```bash
wl status            # 隧道 + 代理 + 三车道；'tunnel: OK' 才继续
wl doctor --json     # 版本、可用 shell、超时上限、可访问路径、任务统计
wl lanes             # 看共享目录里有什么
```

如果 `wl status` 显示 `tunnel: not listening`：**不要循环重试**，报告
"Windows 侧守护未运行"，并提示在 Windows 上双击 `win\start-gui.cmd` 点「启动」。
守护每 10s 自愈一次，等 10~30s 再试一次即可。

## 2. 短命令

```bash
wl run "dir C:\Users"                       # 默认 PowerShell
wl run --shell cmd "ipconfig /all"
wl run --exe ping -- -n 2 127.0.0.1         # 结构化 argv，绕开所有引号层
wl run --json "Get-Date"                    # 完整结构化结果
```

`--exe ... -- args...` 是**首选**写法：路径含空格、单引号、中文、UNC 时不会出错。

## 3. 长任务（本手册最重要的一节）

```bash
# 1) 给一个稳定的 request-id：网络抖动后重发同一条，不会重复执行
wl run --request-id test-$(date +%Y%m%d-%H%M%S)-mesh --timeout 900 \
       --no-wait "run_mesh_test.ps1"        # 立刻返回 job id
# 2) 之后所有查询都用这个 id，随时可查、可断线重连
wl job status  <id> --json
wl job logs    <id> --since <cursor>        # 用返回的 next 作为下次的 since
wl job result  <id> --output summary        # 摘要：状态/退出码/字节数/sha256/路径
wl job cancel  <id>                         # 只杀这个任务的进程树
```

不要让 `wl run` 一直挂着等：`--no-wait` + 轮询 `job status` 更稳，
重连后还能接着看，不会因为一次网络抖动就丢掉任务。

## 4. 大输出：不要灌进上下文

```bash
wl run --output summary "Get-ChildItem C:\Reports -Recurse | Select-Object -First 500"
wl run --json "..." | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["stdout"]["bytes"], d["stdout"]["path"], d["stdout"]["sha256"])'
```

规则：

- 默认 `text` 模式对超大输出只给 **head + tail + 省略字节数**，全文在
  `stdout_path`，并附 `sha256` 供校验。
- 需要原文时用 **游标**读，不要一次读几十 KB：

```bash
CUR=0
while :; do
  OUT=$(wl job logs "$ID" --since "$CUR" --json)
  printf '%s' "$OUT" | python3 -c 'import json,sys;print(json.load(sys.stdin)["text"],end="")'
  CUR=$(printf '%s' "$OUT" | python3 -c 'import json,sys;print(json.load(sys.stdin)["next"])')
  DONE=$(printf '%s' "$OUT" | python3 -c 'import json,sys;print(json.load(sys.stdin)["eof"])')
  [ "$DONE" = "True" ] && break
done
```

- **不要**擅自改写工具输出的原文（比如 OmniCOM 的 hex 字段）。
  桥接只做"截断/摘要/落盘"，原始字节永远完整保存在 `stdout.log`。

## 5. 多步与串口互斥

一次提交多步，减少跨桥往返：

```bash
cat > /tmp/plan.json <<'JSON'
{"resource": "serial:COM139", "timeout": 120,
 "steps": [
   {"name": "probe",  "cmd": "echo probe", "shell": "cmd"},
   {"name": "config", "exe": "OmniCOM.exe", "argv": ["cli", "info"]},
   {"name": "run",    "exe": "OmniCOM.exe", "argv": ["cli", "send", "--port", "COM139"]}
 ]}
JSON
wl batch --file /tmp/plan.json
```

- 同一 `resource` 键的任务在 Windows 侧**串行**执行，不会交错。
- 不同串口给不同键（`serial:COM139` / `serial:COM143`）才能并行。
- 观察类任务（读日志）与设备控制任务建议用不同 resource，
  避免观察任务抢走控制权。
- **不要**给 OmniCOM 另开端口或绕过它的 GUI 转发规则。

## 6. GUI 自动化

```bash
wl ps                     # 找目标窗口
wl focus "记事本"
wl key CTRL+N
wl type "hello"
wl shot /tmp/after.png    # 每步之后截图确认
```

## 7. 文件交换

| 方向 | 做法 | 延迟 |
| --- | --- | --- |
| Linux → Windows，立刻 | `wl put ./a.csv shared/to_win/a.csv` | 立即 |
| Linux → Windows，不着急 | `cp ./a.csv ~/WinLinuxBridge/shared/to_win/` | ≤ 30s |
| Windows → Linux，立刻 | `wl get "C:\Users\<you>\a.csv" ./a.csv` | 立即 |
| 读 Windows 目录 | `wl ls .` / `wl ls C:\Users\<you>\Downloads` | 立即 |

相对路径基于 Windows 的 `%USERPROFILE%\WinLinuxBridge`；绝对路径只能在
`%USERPROFILE%` 之内，越界返回 403。

## 8. 访问 Windows 本机接口

```bash
wl http http://127.0.0.1:5000/health
wl http http://127.0.0.1:5000/api/items -X POST -d '{"name":"x"}' -H 'Content-Type: application/json'
```

这里 `127.0.0.1` 指的是 **Windows 的回环**，不是 Linux 的。

## 9. 硬性规矩

1. **不要**从 Linux 反向连 Windows 端口；只有隧道这一条路。
2. **不要**把大输出直接打印进上下文——用 `--output summary` 或游标读。
3. **不要**在 Windows 上开新的常驻监听端口。
4. **不要**把令牌、密码写进脚本、日志、共享目录。
5. **不要**用 `wl job cancel` 去"清理"你不确定的进程；它只杀你指定任务的进程树，
   但误取消会让别人正在跑的测试失败。
6. 遇到 `wl: cannot reach ... 127.0.0.1:18080`：
   - `wl status` 看隧道；
   - 等 30s（Windows 自愈）；
   - 仍失败就上报，不要盲目重跑任务。

## 10. 自检

```bash
~/WinLinuxBridge/tools/verify.sh          # 布局 + 链路
~/WinLinuxBridge/tools/verify_jobs.sh     # 超时语义/去重/游标/批处理
~/WinLinuxBridge/tools/verify_cancel.sh   # 取消隔离性
```

输出里 `[FAIL]` 为 0 才继续做正事。
