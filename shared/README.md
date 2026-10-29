# shared/ — 三车道共享目录

这个目录在 Linux 与 Windows 两侧层级完全一致。

| 车道 | 谁写 | 谁读 | 同步 |
| --- | --- | --- | --- |
| `from_win/` | Windows | Linux | Windows 每 30 s 用 scp 推 |
| `to_win/` | Linux | Windows | Windows 每 30 s 用 scp 拉 |
| `shared/` | 两边 | 两边 | 拉回 → 按修改时间较新者胜 → 推回 |

规则：

- **不传播删除**：删掉一端，另一端仍保留。要真删请两端都删，
  或直接用 `wl rm <win 路径>` 删 Windows 那一份。
- `shared/` 冲突时**修改时间较新**的胜出。
- 想立刻生效（不等 30 s）用 `wl put` / `wl get`。
- 大文件不要放这里（每轮全量 scp）；用 `wl put` / `wl get` 直传。
