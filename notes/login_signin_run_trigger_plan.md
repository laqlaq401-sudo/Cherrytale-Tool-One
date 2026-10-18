# 登录签到「日常触发」修复方案 —— 方向 A：服务端运行侧补触发（2026-10-17）

> 关联：`notes/login_signin_trigger_gap.md`（根因排查：钩子只挂"登录"两处，而日常会话持久化后
> 从不经过登录页 ⇒ 钩子**从不执行**）。
> 状态：**已实施（2026-10-17）并完成验收（2026-10-17 ~ 10-18）**。实施原则延续 2026-10-06 用户决策：
> 无卡片、无开关、静默、每自然日一次、失败不影响主流程。

## 一、目标与不变量

- **目标**：每个角色**每自然日**至少有一次"真正开始执行游戏操作"的时刻，自动补发一次 33055
  （结算每日签到 + 活动登入奖励）——覆盖 PC 窗口版 / 浏览器 / 安卓 APK / 命令行四条使用路径。
- **保留不变量**：33055 幂等；成功才记账（`.login_signin_state.json`，键 `s={server}|p={player}`）；
  失败静默、下次自动重试；绝不向调用方抛异常；登录页的两条既有钩子（`tasks/login.py:474`、
  `webapi/app.py:640`）原样保留。
- **新增必须守住的不变量**：**dry-run = 零网络流量**（既有契约，
  `tests/test_main.py::test_run_dry_run_returns_0_and_sends_nothing` 在守）。

## 二、触发点设计（为什么是这里）

需求语义 = "当天第一次真正开始执行批次"。工具里这个时刻的唯一**公共必经点**是
`services/runner.py::run_tasks`：Web 排队批次、命令行 `run`、安卓（同 Web）全部经过它。

```
[开机 / 一键执行 / 单卡] → /api/run → RunManager.start()（排队）─┐
[CLI: main.py run …]  → cmd_run() ──────────────────────────────┼→ runner.run_tasks()
[内联运行 run_inline]：登录流程（登录侧已有钩子）/ 只读面板 ────┘   （内联不置位）
                                                                    │
      request.settle_login_signin=True 且 非演练 且 批内无 login 且 有会话
                                                                    ↓
                                                  补发 33055（幂等 · 每自然日一次）
```

- **置位（= 允许触发）只放两处**：
  1. `webapi/state.py::RunManager.start()` —— 所有**排队批次**（启动批 / 一键执行 / 单卡执行）；
  2. `main.py::cmd_run()` —— 命令行每次 `run`。
- **不置位（保持现状）**：`run_inline` 的内联运行——登录流程已有专用钩子；只读面板查询不算"开始执行"。
- **运行级字段**：`RunRequest.settle_login_signin: bool = False`。**默认关**是关键安全设计：
  测试 / 脚本直接构造 `RunRequest` 时零副作用（本工程测试惯用"真跑必 mock"，默认关让既有
  1800+ 用例行为逐字不变）。

## 三、触发条件与语义（最终口径）

位置：`run_tasks` 的 ① 任务名校验**通过之后**、第一个任务开始**之前**。条件：

1. `request.settle_login_signin` 为真；
2. **演练（`request.dry_run`）不触发** —— 守住"dry-run 零网络"契约（宁可少发一次也绝不破契约；
   关掉演练后的第一批即触发）；
3. **批内含 `login` 任务时不触发** —— 该批次的登录完成后由登录侧钩子发（避免拿旧令牌白发一次；
   仅影响 CLI 的 `run login`，Web 无法提交 login）；
4. `context.session_state` 存在（无会话直接跳过）；
5. 其余幂等性 / 记账全部沿用钩子自身（同日第二次运行只读文件即跳过，不发包）。

**结果可见性**：钩子成功 / 失败时，经新增的 `emit` 参数把 ✔/✘ 一行写进**本次运行的说明流**
（用户能在批次日志里直接看到签到发生了什么——这正是之前"看不到、无法确认"的痛点）；
"今天已发过"的静默跳过保持不刷屏。

## 四、变更清单（逐文件）

### 1) `services/runner.py`
- `RunRequest` 新增字段 `settle_login_signin: bool = False`，docstring 写明用途 +
  "为什么默认关"（测试与被直接构造的请求零副作用）+ 置位方（state.start / main.cmd_run）。
- `run_tasks()`：在 `resolved` 校验列表构造之后、`with _temporary_credentials` 之前，
  插入 `_settle_login_signin(context, request, emit)`。
- 新增模块级 `_settle_login_signin(context, request, emit)`：
  - 按 §三 条件短路（含 `getattr(context, "session_state", None)` 防御，兼容测试替身 context）；
  - 局部 import `tasks.login_signin`（与 `_recover_kicked_session` 同风格，避免循环导入）；
  - 调 `run_if_first_login_today(state, emit=…)`；emit 经 `_note(context, text, emit)` 进说明流；
  - **再包一层 try/except**：钩子契约已"绝不抛"，但本触发点跑在**每次批次启动**上、爆炸半径更大，
    双保险，异常只记 warning。

### 2) `tasks/login_signin.py`
- `run_if_first_login_today()` 增加可选参数 `emit: Callable[[str], None] | None = None`：
  ✔ 成功行、✘ 失败行各调一次 emit；"无 key / 今天已发过"的跳过**不调**（静默）。
- 模块 docstring：调用点一节由"登录两处"更新为"登录两处 + 运行侧触发（2026-10-17 方向 A）"，
  并注明触发缺口的根因一句话。

### 3) `webapi/state.py`
- `RunManager.start()` 首行 `request = dataclasses.replace(request, settle_login_signin=True)`
  （`RunRequest` 是 frozen dataclass；补 `from dataclasses import replace`），
  docstring 加一小节："排队批次 = 会尝试当日签到补发（幂等，每自然日一次）"。

### 4) `main.py`
- `cmd_run()` 构造 `RunRequest` 时加 `settle_login_signin=True` + 一行注释。

### 5) 文档
- 本文（新增）；
- `notes/login_signin_trigger_gap.md` 的"修复方向"节末加一行"→ 方案已定稿：见
  `login_signin_run_trigger_plan.md`"；
- 实现完成后：`notes/recon_findings.md` §23.5 补一行修正（"两个钩子点覆盖全部进游戏路径"的说法
  已被 2026-10-17 排查推翻——缺的正是"首页直进"这条日常主路径）。

### 明确**不改**的
前端任何文件（`web/`）、`webapi/app.py` 的内联运行构造、两条既有登录钩子、协议模型、
`.login_signin_state.json` 格式。

## 五、测试计划

**新增** `tests/test_runner_login_signin_settle.py`：
1. 默认（不置位）→ 钩子不被调用（spy）；
2. 置位 + 有会话 → 钩子**恰好调用一次**、收到 `context.session_state`、且**发生在第一个任务开始之前**
   （借 `on_start` 做顺序断言）；
3. 置位 + 无会话 → 不调用；
4. 置位 + `dry_run=True` → 不调用（零网络契约）；
5. 置位 + 批内含 `login` 任务 → 不调用；
6. 钩子抛异常 → 批次照常完成、记 warning（替身会抛）；
7. 钩子调 emit → 文本进入 `report.notes`（且经传入的 emit 传出）。

**接线测试**：
8. `tests/test_webapi_state_queue.py`：`manager.start(...)` 后经 `_execute_one` 到达的 request 带
   `settle_login_signin=True`（沿用现有 `_fake_run_tasks` 捕获手法）；
9. `tests/test_main.py`：`cmd_run` 构造的 request 带 `settle_login_signin=True`
   （monkeypatch `prepare`/`run_tasks` 捕获并返回假报告，零网络）。

**扩展** `tests/test_tasks_login_signin.py`：
10. emit 在成功路径被调用（含文本）；11. emit 在失败路径被调用；12. 跳过路径不调 emit。

**回归**：全量 pytest 期望 **1844 + 新增用例数 / 16 skipped、rc=0**；重点盯 `test_main.py`、
`test_webapi_state_queue.py`、`test_webapi_app.py`、`test_services_runner.py`。

## 六、验收步骤（实现后）

1. 定向测试（新文件 + 三处扩展）→ 全量回归；
2. **干跑守约**：`python main.py run handshake --dry-run` 仍须"未发送"（零网络）；
   真跑一次 → 说明流出现"登录签到（每日+活动） ✔ …"行（令牌失效则为 ✘ 一行，不影响运行结果）；
3. 同日第二次运行 → 不再出现该行（记账跳过）；
4. 真机会话验收：跑一批 → 批次日志出现签到行 → 开游戏确认奖励入账（用户确认）；
5. 安卓侧：`tools/sync_android_assets.py` **前台跑** + `sha256sum` 收尾对比 → 用户手动打 APK →
   安卓复测（**未打新 APK 前修复只在 PC 生效**，安卓仍是旧行为）。

## 七、风险与对策

| 风险 | 对策 |
| --- | --- |
| 令牌失效期：每个批次都会重试（失败不记账）→ 多条 warning + 一次快速失败请求 | 与登录路径同语义，可接受；重登后由登录钩子补上。**不引入失败节流**（避免"失败也不重试"的新坑） |
| 未来新增"走 manager.start + 真 run_tasks + 真会话"的测试会真联网 | 字段默认 False 已挡住直接构造；走 manager.start 的新测试按 §五-8 的捕获范式写 |
| 双进程（CLI + Web 同时开）同日并发 | 最坏多发一次**幂等**查询（现状相同）；记账锁为进程内 |
| 多运行目录（桌面拷贝 / 工程根）各自记账 | 每目录各自每日一次——与现状一致，属预期 |
| 用户长期开着"演练"模式 | 演练不触发（零网络契约优先）；关掉演练后的第一批即触发 |

## 八、回滚

四个文件的小改：删掉 `run_tasks` 里的调用行（或撤销两处置位）即回到"仅登录触发"的旧行为；
记账文件无需迁移。

## 九、明确不做

- 不做前端卡片 / 开关（延续 2026-10-06 用户决策）；
- 不改 33055 协议模型与记账格式；
- 不动 `run_inline` 内联运行（登录侧已有钩子；只读面板不应有副作用）;
- 不引入失败节流，不做"先查询再决定"（协议里没有可用的先行查询）。

---

## 十、实施与验收记录（2026-10-17 ~ 10-18）

- **实现（2026-10-17）**：§四 的四文件改动全部落地 + 新增 `tests/test_runner_login_signin_settle.py`（7 例）、
  `test_tasks_login_signin.py`（emit 4 例）、`test_main.py` / `test_webapi_state_queue.py`（接线各 1 例）。
  当时全量回归 **1857 passed / 16 skipped**（= 基线 1844 + 新增 13）。
- **干跑守约**（10-17 01:37）：`python main.py run handshake --dry-run` → 无签到行、"未发送任何请求" ✓。
- **真跑触发**（10-17 01:38）：签到行出现，钩子真的发出 33055；当日测试会话 token 已失效 →
  ✘ 失败**不记账**、批次照常完成 ✓（失败语义符合设计）。
- **真机入账证据**（10-17 21:13）：记账文件出现两条**成功**记录 —— `s=<server>|p=<player>`、
  `s=<server>|p=<player>`（真实账号，编号已脱敏；日常使用路径）⇒ 方向 A 在"首页直进、不经过登录页"的日常里生效。
- **测试卫生补丁（10-18，「同类漏洞」排查结论）**：`tests/test_login.py` 两处会跑完整登录链却**未**把
  `config.LOGIN_SIGNIN_STATE_FILE` 指到 tmp ⇒ 每跑一次全量就往工程根真实记账写一条假身份
  `s=167|p=10000001`。已实证复现（回拨记录 → 跑 test_login.py → 被改写为当天）并修复两处；
  修复后用"回拨 + 全量 1980 例"验证记录不再被改写。测试污染的假条目已从记账文件清除。
- **安卓**：`sync_android_assets.py` 已于 10-17 19:21 由打包流程前台跑过；四文件 sha256 与桌面端一致
  （APK 1.0.2 已含本改动，仍需用户手动重打/安装才落到手机）。
- 注：10-18 全量跑出 8 个 failed 全在 `tests/test_tasks_battle_arena.py`（另一会话的进行中任务），与本方案无关。
