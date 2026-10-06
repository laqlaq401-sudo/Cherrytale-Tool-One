# 终端事故复盘：为什么会"命令超时 → 重开终端 → 越滚越卡"

> 2026-10-03 记录。触发场景：用户在安卓端反馈商店购买有问题，我为了强行复现，
> 在 PTY 里连做了几轮**长命令直连**（`python -c '…'`、`grep … ; …`），终端随即全面失真：
> 一半命令报 `Command completion could not be observed`，另一半"看不到回复"。
> 本档把**取证、因果、对策、验收**一次写清，避免下一个板块重复付费。

---

## 1. 症状（用户视角）

- Cline 反复报 `Command completion could not be observed; the command may still be running…`；
- 随后每条命令都像"没执行"（回显是**上一条**命令的输出，或干脆是空提示符）；
- Cline 不断新建终端（每个终端顶部都有一行 `source …/.venv/Scripts/activate`）；
- 用户判断："你是不是把终端超时设太短了？重开之后之前那个又没被清除，叠加就更卡。"

---

## 2. 取证（全部在**本机**拿到，可复现）

| # | 证据 | 位置 / 命令 |
|---|---|---|
| 1 | 默认超时 **4000ms** | `~/.vscode/extensions/saoudrizwan.claude-dev-4.1.22/dist/extension.js`：`setGlobalState("shellIntegrationTimeout", r||4e3)` |
| 2 | 这个超时**等的是什么** | 同一文件：`Waiting for shell integration for terminal … with timeout ${this.shellIntegrationTimeout}ms` + `pDe(()=>i.terminal.shellIntegration!==void 0,{timeout:…})` |
| 3 | 失败时**只抛错、不杀进程、留终端** | 同文件：`[Command completion could not be observed; the command may still be running and must not be assumed to have succeeded. …]` → `throw new _U(1,P)`；另一处字符串 `"has been left open and will not be closed automatically"` |
| 4 | 终端复用默认**开** | 同文件：`terminalReuseEnabled=!0` |
| 5 | 本机**没覆盖**这两个设置 | `~/.cline/data/settings/global-settings.json` 只有 `autoUpdateEnabled` / `telemetryOptOut`（两个键都在扩展 global-state 里，只能 UI 改） |
| 6 | Git Bash 启动偏慢 | `%APPDATA%\Code\User\settings.json`：`"Git Bash"` profile 的 `args = ["--login","-i"]`（`--login` 要跑 `/etc/profile`） |
| 7 | **输入被吞**的现场 | 终端回显：`$ d '<项目路径>' && ls -1 …` → `bash: d: command not found`（我发的是 `cd`，只进去 `d`） |
| 8 | 我们**没有**留下孤儿进程 | `python tools/safe_run.py --list` → 登记的两个 PID `market-shelf3` / `dossier` **都 dead** |

---

## 3. 因果链（一句话版）

> 4 秒等不到 shell integration ⇒ 读不到 OSC 633 的"命令完成" ⇒ Cline 报错并**故意留着**终端与可能仍在跑的命令 ⇒ 下一条命令落到这条忙 PTY 上，**输入字符被吞**（`cd`→`d`）⇒ 命令没执行、也看不到完成 ⇒ 再开新终端 ⇒ 雪球。

关键纠正两点：

1. **不是"我设的"**：4000ms 是 Cline 默认，工程里没有任何地方设置过它；
2. **不是"进程叠加"**：真正叠加的是**终端**（与忙 PTY 上的残留命令），我们的 `safe_run` 包装器没有留垃圾（证据 8）。我们自己的超时是 `DEFAULT_TIMEOUT = 300.0` 秒，且超时会**杀整棵进程树**，不会挂住终端。

---

## 4. 对策

### A. Cline 设置（只能人工点，值存在扩展 global-state）

| 设置（源码 key） | 默认 | 建议 | 为什么 |
|---|---|---|---|
| Shell integration timeout（`shellIntegrationTimeout`） | 4000ms | **15000ms** | 直接消除"等不到 shell integration"的根 |
| Terminal reuse（`terminalReuseEnabled`） | 开 | **关** | 每条命令用干净 PTY，杜绝"输入打进忙终端被吞" |

> 路径：Cline 面板 → ⚙️ Settings → 顶部搜索框输入 `shell` / `terminal`。
> 改完建议 `Developer: Reload Window` 再验。
> 验收：`python tools/safe_run.py --doctor` 不再报这两条建议。

### B. 编辑器设置（**已落地**）

- **工程** `.vscode/settings.json`（本次新增）
  ```json
  "terminal.integrated.env.windows": {
    "VIRTUAL_ENV": "${workspaceFolder}\\.venv",
    "PATH": "${workspaceFolder}\\.venv\\Scripts;${env:PATH}"
  }
  ```
  用**环境变量注入**替代"每个终端都跑一次 `source .venv/Scripts/activate`"：
  Python 依旧用 venv，但省掉脚本执行，终端就绪更快（原有 `enablePersistentSessions:false`、
  `confirmOnKill:"never"` 保留）。
- **用户级**（建议手工改）：`terminal.integrated.profiles.windows` 里 Git Bash 的
  `args` 由 `["--login","-i"]` 改成 `["-i"]`（`--login` 会跑 `/etc/profile`）。
  `--doctor` 会在检测到它时给出这条建议。

### C. 执行纪律（已写进 `.clinerules` 第 9、10 条）

- 长命令/带引号命令/可能 >2s 的命令**一律**走 `tools/safe_run.py`；
- 单条命令 ≤200 字符、首部不写 `cd '…'`（用绝对路径，`safe_run` 自带 `cwd=工程根`）；
- 不再用 `python -c` 现敲，需要临时脚本先落成 `tools/xxx.py`；
- 判"命令跑没跑"**只看** `tmp/runs/last.json` 的 `finished_at`，不看终端提示。

---

## 5. 体检工具：`python tools/safe_run.py --doctor`

一次收齐：子进程启动自查（自证"命令真的能跑"）、遗留常驻进程、Cline 两个设置、
VS Code 用户/工作区设置（`--login`、持久化会话、venv 注入）。
退出码：`0` 未命中已知坑 / `3` 有告警 / `1` 硬故障。
报告同时落盘 `tmp/runs/doctor.json`（终端看不见时读它）。
Cline 那两个设置会**两处都读**：扩展 global-state（`state.vscdb` 的 `ItemTable`）与
`~/.cline/data/settings/global-settings.json` —— 这样你在 UI 里改完，doctor 一定看得见
（读完会回写/显示实际值；`values` 里没有该键 = 生效的是默认值）。
另：`--clean` 只清登记表里**已死**的条目（活着的绝不动）。

---

## 6. 失真后的恢复三步

```bash
python tools/safe_run.py --kill-all
#  VS Code 命令面板：Terminal: Kill All Terminals（建议绑 Ctrl+Alt+K）
python tools/safe_run.py --label probe -- echo ok
#  然后读 tmp/runs/last.json：finished_at 刷新 = 链路恢复
```

> ⚠️ 卡死是**按终端**的：Cline 可能把下一条命令派到已失真的那条上 ⇒ 命令根本没执行。
> 所以"跑没跑"必须看 `last.json` 的 `finished_at`，不要相信终端里那行提示。
