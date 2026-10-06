# Web 可视化层（web/ + webapi/ + services/）

> 2026-09-21 新增。这一层**不改动** `client/`、`crypto/`、`models/`、`tasks/` 的任何
> import 路径，只在它们之上加"网页 + 桌面窗口"两个入口。
> 本文是这一层的**唯一总览**：怎么跑、怎么扩、验证过什么、踩过哪些坑。

---

## 一、三种跑法（都是同一份后端逻辑）

```bash
# ① 网页版：只服务本机（默认，最安全）
python web_server.py                     # http://127.0.0.1:8765/

# ② 网页版：让手机也能访问（必须给令牌，脚本会强制检查）
python web_server.py --host 0.0.0.0 --token 自定义口令
#   → 启动时会打印"手机（需同一 WiFi）：http://<本机局域网IP>:8765/?token=…"

# ③ PC 独立窗口（pywebview；未安装时自动回退 Edge/Chrome --app 模式）
python main_gui.py                        # 默认无边框 + 自绘最小化/关闭按钮
python main_gui.py --no-frameless         # 用系统标题栏的普通窗口（更稳）
python main_gui.py --no-window            # 只起服务、不开窗口
```

命令行入口 `python main.py ...` **完全不受影响**：两者的任务执行逻辑是同一份代码
（见下节）。

---

## 二、分层与依赖方向（单向，不会反向依赖）

```
web_server.py / main_gui.py     ← 进程入口：端口、窗口、打印访问地址
        ↓
web/（纯静态前端）   webapi/（HTTP + 状态 + 日志流）
        ↓                ↓
        └────→ services/（装配与执行，**唯一**的调度层）───→ tasks/ → client/ → （网络）
                              ↘ models/（报文）
```

| 目录 / 文件 | 职责 | 关键约定 |
|---|---|---|
| `services/task_spec.py` | **参数面唯一来源**（13 个任务 + 2 个占位项的参数、分组、中文说明） | 每个参数名必须与 `main.py` 的 argparse `dest` 同名，由测试双向校验 |
| `services/task_registry.py` | **内置任务导入清单** | 新增任务文件时**只改这里**（注册是导入时的副作用，忘记导入 = 网页上任务变灰） |
| `services/runner.py` | `RunRequest` → 会话 / 消费闸门 / 运行参数装配 → 串行执行 → `RunReport` | 与"会不会花钻石""battles=0 是否只读"有关的判断**只在这一处** |
| `webapi/logstream.py` | 日志流：`deque + seq` 环形缓冲、logging Handler、**线程感知 stdout 桥** | 只捕获 worker 线程的 `print`；白名单只收 `main/client/tasks/crypto/models/services/webapi` |
| `webapi/state.py` | 运行状态机 + **单次运行锁**（并发 → 409） | 校验放在起线程**之前**（名字写错立刻 400） |
| `webapi/app.py` | 路由、令牌鉴权、静态挂载 | 静态目录挂在 `/`，且必须在路由之后；`stream` 缺省时自动取 `bridge.stream` |
| `web/` | 纯静态前端（html/css/js + manifest） | **不放任何 `.py`**：静态挂载会把源码当文本暴露给局域网 |
| `main.py` | 唯一被改动的现有代码：`cmd_run()` 内部改为调用 `services/runner` | 打印文案与退出码（0/1/2）逐字保留，由 `tests/test_main.py` + 全套测试守护 |

---

## 三、API 契约

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 就绪探针 + 会话摘要（**已打码**）+ 是否运行中 + `auth_required` |
| GET | `/api/tasks` | 分组 + 任务清单 + 参数 schema + 真实约束（需登录/花钻石/接受运行参数）+ `suggested_dry_run` |
| GET | `/api/accounts` | 账号库已保存账号（`SavedAccount.describe()`，已打码） |
| POST | `/api/run` | 触发运行 → `202 {run_id}`；请求不可执行 → **400** `{detail:{problems:[…]}}`；已有运行 → **409** |
| GET | `/api/status` | 运行状态：`idle/running/done` + 每步 `pending/running/ok/fail` + 耗时 + `diamond_spent` |
| GET | `/api/logs?since=<seq>&limit=<n>` | 增量日志；`next_seq` = **已发出的最后一行**（不会跳行），`stream_seq` = 缓冲总进度 |
| POST | `/api/stop` | **501**：不提供强制中断（领取类请求不可撤销） |
| GET | `/` | `web/index.html`（页面本身不校验令牌，否则用户没地方输入令牌） |

鉴权：`X-Auth-Token` 请求头（推荐）或 `?token=`（手机点链接最方便）。
比较用 `secrets.compare_digest`，且**两边先编码成 UTF-8 bytes**
（直接传中文会 `TypeError: comparing strings with non-ASCII characters is not supported`）。

---

## 四、两条日志通道（缺一条网页就少一半内容）

| 通道 | 来源 | 数量 | 采集方式 |
|---|---|---|---|
| logging | `tasks/`、`client/` 等模块的 `logger.info(...)` | 数百处 | `LogStreamHandler` 挂到根日志器 |
| **print** | 演练/只读模式的说明，如"—— 以上为演练输出，未发送任何请求 ——" | `tasks/` 下 11 个文件、100+ 处 | **线程感知 stdout 代理**（只捕获 worker 线程） |

不用 `contextlib.redirect_stdout`：它是**进程级**的，长驻服务里会把 uvicorn 的输出
一起吞掉/串台。不改 `tasks/` 里的 `print`：那会打穿 `tests/test_main.py` 对 stdout 的断言。

前端渲染 `USER` 级别的行时不显示级别标签（它们本来就是"给人看的话"）。

---

## 五、安全约定（每条都有对应实现与测试）

1. **默认只绑 `127.0.0.1`**；`--host 0.0.0.0` 时**强制要求** `--token`（否则拒绝启动，退出码 2）。
2. **密码只在内存**：`RunRequest.credentials` → 运行期间注入环境变量
   `CHERRYTALE_LOGIN_ACCOUNT/PASSWORD` → `finally` 里恢复原值；不落盘、不回显、不进日志。
   （`tests/test_services_runner.py::test_credentials_never_leak` 守着它。）
3. **前端不存秘密**：令牌只进 `sessionStorage`（关标签页即失效），密码提交后立刻清空输入框。
   勾选状态与参数存 `localStorage`（那只是界面偏好）。
4. **单次运行锁**：并发提交返回 409。理由：凭据是进程级环境变量 + 消费额度需要跨任务累加。
5. **默认只读**：`times=0` / `battles=0` 不消耗任何资源；花钻石需要**两道门**
   （总闸 `allow_diamond` + 用途 `invite_friends`）同时打开。
6. **危险动作二次确认**：非演练模式下，勾了"有消耗/花钻石"的参数会先弹确认框。

---

## 六、验证记录（含本轮发现并修正的真实缺陷）

自动化：

```bash
python -m pytest -q                 # 1126 passed, 10 skipped（原 1017 + 新增 109）
python main.py selftest             # 环境就绪
node --check web/assets/app.js      # 前端语法检查
```

真机渲染（无头 Edge 会真的执行 JS 并把渲染后的 DOM 抓出来）。
★ 2026-10-02 起，**起服务与起浏览器一律走 `tools/safe_run.py`**（原因见 §“终端被永久标记 busy”那一节）：

```bash
# ① 服务（常驻）：立刻返回 PID，进程完全脱离终端
python tools/safe_run.py --bg --label web -- python web_server.py --port 8765
python tools/safe_run.py --timeout 60 --label dom -- \
  "/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe" \
  --headless=new --no-sandbox --disable-gpu \
  --user-data-dir="<绝对路径>/tmp/edge" --virtual-time-budget=9000 \
  --dump-dom http://127.0.0.1:8765/
# 期望：6 个任务被勾选、2 个占位项灰显、日志窗 20+ 行、进度卡出现 step--ok
# （DOM 在 tmp/runs/*.log 里；收尾：python tools/safe_run.py --kill-all）
```
```

本轮真实踩到并修正的缺陷（都留了回归测试）：

| 缺陷 | 现象 | 根因 | 修正 |
|---|---|---|---|
| `/api/tasks` 把 `login` 标成"未实现"、复选框全灰 | 网页无法运行任何任务 | 规格层读注册表时**没加载任务模块**；pytest 进程恰好被别的测试模块填满，所以单测全绿 | 新增 `services/task_registry.py`，`describe_tasks()` 显式加载；加**子进程**守卫测试 |
| 网页日志少一半 | 只看到"运行结束"，看不到任务日志与演练说明 | `create_app` 只传了 `bridge` 没传 `stream` → 两个缓冲各写各的 | `create_app` 缺省时取 `bridge.stream`；加"两个缓冲必须是同一个"的测试 |
| 中文令牌完全不可用 | 401 / 500 | `secrets.compare_digest` 不接受非 ASCII `str` | 两边 `encode("utf-8")` 后再比 |
| 启动提示在被强杀时丢失 | 日志文件里没有"已启动" | stdout 重定向到文件时是**块缓冲** | 打印后 `sys.stdout.flush()` |
| 「网关握手」被摆成一张可勾选的任务卡 | 用户在"自动任务"里以为"不勾就登不上"，还要维护一张与业务无关的卡片 | 规格表里 `handshake` 没设 `hidden`（当时只有 `login` 设了） | `services/task_spec.py` 给 `handshake` 加 `hidden=True`（复用前端既有的 `!task.hidden` 过滤，**前端零改动**；CLI 照旧 `python main.py run handshake`）。2026-09-22 |
| 邮件一键领取在网页上"领不到"（`业务错误 code=-2`） | 网页日志：`邮件共 5 封，解析出 5 个 mailID… ✘ 业务错误 code=-2` | **不是 Web 层的锅**：`tasks/mail.py` 把**已读（= 已领过）**的邮件也塞进批量 13003，服务端**整包原子拒绝**、一封都不发。定性见 `notes/recon_findings.md` §十六（Q-031） | `models/mail.py` 增 `mail_is_read_of()` / `unread_mail_ids()`；`tasks/mail.py` 改为**只领未读 + 批量被拒则逐封降级**。2026-09-22 |
| 「演练模式」开关每次进主屏都被重置回勾选 | 用户关掉演练、点运行，再回主屏（或重新登录）后开关又自己打开：任务显示绿色 ✅ 却**什么都没发包**（最容易被误读成"领不到"） | `loadTasks()` **无条件**套用 `suggested_dry_run`，而它在 `boot()` 与登录成功后各调一次 | `web/assets/app.js` 增 `readStoredDryRun()` / `saveStoredDryRun()`：**只在本地没有任何记录时**才套用服务端建议值；运行按钮文案与进度卡徽标分别显示"界面意图"与**服务端回报的** `dry_run`（`progress-mode`，由 `renderProgressMode()` 按 `/api/status` 填）。2026-09-22 |
| 桌面窗口启动时终端刷出几百行 `Error while processing window.native.AccessibilityObject…Alert.Alert…` + 窗口刚出现时卡数秒 | `python main_gui.py`（无边框）启动后打印 `RecursionError: maximum recursion depth exceeded`；窗口随后自己恢复、功能正常 | pywebview 注入 `window.pywebview` 前会**递归遍历 JS 桥对象的公开属性**（`webview/util.py::inject_pywebview()`）；`DesktopChrome.window` 是公开属性 ⇒ 它爬进 `Window` → `native`（WinForms 的 .NET 窗体，`window.py:182` / `winforms.py:195`）→ `AccessibilityObject` → `Owner` → `AccessibleRole`（枚举）→ 枚举成员 `Alert` **自我延伸**（pythonnet 取 .NET 枚举同名成员返回同一个值）⇒ 永不穷尽；`get_functions()` 的 `id()` 去重对"每次新建包装"的 .NET 对象失效（`util.py:181-209`）。**pywebview 6.2.1（当前最新）仍未修** | `main_gui.py`：窗口引用改名 `self._window`（`util.py:193` 会跳过 `_` 开头的名字，扫描第一层就不再深入），绑定改走私有 `_attach_window()`；`minimize`/`close` 是方法、不参与递归，照旧被收集。回归测试：`tests/test_main_gui.py::TestWindowControls::test_chrome_keeps_window_reference_private`。真机验证：`Error while processing` 0 行。规避（若不修）：`python main_gui.py --no-frameless`（`js_api=None` ⇒ 不扫）。2026-09-22 |

---

## 七、怎么扩展（新增一个任务 / 新参数）

1. 在 `tasks/` 里写任务并 `@register_task`（照旧，与命令行时代一致）；
2. `services/task_registry.py` 加一行 import；
3. `services/task_spec.py` 登记 UI 规格（分组、中文标题、参数）；
4. 若任务有专属参数：在 `main.py` 的 `run` 子命令加旗标（`dest` 与规格里的参数同名），
   并在 `services/runner.py` 的 `task_kwargs_for` 里点名传给任务；
5. 跑 `python -m pytest tests/test_services_task_spec.py tests/test_services_runner.py -q`
   —— 覆盖、参数面一致性、kwargs 名字三件事都会被自动校验。

前端**不需要改**：任务清单、参数表单、危险二次确认都由 `/api/tasks` 的规格驱动。
若要加"未来才做的功能"，先在 `task_spec.py` 里加 `enabled=False` 的占位项即可
（界面会灰显并显示 `note` 里的说明）。

---

## 八、2026-09-22 更新：三屏流程（登录 → 选区 → 主界面）与信息展示

### 8.1 为什么改流程
原来是一屏堆卡片：没登录也能点「一键运行」，然后收到一串 401 —— 那是设计问题。
现在按真实顺序分三屏，用户不可能跳过前置步骤：

```
login（用哪个账号）──登录成功──> server（进哪个区）──选好区──> main（干什么）
```

### 8.2 后端：**同一份**登录逻辑支撑两段式

| 阶段 | 接口 | 内部做的事 | 关键性质 |
|---|---|---|---|
| ① 拉区服列表 | `POST /api/login/servers` | 跑 `login` 任务，参数 `plan_only=True` | 只发 1001（+握手），**不写会话、不写账号库、不发 1003** |
| ② 进区 | `POST /api/login/enter` | 跑 `login` 任务，参数 `server_id=N` | 正常 1001→1003→1004，写会话文件与账号库 |
| ③ 信息面板 | `GET /api/player` | 跑 `wallet` + `arena_status`（两个只读任务） | 一次请求一份一致快照；单项失败进 `errors` 不整体失败 |

为支持上述两段，`tasks/login.py` 只**增**了两处（默认行为不变，由既有 663 行测试守门）：

- 构造参数 `server_id: int | None`（强制进区）与 `plan_only: bool`（只拉列表）；
- `_select_server()` / `_plan_only_result()` 两个方法；
- 成功时 `TaskResult.data` 增补 `player_name` / `fighting_force` / `login_days`（来自 1004 的 `PlayerClass`）。

**为什么第二段要重新发 1001**（而不是把第一段的连接留到第二段用）：两次 HTTP 之间要保持
一个 `GameClient`（连接 + Cookie + 信封状态）活着，就得引入"待选区会话 + 超时清理"，
而手机断网时那些状态只会变成垃圾。重发 1001 的代价是 330 字节，换来的是**无状态**：
任何一步失败都能单独重试。

### 8.3 新只读任务 `wallet`（金币 / 钻石 / 体力）

| 项 | 值 |
|---|---|
| 协议 | `GetAllItemPacket(2007, 空报文) → GetAllItemRes(2008)` |
| 字段 | 只有一个 `itemList`，元素**复用** `models.daily.ItemClass{itemSid,itemID,itemAmount}` |
| 取值 | 金币 `COINS_ID=200000050`、钻石 `DIAMOND_ID=200000001`、体力 `ENERGY_ID=200000002` |
| 禁发清单 | `2001 UseItemPacket` / `2005 MixItemsPacket` / `2011 HarvestDreamStonePacket` |
| 真机实测 | 金币 37,193,710 / 钻石 3,426 / 体力 220（背包 823 条） |

> `models/proto_fields.py` 是生成文件：新协议类要**先加进 `tools/extract_proto_fields.py`
> 的 `FOCUS_CLASSES`，再重新生成**（`python tools/extract_proto_fields.py`）。
> 本轮顺带发现该表**早已落后于 dump.cs**（TopPvp / PVPHome / WuDou 等一批类的字段编号
> 从未生成过），已一并补齐（+170 行，`--check` 通过）。

### 8.4 信息展示的数据来源（都是只读）

| 显示项 | 来源 |
|---|---|
| 角色名 | 1004 的 `PlayerClass.name`（**不入会话文件** → 前端记在 sessionStorage，标注"来自最近一次进区"） |
| 区服 / 平台账号 | 会话状态（`describe_session()` 的结构化字段；平台 userId **在 API 层打码**） |
| 游戏金币 / 钻石 / 体力 / 背包条目 | `wallet` 任务（2007 空包） |
| 竞技次数 / 已购 / 战力 / 公会 / 对手数 | `arena_status` 任务（26001）。实测战力与 1004 的值一致（2,518,906），互为交叉验证 |

### 8.5 本轮修正的缺陷（都留了回归测试）

| 缺陷 | 现象 | 根因 | 修正 |
|---|---|---|---|
| `models/proto_fields.py` 过期 | `wallet` 一跑就 `KeyError: 未知的协议类 'GetAllItemPacket'` | 生成表只收录 `FOCUS_CLASSES` 里的类，且早已与 dump.cs 脱节 | 加名单 + 重新生成 + `--check` 守卫 |
| **`call()` 漂移出 IIFE** | 页面白屏级别的问题：所有接口调用抛 `state is not defined` | 编辑过程中函数被留在 IIFE 之外，成了全局函数（`node --check` 查不出这种结构性错误） | 移回 IIFE 内；靠**无头 Edge 渲染**复验（这正是"只看语法检查不够"的实证） |
| 平台 userId 未打码 | 信息面板显示完整 38 字符 ID | API 直接透传了 `describe_session()` 的原值 | API 层 `_masked_session()` 打码（命令行仍看完整值） |

### 8.6 前端结构（`web/`）

```
index.html   三屏骨架 + 常驻日志 + 底部操作条
app.js       setScreen() 状态机；登录/选区/主界面各自的渲染与请求；
             任务清单由 /api/tasks 的 group 字段分流（auto / manual / planned）；
             钻石页对"涉及钻石的任务"只做**只读展示**（避免同一任务两套控件互相不同步）
styles.css   暗黑 + 移动优先：.tabs / .stat-grid / .server-card / .sticky-actions
```

**回退开关**：`?flat=1` 直接进主界面（跳过登录/选区，只显示任务与日志）——
三屏万一出问题，仍有一条能用的老路径。

---

## 九、活动关卡「预览」链路（2026-09-22）

### 9.1 问题：网页上只能手输活动区域 / 关卡号

用户反馈：「降临 / 活动关卡扫荡」的活动区域、指定关卡、扫荡次数**都没有信息**，
必须自己手输。查明结论：**不是前端写错，也不是后端算错，而是一条链路从未打通** ——

| 环节 | 事实 | 证据 |
|---|---|---|
| 后端**早就**在拉数据 | `times=0` 时任务会发 11025（区域表）与 11003（关卡表） | `tasks/sweep.py::_fetch_areas` / `_fetch_sections` |
| 但数据被丢掉 | 只读分支的 `TaskResult.data` 里只有**数量**（`open_areas` / `sweepable` / `blocked`），完整清单只进日志 | 改动前的 `tasks/sweep.py` |
| 没有预览接口 | `webapi/app.py` 只有 health / tasks / accounts / login* / player / run / status / logs / stop | 同上 |
| 规格面无法表达"候选" | `ParamKind` 只有 int / float / str / bool，`param_view()` 也不带候选信息 | `services/task_spec.py` |
| 前端只会画输入框 | `renderParam()` 只有 checkbox / number / text 三支 | `web/assets/app.js` |

因此改动必须**同时**落在四处，只修一边都不会有变化。

### 9.2 现在的链路

```
web/assets/app.js  「拉取候选」按钮
   └─ GET /api/activity/preview?area=<可选>
        └─ RunManager.run_inline(RunRequest(tasks=[sweep_activity{area, times=0, section=None}]))
             └─ tasks/sweep.py：11025 → 11026 / 11003 → 11004（**只读**）
                  └─ TaskResult.data：areas[] / area / sections{sweepable[], blocked[]}
```

| 位置 | 新增内容 |
|---|---|
| `tasks/sweep.py` | `SweepCandidate.as_view()`、`BlockedSection.as_view()`、模块级 `area_view()`；只读分支追加 `data["areas"]` / `data["area"]` / `data["sections"]`（**计数键保留**，旧调用方不受影响） |
| `webapi/app.py` | `GET /api/activity/preview`（`scope` 仅实现 `current`，其它值明确 400） |
| `services/task_spec.py` | `ParamSpec.choices`（固定档位）与 `ParamSpec.options_source`（动态候选来源）；`param_view()` 透传；`times.choices=(0,1,5,10)`、`area.options_source="activity_areas"`、`section.options_source="activity_sections"` |
| `web/assets/app.js` | `renderChoiceParam()` / `renderCandidateParam()`（下拉 + 手输 + 「拉取候选」+ 状态行）；候选缓存 `state.activityOptions`；换区域时清空关卡候选 |
| `web/assets/styles.css` | `.param-tools`（按钮与状态行的排版） |

### 9.3 ★ 两条实现陷阱（改这块之前必读）

1. **预览接口绝不能带 `dry_run=True`。**
   `tasks/sweep.py::_dry_run()` 的语义是"**不联网、不发包**"：带它调用只会拿到一段
   说明文字，一个真实区域都没有 —— 表现是"接口通了但永远是空的"。
   只读的真正保证来自 **`times=0`**（`execute()` 在 `times<=0` 时直接返回，
   `11009` 连组装都不会发生）。回归断言：
   `tests/test_webapi_app.py::TestActivityPreview::test_preview_is_read_only_and_returns_areas`
   （同时断言 `dry_run is False`）。
2. **界面不手输，但 API / CLI 仍接受手输。**
   网页上的活动区域与指定关卡是**纯下拉**（`ParamSpec.allow_manual=False`）：
   这两项是"每期都换的随机 ID"，让用户去别处抄数字，抄错的症状
   （服务端静默拒绝）极难排查。但**能力本身没有取消**：
   `--area` 仍接受 areaID / 清单编号 / 名称关键字，`--section` 仍接受
   sectionID / 候选编号（`main.py` 是手写 argparse，不读规格表）。
   档位同理：`choices` 只约束界面，`coerce(3)` 依旧返回 `3`，
   由 `tests/test_services_task_spec.py::TestParamViewHints` 守着。

### 9.4 只读 ≠ 零请求

预览会真的发两个**只读**包（11025 / 11003）。因此：

- 入口做成**按钮触发**，不做自动轮询（避免每开一次页面就多两次风控暴露）；
- 接口把 `sent_packets` 回给前端，界面直接显示"只发了 11025 / 11003"——
  用可核对的事实代替口头承诺；
- 与"一键运行"共用同一把闸门：运行中预览会拿到 **409**（而不是并发发包）。

### 9.5 未做（有意留口）

`11026` 还返回 `memoryAreaStateList`（回忆区，28 条）与 `permanentAreaStateList`
（常驻区，37 条）。当前只暴露 `areaStateList`（当期，16 条），接口预留 `scope` 参数；
要扩展时同时改：`webapi/app.py` 的 scope 分支 + `tasks/sweep.py` 的清单来源
（`ActivityAreaStateClass` 的类文档已写明三个列表的区别）。

### 9.6 验证命令（改完照抄）

```bash
python -m pytest tests/test_sweep_task.py tests/test_webapi_app.py tests/test_services_task_spec.py -q
node --check web/assets/app.js
(timeout 90 python web_server.py --port 8765 > tmp/web.log 2>&1 &) ; sleep 7 ; tail -3 tmp/web.log
# 前端是否真的把它们渲染出来（跑在无头 Edge 里抓渲染后的 DOM）：
#   --virtual-time-budget=9000 --dump-dom http://127.0.0.1:8765/
#   判据：DOM 里出现「更新区域」按钮与 .task-status 状态行（**不需要勾选任务**），
#         以及 3 个 <select>（times / area / section）
```

### 9.7 缓存与刷新策略（2026-09-22 第二轮：不再每次拉取）

用户要求：不提供手输、由脚本自动拉取、**不要每次都拉**（一周才变一次）、
周三 20:00 之后自动更新、并留一个手动按钮。

**落点分工**

| 层 | 职责 | 关键文件 |
|---|---|---|
| 缓存 | 存快照 + 判定过期（周三 20:00 / 换号 / 兜底天数） | `models/activity_cache.py`、`config.ACTIVITY_CACHE_FILE` 等 4 个常量 |
| 服务 | "新鲜就用、过期才联网"，并把结果落盘 | `services/activity_cache_service.py` |
| 接口 | 缓存优先；`refresh=1` 强制重拉 | `webapi/app.py::/api/activity/preview` |
| 界面 | 纯下拉 + 卡片标题行的「更新区域」+ 状态行（不勾选也可见） | `web/assets/app.js` |

**什么时候会自动刷新**（`ActivityCache.needs_refresh`，任一成立）
1. 从没拉过（缓存为空）；
2. 换了账号 / 区服（`player_id` / `server_id` 不符）——
   进度 `5/5`、星数是**账号数据**，不重拉就会显示上一个号的数据；
3. 已过 `next_refresh_at`（= **下一个周三 20:00**，按 `DISPLAY_TZ` UTC+8 算）；
4. 超过兜底 `config.ACTIVITY_CACHE_MAX_AGE_DAYS` 天 ——
   万一"周三"这条规则与实际不符，也不至于让数据无限旧下去。

**触发时机**
- **进入主界面时**（前端 `loadTasks()` → `ensureActivityCache()`）：过期才联网；
  命中缓存时**零请求**。这是"启动时自动更新"的实际落点 ——
  拉区域表必须带会话令牌，所以进程刚启动（还没登录）时做不到，见下。
- **手动**：卡片标题行的「更新区域」→ `?refresh=1`，强制重拉区域 + 当前区关卡。

**★ 三条容易搞错的地方**
1. **进程启动那一刻刷不了**：`11025` 需要登录态（`requires_auth`）。
   所以"启动时自动更新"落在"已登录进主界面时"，而不是 `web_server.py` 起来时。
2. **缓存只服务界面**：真正扫荡时 `tasks/sweep.py` 仍现场发 `11025` + `11003`
   （用最新 `sectionState` 判断"是否已通关"）。缓存里的状态会过时 ——
   拿它做判断会**误挡**刚通关的关卡。这条是用户明确选定的取舍。
3. **测试必须隔离缓存文件**：服务层默认读写工程根的 `.activity_cache.json`，
   跑测试时若不 `monkeypatch config.ACTIVITY_CACHE_FILE`，
   结果会取决于"你这台机器上有没有缓存、它新不新鲜"（实测踩到一次假故障：
   本机验证过一次 → 单元测试变红）。见 `tests/test_webapi_app.py::TestActivityPreview`
   的 autouse fixture 与 `tests/test_services_activity_cache_service.py`。

**回归测试**
- `tests/test_activity_cache.py`：周三 19:59 / 20:00 / 20:01、跨周、换号、兜底天数、
  坏 JSON、脏字段、原子写。
- `tests/test_services_activity_cache_service.py`：**缓存新鲜 → 假执行器调用次数必须是 0**。
- `tests/test_webapi_app.py::TestActivityPreview`：缓存命中 `sent_packets == []`；
  `refresh=1` → `source == "network"`；`area` 非数字 → 400；运行中 → 409。

---

## 十、每日任务自动执行（2026-09-24：删掉「一键运行」）

> 这一节是这次改动的**唯一总览**：交互怎么变、为什么变、去重口径在哪、怎么验证。

### 10.1 交互变化（用户需求）

| 原行为 | 现行为 |
|---|---|
| 底部常驻 `#actionbar`（`#selection-hint` + `#btn-run`「一键运行」）：勾选若干 → 点按钮 → 跑 | **整条操作条删除**，界面上不再有任何"运行"按钮 |
| 自动任务：勾选只是"待跑的清单" | 自动任务：**勾选即生效** —— 进入主界面 / 登录成功 / 切换区服 / 勾选变动时自动执行（`app.js::runAutomation`） |
| 主动任务：与自动任务共用同一个复选框 | 主动任务：左侧换成 **▸ 展开箭头**（`manualToggle`），展开后显示参数与**「执行这个任务」**；执行前仍做危险参数二次确认 |

两个分组的语义因此彻底分开（`services/task_spec.py` 的 `GROUPS` 早就这样定义）：
`auto` = 可以无脑跑的，交给脚本在启动/状态变动时做；`manual` = 会花资源的，必须当场确认。

### 10.2 提交与顺序（关键约定）

1. **自动执行**：提交 `collectTasks({group:"auto"})` 里"今天还没跑过"的任务。
2. **主动任务**：提交 `[该任务, daily_box]` —— **一次运行两步**，由 `runner.run_tasks`
   串行执行。不发第二次 `/api/run`：运行闸门同一时刻只允许一次运行（第二次 409），
   而且分两次提交会让进度条 / 日志 / 消费额度断成两段、难以对应。
3. **活跃宝箱永远排在最后**：它按活跃点解锁，活跃点靠前面的任务涨。规格表里
   `daily_box` 已挪到 `TASK_SPECS` 的 auto 组末尾（`describe_tasks` 只按分组排序，
   组内保持插入顺序），`runAutomation` 里再按 `REPEATABLE_AUTO` 显式排一次。

### 10.3 宝箱单一归属，随后 daily 整体删除（2026-09-24，两轮）

**第一轮**：`tasks/daily.py` 的 `steps` 从 `(daily_box, login_pass, free_gift)`
改为 `(login_pass, free_gift)` —— 宝箱只由 `tasks/daily_box.py` 负责。

- **为什么**：宝箱是一天里唯一要跑多次的日常动作（每做完一个主动任务，活跃点可能
  涨到下一档）。放在 `daily` 里之后，"自动跑一遍 daily"与"主动任务后补跑 daily_box"
  变成两个入口指向同一件事 —— 同时勾选两者时同一批宝箱请求会发两遍。
- 宝箱任务本身幂等：无新达标宝箱时输出"没有可领的宝箱"，且 `ok=True`
  （不会在日志里刷一串 ✗）；它在 `app.js` 里是 `REPEATABLE_AUTO` 的唯一成员。

**第二轮（用户要求）**：`daily` 这层"流程壳"**整体删除** —— 它只是把子任务串起来，
而在新交互下每个子任务本来就是独立的自动任务。删掉之后"每日自动执行"的实质内容 =
`mail` + `login_pass` + `free_gift` + `daily_box`（这四个现在**全部默认勾选**）。

删除时容易漏的地方（都已处理）：

| 位置 | 处理 |
|---|---|
| `tasks/daily.py` | 删除文件 |
| `services/task_registry.py` | 去掉 `import tasks.daily`（漏了会触发"注册表 ↔ 规格表"集合比对失败） |
| `services/task_spec.py` | 去掉 `"daily"` 规格条目 |
| `tasks/__init__.py` | 去掉清单行 |
| `main.py` | 帮助举例改为 `handshake / login / mail` |
| `tests/test_tasks.py` | 删 2 个 daily 用例；副作用 import 改为 `tasks.daily_box`（否则注册表里没有它） |
| `tests/test_main.py` / `test_services_task_spec.py` / `test_services_runner.py` | 断言与示例对象换成 `mail` / `daily_box` / `wallet` |

命令行不再有 `python main.py run daily`；要一起做就写
`python main.py run mail login_pass free_gift daily_box`（顺序即执行顺序，宝箱最后）。

### 10.4 游戏日 = 每天 05:00（UTC+8）

| 层 | 职责 |
|---|---|
| `config.DAILY_RESET_HOUR`（默认 5，env `CHERRYTALE_DAILY_RESET_HOUR`） | 换日时刻的**唯一**配置 |
| `models/daily_reset.py` | `game_day()` / `next_reset_after()` / `format_reset()`，纯函数可直测 |
| `webapi/app.py::/api/tasks` | 把 `game_day` / `next_daily_reset` **算好**再给前端 |
| `web/assets/app.js` | 只做字符串比较（`ranToday`），**不自己算日期** |

前端判重记录：`localStorage["cherrytale.autoRun"] = {game_day, server_id, player_id, tasks, at}`。

- 三个条件（游戏日 + 区服 + 玩家）都要对得上才算"跑过"：换号 / 换区视为新对象，必须重跑。
- 记录损坏 / 隐私模式下读不到 → 当作"没跑过"（宁可多跑一次，绝不永远不跑）。
- 与区域表的时间同一个口径：时间文本一律后端算（见 9.7 与 `activityStatusText` 的注释）。

### 10.5 演练开关仍然管着"会不会真发包"

自动执行**沿用**运行选项里的「演练模式」开关（用户选定）：开关开着 → 自动执行只组装、
不发包；关掉 → 自动执行真实发包。所以第一次验证时先看这个开关的状态。

其他安全机制不变：钻石总闸、危险参数二次确认、单一运行闸门（并发 → 409）。

### 10.6 运行中触发的自动执行不排队

`state.pendingAuto`：运行中触发（例如"刚登录就勾了一个任务"）不硬提交（会 409），
只打一个标记，等 `pollOnce` 发现"刚跑完"时补一次（`runAutomation("补跑")`）。

### 10.7 验证（改完照抄）

```bash
node --check web/assets/app.js
python -m pytest -q
(timeout 90 python web_server.py --port 8766 > tmp/web.log 2>&1 &) ; sleep 7 ; tail -3 tmp/web.log
# DOM 判据（无头 Edge；--user-data-dir 必须给绝对路径）：
#   id="btn-run" 计数 == 0            （一键运行已删除）
#   class="task-toggle" 计数 == 主动任务个数（每个 manual 任务一个 ▸）
#   <p id="auto-status"> 形如「自动任务：本游戏日（2026-09-24）已完成 6 个 · 下次重置 09-25 05:00 · 模式 演练（不发包）」
```

实测（2026-09-24，`--dump-dom` 抓渲染后的 DOM）：`btn-run` 0 个、`task-toggle` 3 个
（扫荡 / 荣耀之巅 / 宴席）、状态行显示"已完成 6 个 · 下次重置 09-25 05:00 · 模式 演练"；
`tmp/web866.log` 里自动任务按序完成、**`daily_box` 排在最后**。
（上面的日志摘录来自"daily 尚未删除"的那一版；daily 删除后这条结论同样成立 ——
`daily` 与 `daily_box` 现在都是独立任务，顺序仍由规格表保证。）
手动双步提交（演练）返回
`steps: [(top_pvp_battle, ok), (daily_box, ok)]`、`summary: 2/2 步成功`。

**交互级验证（无头 Edge + CDP）**：语法检查与 `--dump-dom` 只能证明"元素渲染出来了"，
证明不了"点下去会发生什么"。所以配了一个**长期保留**的脚本
`tools/verify_web_ui.py`（标准库手写 WebSocket 客户端，不引入任何新依赖）。
它会**先强制勾上「演练模式」**，再真实点击，所以不会碰真实账号：

```bash
# ★ 2026-10-02：这些命令**必须**经 tools/safe_run.py 转一手（否则会把终端卡死，
#   详见本文件末尾「终端被永久标记 busy」一节）
python tools/safe_run.py --bg --label web867 -- python web_server.py --port 8767
python tools/safe_run.py --bg --label edge9223 -- \
    "/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe" \
    --headless=new --disable-gpu --remote-debugging-port=9223 \
    --user-data-dir='<工程绝对路径>/tmp/edgeV2' 'http://127.0.0.1:8767/?flat=1'
python tools/safe_run.py --timeout 240 --label verify-ui -- python tools/verify_web_ui.py 9223 8767
python tools/safe_run.py --kill-all          # 收尾：把 web_server 与 Edge 一起关掉
```

> ``--bg`` 是"立刻返回"（这正是它不卡终端的原因），所以要先确认两个进程活着再跑验证：
> ``python tools/safe_run.py --list`` 里两条记录都是 ``alive``；服务可用
> ``python tools/safe_run.py --timeout 10 --label health -- curl -s -m 5 -o /dev/null -w "%{http_code}" http://127.0.0.1:8767/api/health``
> 探一下（预期 ``200``）。

它做的事：先**强制勾上「演练模式」**（保证这次点击不发包）→ 真实点击 `.task-toggle`
→ 断言出现 `.task-exec` 与 `.task--expanded` → 再点 `.task-exec`
→ 读 `/api/status` 核对步骤顺序，最后用 CDP 的 `Browser.close` 只关自己启动的那个 Edge。

实测 **8/8 通过**：3 个展开箭头、0 个 `#btn-run`、展开前 0 个执行按钮、
按钮文案 `执行这个任务（演练（不发包））`、提交步骤 `sweep_activity,daily_box`、
`dry_run=true`（服务端确认未发包）。

**★ 脚本本身也踩过一次坑（值得记住）**：进入主界面现在会**自动执行**每日任务，
而运行结束后 `pollOnce` 又会调 `/api/player` 刷新信息面板（5 个只读任务串行，
占着**同一把运行闸门**）。最初的脚本用固定 `sleep` 读 `/api/status`，读到的是
"上一次自动执行"的步骤，点击也被 409 静默丢掉 —— 于是出现假失败。
现在脚本改成三步：① 轮询后端空闲 → ② 再等一拍让前端解禁按钮（空闲时前端
5 秒才轮询一次）→ ③ 点击后**轮询到新运行的步骤出现为止**（最多重试 3 次，
并把页面里的 `alert` 文本打出来）。

### 10.8 只读状态类任务只在「信息展示」里出现（2026-09-24）

用户要求：**金币钻石查询 / 竞技场状态 / 天命对决状态 / 失控炼成阵状态**
不要在「自动任务」里显示，全部集中到「信息展示」面板；荣耀之巅（主动任务）的
**名次与积分**也一并显示。

| 层 | 落点 |
|---|---|
| 规格 | `services/task_spec.py`：这四个任务加 `hidden=True`（与 login / handshake 同一机制）+ note 写明"数据在「信息展示」里看" |
| 接口 | `webapi/app.py::/api/player`：任务列表从 2 个扩到 **5 个**（`wallet` / `arena_status` / `wudou_status` / `yimo_status` / `top_pvp_battle`），返回新增 `wudou` / `yimo` / `top_pvp` 三段 |
| 数据 | `tasks/top_pvp.py` 只读分支补 `my_points`（积分只在 `my_rank()` 返回的榜单项里，字段名 `rankPoing`） |
| 界面 | `web/assets/app.js::renderInfo`：新增"天命对决次数 / 失控炼成阵次数 / 荣耀之巅名次 / 荣耀之巅积分"四张卡 |

**为什么 `hidden=True` 就够了（前端零改动）**：`renderTaskGroup()` 过滤
`!task.hidden`（清单里不出现）、`collectTasks()` 跳过 hidden（**不参与自动执行**）；
CLI 不看 hidden，所以 `python main.py run arena_status` 照旧可用。

**刷新时机只有两个**（不新增后台轮询）：

1. 手点「角色与资源」的**刷新** → `loadPlayer()`；
2. **每次运行结束**（含执行主动任务、自动任务）→ `pollOnce` 的"刚跑完"分支会 `loadPlayer()`。

**★ 只读保证**：`/api/player` **自己构造** `RunRequest`（不沿用界面上的任何参数），
所以 `top_pvp_battle` 必然只读（不消耗次数、也**不写**"跳过战斗演出"开关）。
界面上的「进阶：打几场」永远不会被它沿用。回归断言见
`tests/test_webapi_app.py` 的 `test_player_endpoint_merges_and_reports_partial_failure`
——它同时守住三件事：请求的任务清单与顺序、场数为 0、逐项容错。

> **2026-09-25 更新**：这里的"只读"改由**显式** `params={"battles": 0, "interval": 0}`
> 保证（原来靠"不传 → 退回 `config.TOP_PVP_BATTLES`"，而那个默认值可以被环境变量
> 改成 5 —— 那时"刷新一下信息面板"就变成真开打）。同时「进阶：打几场」这个位置
> 已经不存在了：场数 / 间隔已归位到荣耀之巅卡片，见 §10.10。

**代价**：每次刷新最多 6 个只读包（2007 / 26001 / 26031 / 26033 / 22101 / 39001）
且串行，所以比原来慢一点（用户已知情）。

**荣耀之巅为什么没有"次数"**：39002 里只有 `buyChallengeCount`（**已购**次数）与
语义待确认的 `rewardCount`（实测值 2026），**没有"剩余挑战次数"**（见
`models/top_pvp.py` 的字段注释）。所以信息面板按用户选定的最精简口径，只显示名次与积分。

### 10.9 「执行这个任务」遇忙必须说清楚（2026-09-24，实测发现）

§10.7 那次脚本假失败，暴露的是一个**真实的产品缺口**：

`/api/run` 与 `/api/player`（信息面板刷新）**共用同一把运行闸门**
（`webapi/state.py::RunManager._gate`），而后者在"每次运行结束后"都会被 `pollOnce` 调用。
用户在那一瞬间点「执行这个任务」会拿到 **409** —— 修复前前端只把 `pendingAuto` 记下来
（那是给**自动执行**用的补跑标记），**手动点击就这样彻底丢了**，现象是"点了没反应"。

修复：`submitRun()` 的返回值从布尔改成**状态字串**
（`"ok"` / `"busy"` / `"auth"` / `"error"`），由调用方决定怎么处理：

| 调用方 | 遇到 `"busy"` 时 |
|---|---|
| `runAutomation()`（自动执行） | 正常 —— 记 `pendingAuto`，等这次跑完自动补一次 |
| `runManualTask()`（手动点执行） | **弹提示**："刚才这一步没有提交：现在有别的请求正在跑（例如刷新信息面板）…请等一两秒再点一次" |

另外 `runManualTask()` 开头补了"上一次运行还没结束，请等它跑完再点"的提示
（修复前是直接 `return`，同样表现为点了没反应）。

回归：`tools/verify_web_ui.py` 会打印页面收集到的 `alert` 文本，并在未提交成功时重试点击。
实测输出：`第 1 次点击未提交成功（页面提示：[…请等一两秒再点一次…]），重试…`
→ 第 2 次提交成功 `sweep_activity,daily_box`。

### 10.10 荣耀之巅：参数归位 + 间隔 15 秒 + 删掉「保留演出」开关（2026-09-25，两批）

**用户反馈**：主动任务里"荣耀之巅的战斗次数之类的不知道为什么划到了进阶"。

**根因**：不是分组错，是**参数归属**错。`services/task_spec.py` 把
`battles` / `interval` 登记在 `RUN_LEVEL_PARAMS`（运行级参数）里，而
`web/index.html` 那个 `<details><summary>进阶：场数与间隔（只对「荣耀之巅」等任务生效）</summary>`
就是"运行级参数"在界面上的位置（它属于最上面的「运行选项」卡片）。
可这两个值**全项目只有荣耀之巅一个消费者**（`tasks/base.py::RunOptions`），
登记成运行级本就名不副实 —— 判据应该是"是不是每个任务都认得它"。

| 层 | 落点 |
|---|---|
| 规格 | `services/task_spec.py`：`BATTLES_PARAM` / `INTERVAL_PARAM` 移进任务级区；`top_pvp_battle.params = (battles, interval)`；`RUN_LEVEL_PARAMS` 缩为 `(dry_run, allow_diamond)` |
| 调度 | `services/runner.py`：`RunRequest` **删掉** `battles` / `interval` 字段，改成派生属性 `requested_battles` / `requested_interval`（照 `wants_invite_friends` 的先例："只有某个任务认识的参数，就只从那个任务的参数里读"）；`prepare()` 改读它们 |
| CLI | `main.py`：`--battles` / `--interval` 旗标**原位保留**（用法一字未变），只是不再进 `run_level_from_cli`，而由 `task_params_from_cli(args, "top_pvp_battle")` 取出 |
| 接口 | `webapi/app.py`：`RunPayload` 删两个字段；`/api/player` 把只读从"隐式"改成**显式** `params={"battles": 0, "interval": 0}` |
| 界面 | `web/index.html` 删掉那个折叠块；`web/assets/app.js` 的 `runOptions()` 删两行（连带删掉只服务它的 `numberOrNull()`） |

**为什么前端一行渲染代码都不用改**：卡片参数完全由 `/api/tasks` 的规格驱动
（`renderTask` → `renderParam`，`kind="int"/"float"` 本来就支持），
所以"参数归位"在后端改完就自动生效 —— 这正是把参数面抽成规格表的回报。
另一个自动生效的收益：`battles.danger=True` → `TaskSpec.consumes=True`，
于是卡片出现「有消耗」徽标，填了场数点执行会**先弹二次确认**
（以前它在运行级，而 `dangerReasons()` 遍历的是 `task.params`，根本看不见它）。

**必须走 `ParamSpec.coerce`**：网页交上来的是**字符串**（`<input type="number">` 的
value），所以 `RunRequest.requested_battles` 不能直接 `int()` —— 空串要等于"没填"、
`"3"` → `3`、`"0.25"` → `0.25`，这些都是规格表已经定义好的语义。

**回归断言**：`tests/test_services_task_spec.py::TestTopPvpParamPlacement`
（`RUN_LEVEL_PARAMS` 不含这两个名字且恰好是 `{dry_run, allow_diamond}`；
`top_pvp_battle.param_names == ("battles","interval")`；
`consumes is True`）；`tests/test_services_runner.py` 另加一条
"别的任务带上 `battles` 不影响运行参数"。

**实测验证（2026-09-25）**：

```bash
python -m pytest tests -q     # 退出码 0（全部通过）
python main.py selftest       # 结论：环境就绪
python main.py run top_pvp_battle --dry-run --battles 2
# → [✔] top_pvp_battle: 演练完成：计划发出 5 个包（其中挑战包 2 个，消耗次数 2 次），未发送
curl -s http://127.0.0.1:8765/api/tasks | grep -o '"name":"top_pvp_battle".\{0,1200\}'
# → params 含 battles(danger=true) / interval / keep_battle_animation；consumes=true
```

无头 Edge 抓渲染后的 DOM（`--headless=new --dump-dom`）实测（**第一批当时的状态**）：
`>打几场<`、`>每场间隔（秒）<`、`>保留战斗演出<` 各出现 **1 次**（都在荣耀之巅卡片里），
`opt-battles` / `opt-interval` 出现 **0 次**（旧的运行选项输入框已彻底消失）。
第二批删掉了演出开关，所以现在 `保留战斗演出` 是 **0 次** —— 见下。

#### 10.10.1 第二批：间隔默认 15 秒 + 删掉「保留战斗演出」（同日）

| 项 | 变动 |
|---|---|
| 间隔默认值 | `config.TOP_PVP_INTERVAL_SECONDS` 默认 `2` → **`15`**；hint（"默认 15 秒"）、`--interval` 的 help、config 注释三处同步 |
| 「保留战斗演出」 | **三层全删**：规格参数（原 `KEEP_ANIMATION_PARAM`）、命令行旗标（`--keep-battle-animation`）、任务形参（`keep_battle_animation`）。行为固定为"真打前把服务端 39019 置为开启" |
| 逃生舱 | 保留 `CHERRYTALE_TOP_PVP_SKIP_BATTLE=0`：此时工具**不去改**服务端设置（`_ensure_skip_battle_enabled` 与 dry-run 的 `skip_policy` 都按它分流）。界面与命令行上都没有这个开关 |

**删开关时唯一不能碰的东西**：只读模式（`battles=0`）依然绝不写 `39019` ——
那是"只读承诺什么都不改"的一部分，与有没有界面开关无关。

**为什么旗标也要删（而不是留着当隐藏选项）**：界面上已经没有这个开关，
留着 CLI 旗标就变成"看不见的开关还能被命令行打开"，比原来更难理解。
删除是**破坏性**的：旧脚本用它会被 argparse 拒绝（退出码 2），这是需求本身的一部分。

**回归断言**：
`test_keep_animation_flag_is_gone`（规格参数与 CLI 旗标都不存在）、
`test_interval_default_is_15_seconds`（默认 15 秒且 hint 写明）、
`tests/test_tasks_top_pvp.py::test_escape_hatch_env_skips_toggle`
（只有逃生舱关闭时才不写 39019 —— 它替换了被删的"保留演出"那条测试，覆盖位置不丢）、
`tests/test_services_runner.py` 的 kwargs 名字校验顺带守住任务形参。

**实测（2026-09-25）**：

```bash
python -m pytest tests -q      # 退出码 0
python main.py run top_pvp_battle --dry-run --battles 2
#  → 运行参数：计划打 2 场（间隔 15s）
#  → 战斗演出策略：默认跳过：若发现服务端开关是关闭的，会先写 39019 置为开启
python main.py run top_pvp_battle --dry-run --battles 1 --interval 0.5
#  → 运行参数：计划打 1 场（间隔 0.5s）        ← 覆盖仍然生效
CHERRYTALE_TOP_PVP_SKIP_BATTLE=0 python main.py run top_pvp_battle --dry-run --battles 1
#  → 战斗演出策略：按配置不动服务端开关（CHERRYTALE_TOP_PVP_SKIP_BATTLE=0）：不会去写 39019
#  → 报文共 3 个（只有 39001 / 39011 / 39007，没有 39019）
python main.py run top_pvp_battle --keep-battle-animation
#  → error: unrecognized arguments（旗标已删，退出码 2，属预期）
```

无头 Edge `--dump-dom`（主界面渲染后）：`>打几场<`（带 `param--danger` 红框）、
`>每场间隔（秒）<` 都在荣耀之巅卡片里；**`保留战斗演出` 出现 0 次**。

**命令行用法一字未变**（本次改动的硬约束）：
`python main.py run top_pvp_battle --battles 2 --interval 0.5` 照旧可用；
唯一区别是"别的任务误加 `--battles`"现在会被 `normalize_params` 丢弃并打 WARNING
（过去是静默忽略）。




---

## 十一、前端逻辑修复 + 界面深度打磨（2026-09-30，只动 web/）

**后端零改动**。视觉方向：深度打磨现有暗黑风（蓝色 accent 保留，用户选定）。

| 逻辑修复 | 根因 / 效果 |
|---|---|
| 轮询并发竞态守卫 | `submitRun` 起跑后立刻调的 `pollOnce` 与定时轮询并发时，两次 `fetchLogs` 用同一 cursor → **同一批日志行追加两遍**。加 `state.polling` in-flight 标志，`finally` 释放 |
| 日志窗 DOM 上限 | 过去无限累积（长会话上万节点、手机掉帧）。`LOG_DOM_LIMIT=800`，超出从最旧一端裁 |
| 手动/自动失败解耦 | `submitRun` 过去无条件写 `state.autoError` → 手动执行失败会错位显示"自动任务：上次提交失败"。改 `origin` 参数，只有 auto 路径写入 |
| 选区搜索防抖 120ms | 167 张服务器卡不再跟着每次键入全量重建 |
| a11y | tabs 加 `role=tablist/tab + aria-selected`；🔑/⏻ 补 `aria-label` |

**界面**：:root 令牌升级（`--line-strong/--accent-soft/--trans/--shadow-1/2`，表面三层明度级差拉开 → **底色 0f1116→0d0f14，meta/manifest 已同步**）；顶栏渐变字标 + 连接胶囊状态点（wait 态呼吸）；卡片标题渐变强调条；统计卡 14 张加图标与按资源着色（`statCard` 增 icon/tone 参数）；任务卡勾选微光/展开 accent 态/badge--danger ⚠ 前缀/执行按钮三态；`auto-status` 状态圆点；日志窗细滚动条 + 行 hover + USER 行左缘竖条；server-card 选中 ✓；`.empty` 空状态；`prefers-reduced-motion` 扩展（`--trans:0s`）。有意**不做**：进度步骤淡入 —— `renderProgress` 每次轮询整体重建，动画会每 1.2s 重放闪烁。

**回归契约未破坏**：`alert/confirm`、`.task-toggle/.task-exec/.task--expanded`、步骤顺序全部原样 —— `tools/verify_web_ui.py` 实测 **8/8**；无头 Edge `--dump-dom`：`task-toggle` 3、`btn-run` 0、`stat-icon` 14、`auto-status--ok` 正常、`aria-selected="true"` 恰 1 个；`node --check` 过；`pytest -q` exit 0。

---

## 十二、两页拆分 + 侧边栏（2026-09-30 深夜，只动 web/ + verify 脚本一处）

**用户需求**：菜单改侧边栏；登录界面独立出去；登录并选区后再进任务界面。
**后端零改动**（`html=True` 静态挂载天然服务 `login.html`，app.py:777-781）。

### 新结构

```
login.html（门）                            index.html（屋子）
  令牌卡（--token 启动时按需出现）             aside.sidebar：品牌/连接/退出 + 5 个导航 + 会话摘要
  ① 账号库免密 / 账号密码登录                  main.content：运行选项 + 5 个 panel + 进度卡 + 日志卡
  ② 筛选区服 → 进入游戏 ── location.href='/'   （≥1200px：面板左、进度+日志右列 areas；
                                                <900px：侧栏重排成顶部两行，side-foot 隐藏）
```

| 文件 | 内容 |
|---|---|
| `web/assets/common.js`（新） | 两页共享工具挂 `window.Ct`：`$ / el / call / describeError / formatNumber / debounce / STORAGE_KEYS` + 令牌单例（`initToken/getToken/saveToken/clearToken`，URL token 照旧读后即抹） |
| `web/login.html` + `login.js`（新） | 门的两步 + `doEnterServer` 成功后写 `playerName` → 跳 `/`；元素 id 与旧版一致（login-accounts / server-list / btn-enter-server…） |
| `web/index.html` | 删 topbar 与登录/选区两屏；`.shell = aside.sidebar + main.content`；`#tabs` 保留（data-tab/aria-selected 不变，加 emoji 图标）；`#side-session` 顶替 `#topbar-session` |
| `web/assets/app.js` | 删登录/选区/令牌卡代码（-230 行）；`boot()`：非 flat 且 `next_screen!=="main"` → `location.href="/login.html"`；轮询/提交 401 → `Ct.clearToken()` + 跳登录页；`renderTopbar`→`renderIdentity` |
| `web/assets/styles.css` | 删 `.topbar` 系；加 `.shell/.sidebar/.side-brand/.side-foot`（sticky 100dvh、毛玻璃）、`.sidebar .tabs` 纵向、<900px 顶部两行重排、≥1200px 右列 areas（`.panel` 同区安全：同时只一个可见）、`.gate` 门面 |
| `tools/verify_web_ui.py` | 判据①期望值改**动态**（= /api/tasks 的 manual 组数量）：今晚新增 `buy_energy` 任务把写死的 3 变过期值 —— 写死数字会在"任务正常加卡"时报假失败 |

### 跳转与状态交接（无额外同步逻辑）

- 会话在**服务端文件**：登录/选区成功 = `/api/login/enter` 写会话 → 跳转 `/` 后
  `health.next_screen === "main"` 自然放行；未登录访问 `/` → 回门。
- 浏览器侧交接单：`sessionStorage` 的 token / playerName（键名未变）；
  `localStorage` 的 autoRun 判重键不变。
- `?flat=1` 逃生舱保留在 index（跳过登录检查直进任务界面）。
- 退出 ⏻ = 清 playerName + 跳 login.html（服务端会话照旧不清，见 §"退出/换区"注释）。

### 验证（2026-09-30 深夜实测）

- `node --check` ×3 全过；无残留引用（grep `state.token|setScreen|showTokenCard|...` 仅命中注释）。
- 无头 Edge dump-dom：`login.html` 渲染门面（gate-login/gate-server/login-accounts/btn-enter-server 全在）；
  `/?flat=1` 出侧边栏 + 4 个 `.task-toggle`（= manual 组数）+ `btn-run` 0 + 13 张统计卡。
- **重定向**：`--token` 启动 8768 端口、无令牌访问 `/` → 最终 DOM 是 login 门面且令牌卡可见（401→clearToken→跳转，实测通过）。
- `tools/verify_web_ui.py` **8/8**（步骤顺序 `sweep_activity,daily_box`、dry_run=true）。
- `pytest -q` 全套 exit 0（后端未动，确认无隐藏耦合）。

### §十二补：桌面窗口三项修复（2026-10-01 凌晨，main_gui.py + 测试）

用户报告"最小化/关闭按钮和任务栏重合"，顺带要求最大化 + 边缘拖拽缩放：

| 项 | 落点 |
|---|---|
| 按钮压内容 | 旧注入 `fixed top:8 right:10` 在顶栏删除后正好盖住右上角 sticky 的日志卡标题。现改为**停靠进 `.side-brand` 顶部**（独立右对齐行，—/□/✕ 三钮，`.iconbtn` 皮肤 34px）；无侧边栏的页面（login.html）兜底右上角悬浮 |
| 窗口压任务栏 | pywebview 把窗口居中于**屏幕**而非工作区：125% 缩放屏（有效高 ~864）上 860 高的窗口底边压进任务栏。新增 `_query_screen_workarea()`（ctypes SPI_GETWORKAREA）+ `_fit_window_size()` 纯函数：居中不压任务栏 ⇔ `h ≤ 2×work.bottom − 屏高` 且 `h ≤ 屏高 − 2×work.top`（顶/侧任务栏同理），再保底 min_size |
| 最大化 | **不能**用 `window.maximize()`——WinForms 对无边框窗体的系统最大化铺满整屏（盖任务栏，正是要修的问题）。`toggle_maximize()` 手动 move+resize 到工作区矩形，再点还原保存的 bounds；状态记在私有 `_maximized` |
| 边缘拖拽缩放 | 无边框窗体没有系统缩放边框。注入 8 个透明把手（n/s/e/w + 四角，fixed 3px/角 14px，对应 cursor）；mousedown `stopPropagation`（拦在 target 阶段，pywebview 的 easy_drag 挂在 window 冒泡上就收不到，不会"边缩放边拖动"）→ `begin_edge_resize(edge)` 起 daemon 线程：Win32 `GetCursorPos` 轮询（~60fps）直到 `GetAsyncKeyState(VK_LBUTTON)` 松开，几何经纯函数 `_edge_geometry` 后走 `window.move` + `window.resize(fix_point)`（pywebview 内部转 UI 线程，跨线程安全；直接改 WinForms 控件会崩） |

**桥对象公开面变化**：`DesktopChrome` 公开方法从 `close/minimize` 扩为 `begin_edge_resize/close/minimize/toggle_maximize`（方法不参与 pywebview 递归扫描，安全；私有字段相应加 `_maximized/_saved_bounds`，`tests/test_main_gui.py::test_chrome_keeps_window_reference_private` 已同步）。

**验证**：`pytest tests/test_main_gui.py` 23 通过（含 `_fit_window_size` 4 例、`_edge_geometry` 4 例——修掉两处真 bug：钳制公式最初被 `max()` 守卫吃掉；测试元组把 left/right 写反）；CDP 真页面注入实测 **12/12**（停靠/把手/去重/登录页兜底）；`tools/verify_web_ui.py` 需在系统代理开着时直连 CDP——临时脚本用 `ProxyHandler({})` 绕过（`getproxies_registry` 会劫持 127.0.0.1）。拖拽与最大化属真实 GUI 交互，自动化只覆盖"注入成功 + 几何正确"，请使用时手测一遍。

### §十二补 2：顶栏回归 + 最大化铺满（2026-10-01，用户实测反馈两项）

| 反馈 | 根因（已核实） | 修复 |
|---|---|---|
| "窗口控制放侧边栏不合操作惯例" | 上一轮把 —/□/✕ 停靠进侧边栏顶部 | 恢复**顶栏** `.topbar`（sticky + 毛玻璃，`--topbar-h:46px`）：左品牌、右侧 conn + ⏻ + **`#win-dock` 停靠位**（桌面壳注入 —/□/✕；网页/手机上为空不显示）。侧边栏只留导航 + 底部会话摘要；`renderIdentity`/app.js 零改动（id 全保留） |
| "最大化后内部界面没跟着放大" | **不是** pywebview 的锅：WebView2 控件 `DockStyle.Fill`（窗体变视口必跟随，edgechromium.py:99），进程非 DPI 感知（`_scale=1.0` 坐标不二次缩放）。真凶是 CSS：`.content` 的 `max-width:1400px` 封顶 + 宽屏右列固定 420px → 1920 宽下右侧空 ~290px 死白 | `.content` 去 max-width；宽屏右列 `minmax(0,420px)` → `minmax(320px,32%)`（进度/日志列随窗口变宽）；统计卡 auto-fill 自动多排 |

注入停靠目标 `.side-brand` → `#win-dock`；`tests/test_main_gui.py` 断言同步。验证：`pytest tests/test_main_gui.py` 23 过；CDP 真页面注入 12/12（含顶栏顺序 `conn,btn-logout,win-dock`、登录页兜底 fixed）；dump-dom：topbar/win-dock/side-session 各 1、side-brand 0、task-toggle 4；`verify_web_ui.py` 8/8；全套 pytest exit 0。

### §十二补 3：「日志记录」分区 + 底部日志坞（2026-10-01 第二轮，参考 lucima-tools）

**需求**：侧边栏新增日志记录；开发者日志移进去并与运行日志并列；底部加可开关的日志坞（样式/交互参考 StardustChocolate/lucima-tools 的 bottom-log-dock）。

**视图分流**（`state.logLines` 环形缓冲 ≤800 行 = 唯一事实源，`appendLogLine` 同时投喂三个视图）：

| 视图 | 位置 | 口径 |
|---|---|---|
| `#run-log` 运行日志 | 「📜 日志记录」第一张卡 | 仅 USER + WARNING + ERROR（`RUN_LEVELS`），无 logger 前缀，行距更疏朗 |
| `#log` 开发者日志 | 同分区第二张卡（原实时日志整体迁入，级别筛选/暂停/清空/复制 id 全保留） | 全量流 |
| `#dock-log` 底部坞 | 固定底部面板 | 与运行日志同口径，自动跟随滚动 |

**底部坞（lucima 式交互）**：收起 = 右下角悬浮 peek 按钮（62×40、accent 发光描边、左上角红色未读徽标，>99 显示 99+）；展开 = `fixed bottom` 面板（桌面 `left:232px`、移动 0），高度 `--bottom-log-h`，入场 `bottom-log-rise` 0.22s 上浮淡入；顶缘 resizer 用 Pointer Events 拖拽调高（上拖加高、钳 120px~60vh、松手持久化）。开关在日志记录分区的 `.log-dock-setting` 自绘开关行；开关状态与高度都进 localStorage（`cherrytale.bottomLog` / `cherrytale.bottomLogH`）。body 类联动布局预留：`bottom-log-enabled:not(expanded)` → `.content` 预留 84px；展开 → 预留坞高。

**落点**：`index.html`（tabs 加 logs 项 + panel-logs 三卡 + 坞骨架；旧 `#log-card` 删除，≥1200px 右列 grid-areas 收敛为只放运行进度 `"runopt side"/"panel side"`）、`app.js`（RUN_LEVELS/buildLogRow/appendLogView 三视图分流 + 坞逻辑 `setBottomLogEnabled/setBottomLogExpanded/updateDockBadge/applyDockHeight/initBottomLogDock`；级别筛选/暂停只作用于开发者视图）、`common.js`（STORAGE_KEYS 加两键）、`styles.css`（坞全套样式）。

**验证**：`node --check` 过；dump-dom 结构判据全过（panel-logs/run-log/dock-log/peek/徽标/开关各 1、旧 log-card 0）；CDP 交互 **16/16**（主动触发一次演练运行产生运行口径行 → 三视图有内容、收起未读徽标出现、peek 展开收起、徽标清零、开关关/开、resizer 光标）；`verify_web_ui.py` 8/8；全套 pytest exit 0。坑：CDP 会话恢复的 login.html 标签会干扰页面匹配 —— 选择器必须带 `flat=1`；ranToday 去重让安静服务器缓冲里没有 USER 行，交互验证要自己造一次演练运行。

### §十二补 4：开发者日志收起化 + 坞视觉打磨（2026-10-01 第三轮，用户反馈三项）

| 反馈 | 落点 |
|---|---|
| 「详细日志」移到日志记录 | 运行选项卡只剩演练模式；`opt-verbose`（id 不变，app.js 零改动）迁入开发者日志卡标题行 |
| 开发者日志不展开显示，只留保存按钮 | 删除 `#log` 展开视图与级别筛选/暂停/清空/复制（那些是"看流"的工具）；`state.logLines` 缓冲成为唯一事实源，`saveDevLog()` 导出 `cherrytale-dev-log-<时间戳>.txt`（带 `[级别] logger:` 完整签名，页面上不展示的机器信息正好进文件）。app.js 相应删除 LEVEL_ORDER/MIN_LEVEL/levelVisible/applyLevelFilter/state.paused/state.level |
| 底部坞展开后有点丑 | 从"贴边平板"改成"控制台抽屉"：四边留缝（左 248 / 右 16 / 下 12）+ 14px 圆角 + `--shadow-2` + `overflow:hidden`；头部 accent 竖条标题 + 清屏/收起升级为小胶囊按钮；resizer 加淡 accent 渐变底、hover 时横杠变宽变亮；日志区更深底色（#0a0c10）+ 内边距；移动端左右各留 12px |

验证：`node --check` 过；dump-dom（`btn-devlog-save` 1、旧 `#log`/`#log-level` 0、verbose 在日志分区、task-toggle 5=manual 组数）；CDP 交互 **19/19**（新增：开发者日志无展开视图、verbose 迁入、点保存不抛错）；`verify_web_ui.py` 8/8；全套 pytest exit 0。

### §十二补 5：运行视图改为「结果流水」（2026-10-01 第四轮，用户要求精简日志）

**需求**：让使用者简单看见运行结果；重复行与脚本运行逻辑描述全部去掉。

**方案（纯前端，后端 tasks 的 print 与 CLI stdout 断言零影响）**：运行结果视图与底部坞**不再搬运任务 print 流**，改两个来源：
1. `/api/status` 的结构化步骤结果（`renderRunResults`）：每次运行先画一条模式分隔线 `── 演练（不发包） · HH:MM:SS ──`，然后**每个任务一行** `✔/✘ 标题 · 耗时 · 后端摘要`——`state.lastRunId/seenSteps` 去重（同一步骤同一结局只记一次），`status.error` 只取首行；
2. WARNING/ERROR 日志行：只取首行（完整多行说明走「保存」导出）。

任务 print 细节仍全部进 `state.logLines` 缓冲（`saveDevLog` 导出全文）。首次历史回填（`since=0`）传 `countUnread=false`：历史不是"未读"。视图加 `.log-placeholder` 占位（首行到来即移除），卡片/坞标题改「运行结果」。

**验证**：`node --check` 过；CDP **22/22**（含：✔ 结果行、演练分隔线、"—— 以上为演练输出"等脚本逻辑行**不再出现**于两个视图）；`verify_web_ui.py` 8/8。

### §十二补 6：全站拨动开关（2026-10-01 第五轮，用户要求）

所有 type=checkbox 的勾选 UI 统一改为 **pill 轨道 + 圆形滑块**的拨动开关（用户给的参考图：关=灰轨左块 / 开=accent 轨右块）：

- **实现**：真实 `<input type=checkbox>` 保留（键盘/读屏/表单语义不变），`appearance:none` + `::after` 自绘滑块（46×26，滑块 20px，checked 位移 20px）。样式集中在 `input.toggle, .switch > input[type=checkbox]` 一条规则；危险开关（钻石总闸）checked 用 `--danger` 红轨。
- **位置**：自动任务的开关移到**卡片最右**（`.task-head--flip`：信息 `.task-info` 靠左吃满、开关右贴边，勾选动作不再打断读标题的视线）；主动任务的 ▸ 展开箭头不是勾选件，保持左侧；`.switch` 行（演练模式/详细日志/钻石总闸/底部坞/登录页"只看有角色的区"）统一**文字左、开关右**（flex order 重排）；任务 bool 参数（如宴席邀请好友）`.param--bool` 网格：标签左、开关右、说明换行铺满。
- **验证**：`node --check` 过；dump-dom：`input.toggle` 7 个、`task-head--flip` 4（=auto 组数）、`task-info` 8（=全部任务卡）；CDP **25/25**（新增：自动任务开关在卡片最右、点击关闭/打开均生效）；`verify_web_ui.py` 8/8（演练开关照常工作）；全套 pytest（除并行开发的 main_stage）exit 0。

### §十二补 7：两页跳转后的窗口控制丢失 + 退出/关窗混淆（2026-10-01 第六轮，用户实测两个 bug）

| Bug | 根因（已核实） | 修复 |
|---|---|---|
| 退出重登后顶部 —/□/✕ 消失 | 注入是一次性的（`webview.start(_inject_window_controls, window)` 只在首屏跑一次）；两页拆分后登录/退出**真实跳转**，每次导航 DOM 重建，注入物（含 8 个缩放把手）随之丢失。pywebview 6.2.1 已核实：`on_navigation_completed → inject_pywebview → events.loaded.set()` 每次导航都触发，`Event` 支持 `+=` 多处理器 | 新增 `_wire_window_events(window)`：在 `webview.start()` **之前**订阅 `window.events.loaded += lambda: _inject_window_controls(window)`——每次页面加载后重新注入（脚本自带 `#win-controls` 去重，跨导航必然重建）；登录页无 `#win-dock` 时以右上角悬浮兜底，登录界面同样可关窗/缩放 |
| 点"关闭"却回到登录层 | 顶栏右侧原顺序 `conn → ⏻退出 → 窗口按钮`；重进后因上 bug 窗口按钮不在，右上角唯一像"关闭"的 ⏻ 电源图标实际是**退出登录** | ⏻ 改为**文字胶囊「退出」**（`btn-logout` id 不变，app.js 零改动；幽灵描边样式 `.topbar-logout`）——右上角只有 —/□/✕ 三件套长得像窗口控制 |

**隐性 bug 加固**：① 桥未就绪（跳转完成后 `window.pywebview` 异步注入的窗口期）点击窗口按钮会静默失效 → 注入脚本改为：桥不可用时禁用按钮 + `setInterval` 150ms 重试 ≤20 次，就绪后调用一次并恢复——同时防止"最大化连点两次变还原"；② `destroy()` 存在且线程安全（venv 源码核实）；③ `_maximized/_saved_bounds` 跨导航保留，重进后 □ 仍能还原；④ 服务器停止在 finally，与所在页面无关。

**测试**：抽 `_wire_window_events` 便于单测（FakeEvents `__iadd__` 记录订阅；处理器在无 evaluate_js 的 Fake 上调用不崩）；脚本断言补 `setInterval` 标记。`pytest tests/test_main_gui.py` 24 过；CDP **16/16**（含退出文字按钮、模拟"导航→重注入"往返：登录页兜底 fixed、回任务界面重新停靠 + 把手重建）。**需手测**：真实 pywebview 里退出→重登→右上角三件套还在、点 ✕ 真正关窗。

---

## 附：★ 2026-10-02 —— "终端被永久标记 busy"的根因与根治（`tools/safe_run.py`）

### 现象
执行某一条命令后，工具端一直显示：

```
Command completion could not be observed; the command may still be running and must not be assumed to have succeeded.
The terminal has been left open and will not be closed automatically.
[Shell integration did not report command completion …]
```

随后**每条**命令都这样，Cline 只能不停新建终端（本次实测 20+ 个，VS Code 明显变卡）。

### 根因（不是"终端只复用不关闭"）
VS Code / Cline 判断"命令结束"靠 **shell 集成**：bash 在命令跑完后向 PTY 写一个
OSC 633 标记。**只要有进程还挂在同一条 PTY 上，或命令在等输入，这个标记就永远不来** ⇒
该终端被永久标记 busy ⇒ 后续命令全部"看不到完成"。

触发点就是下面这类写法（本文件旧版第 4 条规则教的写法）：

```bash
(timeout 240 python web_server.py --port 8767 > tmp/web867.log 2>&1 &) ; sleep 8
(timeout 180 msedge --headless=new --remote-debugging-port=9223 … > tmp/edge2.log 2>&1 &) ; sleep 12
```

`> log 2>&1 &` **只把 fd 指向文件**：子进程仍然是这条 shell 的子进程（同一控制台 / 同一进程组），
`msedge` 与 `uvicorn` 会长期持有那个句柄 —— 于是"终端坏了"，而不是"命令慢"。
另一个隐形版本是**命令在等 stdin**（pager、`y/n` 提示、REPL）。

### 根治：`tools/safe_run.py`
把子进程**真正脱离**，再由一个短命父进程回一行摘要：

| 措施 | 解决的具体问题 |
|---|---|
| `stdin=DEVNULL` | 命令不可能"等输入" |
| `stdout/stderr → 文件` | 子进程输出**从不进 PTY**，终端输出恒为十几行 |
| 新会话 / 新进程组（`setsid` / `CREATE_NEW_PROCESS_GROUP`） | 子进程不属于这条 shell，退出时不拖住 PTY |
| **`CREATE_NO_WINDOW`（不是 `DETACHED_PROCESS`！）** | 给子进程一个**隐藏的新控制台**：既脱离我们的终端，又不会弹系统错误框 |
| 硬超时 + `taskkill /T` / `killpg` | 挂住的命令不会永远挂着（返回 `124`） |
| `--bg` + `tmp/runs/bg_pids.json` + `--kill/--kill-all` | 服务器 / 无头浏览器"起得来也关得掉" |
| 摘要写 `tmp/runs/last.json`（含 `finished_at`） | **终端看不见输出时，用读文件拿结论**；还能判断"命令到底执行了没有" |

### ⚠️ 踩过的第二个坑：`--bg` 用 `DETACHED_PROCESS` 会弹系统错误框（2026-10-02 已修）
第一版为了"彻底不带控制台"给后台进程加了 `DETACHED_PROCESS`。结果在 Windows 上
**每次后台启动都弹出一个系统错误对话框**：

```
出现错误 2147942632 (0x800700e8)（启动 "…\.venv\Scripts\python.exe" -c "import time; time.sleep(120)" 时）
```

`0x800700E8 = ERROR_NO_DATA`（"没有可用的控制台 / 管道"）。根因：venv 里的
`python.exe` **不是解释器本体，而是一个启动器**（它还要再 `CreateProcess` 一次 base 解释器），
在"完全没有控制台"的环境里启动就会走这条报错路径 —— 是**我们少给了一个控制台**，不是用户机器的问题。

正确做法：`CREATE_NO_WINDOW`（= 给子进程一个**隐藏**的新控制台）。
`tests/test_tools_safe_run.py::TestSpawnFlags` 用"查标志位"的方式把它钉住
（这一组**不启动任何子进程**，所以回归它是零副作用的）。

### 验证（回归契约）
```bash
python tools/safe_run.py --timeout 300 --label pytest-tools -- \
    .venv/Scripts/python.exe -m pytest tests/test_tools_safe_run.py -q     # 13 passed
```
`tests/test_tools_safe_run.py` 把三条不可见性质钉成断言：**stdin 是 DEVNULL（不会吊住）**、
**超时真的杀进程并返回 124**、**包装器自己的 stdout ≤ 15 行**；另有 `--bg/--list/--kill/--kill-all` 的端到端测试。

### 顺带修掉的一个坑（写测试时踩到的）
Windows 上 `subprocess.run(text=True)` 默认按**系统 ANSI 代码页（GBK）**解码，而本工程工具统一输出
UTF-8 ⇒ 读线程抛 `UnicodeDecodeError: 'gbk' codec can't decode byte …`，测试以一种与业务无关的姿势失败。
**凡读本工程命令输出，都要显式 `encoding="utf-8", errors="replace"`。**

### 已经坏掉时的自救顺序
1. `python tools/safe_run.py --kill-all`（回收本工程遗留的 web_server / Edge）；
2. `Terminal: Kill All Terminals`（VS Code 命令，建议绑 Ctrl+Alt+K）；
3. `python tools/safe_run.py --timeout 30 --label probe -- echo ok` 验证"完成事件"已恢复。


### §十二补 4：PC 原生窗口为什么起不来（2026-10-03，新增 `tools/probe_clr.py`）

**现象**：`python main_gui.py` 抛
`RuntimeError: Failed to resolve Python.Runtime.Loader.Initialize from …\pythonnet\runtime\Python.Runtime.dll`
⇒ 窗口完全起不来（pywebview 的 Windows 后端在 `webview.start()` 里才 `import clr`）。

**证据**（一条命令给全：`.venv/Scripts/python.exe tools/probe_clr.py`；探测矩阵每行都是独立子进程，
每层跑完都能看到"报错换了个地方"，这也是排查顺序本身）：

| 层 | 实测 | 结论 |
|---|---|---|
| ① 运行时选择 | 默认 / `netfx` / `mono` 全 FAIL，只有 `PYTHONNET_RUNTIME=coreclr` OK（CLR 报 10.0.11） | pythonnet 在 Windows 默认走 .NET Framework；但 3.1.0 的 wheel 里 `runtime/Python.Runtime.dll` **只有 .NETStandard v2.0 一份**（递归扫过，无 netfx 子目录），netfx 加载器找不到 `Python.Runtime.Loader.Initialize` |
| ② WinForms 程序集 | 只设 coreclr ⇒ `FileNotFoundException: System.Windows.Forms`；补一份含 `Microsoft.WindowsDesktop.App`（+ `System.Runtime.InteropServices.BuiltInComInterop.IsSupported`）的 runtimeconfig ⇒ `WINFORMS_OK`。再 `clr.AddReference(TPA 里的 Microsoft.Win32.SystemEvents.dll)` ⇒ `add=ok`（**按名字** AddReference 会报 `AttributeError: 'NoneType' object has no attribute 'GetMethod'`） | 自己生成 runtimeconfig + `pythonnet.load('coreclr', runtime_config=…)` 能让 **WinForms 本身**可用 |
| ③ pywebview 自身 | 前进到 `winforms.py:680`（`class OpenFolderDialog` 类体）：`windowsFormsAssembly.GetType('System.Windows.Forms.FileDialogNative+IFileDialog')` 返回 **None** ⇒ `None.GetMethod('SetOptions', flags)` 崩 | **.NET Core 的 WinForms 没有 `FileDialogNative+IFileDialog`**（.NET Framework 才有）。这段代码在**类体里**、import 期执行，外部无从规避 ⇒ pywebview 6.2.1 的 WinForms 后端在 CoreCLR 下必然导入失败（上游 master 同款代码） |

**为什么不能降级 pythonnet**：`pythonnet==3.0.5`（netfx 还能用的最后版本）`requires_python = "<3.14"`，
而 Python 3.14 支持是 `3.1.0` 才加的（CHANGELOG 实测口径）⇒ 本机的组合无解。

**本项目的取舍**：
- `main_gui.prepare_clr_runtime()` 保留（`PYTHONNET_RUNTIME=coreclr` + 自动生成 runtimeconfig + `pythonnet.load` + 从 TPA 补 `SystemEvents`）——
  它是"必要但不充分"的一半，将来 pywebview 修掉 ③ 就能直接生效；
- `main()` 的异常收口从 `RuntimeError` 放宽到 `Exception`（三种异常长相不同：pythonnet 的 RuntimeError、
  pywebview 的 `WebViewException`、底层 .NET 异常），**日志记堆栈、屏幕只给一句人话，并自动回退 Edge/Chrome `--app` 独立窗口**；
- 回归：`tests/test_main_gui.py::TestClrRuntime`（8 例）钉住三个共享框架判断、版本数值排序、runtimeconfig 内容、以及"用户/非 Windows/缺运行时时一律不干预"。

**可用性现状**：原生窗口 = 不可用；`--app` 窗口 = 自动可用（无地址栏，功能与网页/手机端完全一致）。
想恢复原生窗口的三条路：① 等 pywebview 修 `FileDialogNative`；② 换 Python ≤3.13 配 `pythonnet==3.0.5`（走 netfx）；
③ 装 PyQt6 + PyQt6-WebEngine 走 pywebview 的 qt 后端。

**复现**：`.venv/Scripts/python.exe tools/probe_clr.py`（退出码 `0` = 原生窗口可用，`1` = 见上表）。


### §十二补 5：原生窗口白屏故障的最终定位与修复（2026-10-03 傍晚，**已修好**）

**终局现象**：`python main_gui.py`（VS Code F5）**窗口能弹出来但纯白**；而 `web_server.py` + 浏览器打开
`http://127.0.0.1:8765/` 一切正常。**现已恢复原生窗口**（验收：`pywebview-fail=0` 且日志出现页面自动任务行）。

#### 三层原因（一层比一层深，前两层是今天排掉的）
1. **运行时选择**：`pythonnet 3.1.0` 在 Windows 上默认走 `netfx`(.NET Framework)，但该 wheel 只带
   `.NETStandard2.0` 的 `Python.Runtime.dll` ⇒ `Failed to resolve Python.Runtime.Loader.Initialize`
   （字符串只存在于 `clr_loader/netfx.py:47`）。**实测**：默认/`netfx`/`mono` 全 FAIL，只有
   `PYTHONNET_RUNTIME=coreclr` 可用 ⇒ `main_gui.prepare_clr_runtime()` 负责：设环境变量 → 生成
   `tmp/clr-runtimeconfig.json`（含 `Microsoft.WindowsDesktop.App`）→ `pythonnet.load(...)` →
   从 TPA 补 `Microsoft.Win32.SystemEvents`。
2. **回退窗口也会白**：`open_browser_window()` 的 `--app` 窗口用**专用全新 profile**
   `tmp/gui-browser-profile`，Edge 首次用它启动会走"首次运行"流程 ⇒ 窗口先出来、内容空白；之后
   的启动只会把 URL **转发给这个坏实例**（进程表里找不到带 `--app=` 的进程就是转发特征）。
   ⇒ 已加 `--no-first-run --no-default-browser-check`（仍有偶发，故仅作兜底）。
3. **★ 真凶：pywebview 6.2.1 的 WinForms 后端在 .NET Core 下无法 import**：
   `webview/platforms/winforms.py:668` 的 `class OpenFolderDialog` **类体**里做反射
   `GetType('System.Windows.Forms.FileDialogNative+IFileDialog')`（.NET Framework 专有内部类型）
   ⇒ .NET 10 下返回 `None` ⇒ `None.GetMethod('SetOptions', flags)` 抛 `AttributeError`。
   异常发生在 **import 期** ⇒ `import webview.platforms.winforms` 整体失败 ⇒ pywebview 的
   Windows 后端（`winforms` 与 `edgechromium` 都走它）**一律起不来**。
   本工具**从不使用"选择文件夹"对话框** ⇒ 把这段反射整体包进 `try`、取不到就置 `None` 即可。

#### 修复清单（可复现）
| 落点 | 内容 |
|---|---|
| `main_gui.py` | `prepare_clr_runtime()`（coreclr + runtimeconfig + SystemEvents）＋ `main()` 统一收口异常并回退 `--app` |
| `webview/platforms/winforms.py` | **本地补丁**（`try/except` 包住 import 期反射）；备份 `winforms.py.orig` |
| `tools/patch_pywebview_winforms.py` | **幂等补丁器**：`--check` / 默认打补丁 / `--restore` 还原。**`pip install -r requirements.txt` 或重建 venv 后必须重跑**（PyInstaller 会照抄 venv 里这份文件，打包前也应先跑） |
| `main_gui.py::open_browser_window` | 加 `--no-first-run --no-default-browser-check`（兜底窗口不再卡首次运行） |

#### 顺手澄清的两个"记忆偏差"（都查过，别再重复怀疑）
- **不存在 Python 3.10**；机器上只有 `pythoncore-3.14-64`（`.venv` 的基底，09-20 11:00 建）与
  `pythoncore-3.11-64`（**2026-10-02 01:22 才装**，为安卓打包；它的 site-packages 里只有 pip）。
- **F5 一直用 `.venv` 的 Python 3.14**：`ms-python.debugpy` 日志三处一致
  （10-02 01:10 / 今天 14:38 / 今天 15:11）都写着 `executable='...\.venv\Scripts\python.exe'
  version='3.14.3.final.0'` ⇒ "莫名走了 3.14" 是错觉。
- **还原实验（10-02 00:18 版）今天秒崩**：`RuntimeError: Failed to resolve ... Loader.Initialize`
  （1.0 秒退出，连窗口都没有）⇒ 说明 netfx 通道现在确实不可用，**还原不是出路**，
  回退版备份留在 `tmp/main_gui.20261003-clr.py`。**图标/界面美化与本故障无关**（那条线已由
  "浏览器打开正常 + 无头渲染正常"排除）。

#### 验收 / 复发处理（一条命令 + 两个判据）
```
.venv/Scripts/python.exe tools/probe_clr.py        # 退出码 0 = pywebview 可导入（第③层已通）
.venv/Scripts/python.exe tools/patch_pywebview_winforms.py --check   # 必须显示「已打补丁」
py -3.14 tools/safe_run.py --bg --label gui -- .venv/Scripts/python.exe main_gui.py
# 判据：日志里 pywebview-fail=0（无「原生窗口初始化失败」）且 task-lines>0（页面自动任务跑起来了）
```
**未决（外部事项）**：上游 pywebview 仍未修该 import 期反射（6.2.1 为当前最新）⇒ 本地补丁需长期保留，
升级 pywebview 后先跑 `--check`，若显示"无法识别（上游可能改过）"则人工核对（判据见工具报错文案）。

