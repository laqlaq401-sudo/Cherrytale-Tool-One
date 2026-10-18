# 「展开任务卡片时执行按钮闪烁」修复方案

> 状态：**方案已定稿（访谈 3 轮已完成），尚未动代码**
> 决策日期：2026-10-16 ｜ 复现素材：`tmp/bugvideo/bug.mp4`（用户录制，8.69s / 640×1408）
> 关联笔记：`notes/manual_batch_exec_plan.md`（一键执行，同一片渲染区域）、`notes/web_layer.md`（前端分层）

---

## 一、bug 是什么

**现象**：在「主动任务」区**展开某张卡片**时，其它卡片上那些「正在执行 / 已在队列中」的按钮
会瞬间退回常态「执行」，过一小会儿（≤1.2s）又自己变回「停止执行」。反复点箭头就反复闪。

**根因（一行话）**：展开动作走的是"**整组重建任务列表**"，而重建出来的按钮**不读**
"这张卡在执行/排队中"这份状态；全列表唯一会把它写进 DOM 的函数，从来没被重建路径调用。

### 证据链（代码）

| # | 事实 | 证据 |
|---|---|---|
| 1 | 「停止执行」这个态只有一个前端来源：内存集合 `state.stopTasks` | `web/assets/app.js:86` |
| 2 | 唯一把它写进 DOM 的是 `syncExecButtons()` | `app.js:1792-1801` |
| 3 | 整组重建函数 `renderTaskGroups()` **不调用** `syncExecButtons()` | `app.js:849-856`（函数体只有 5 行：两个组、钻石页、状态行、一键执行状态行） |
| 4 | 重建是"清空 + 全部重造" | `app.js:858-869`（`container.textContent = ""` 后逐张 `renderTask`） |
| 5 | 新按钮由这两个函数生成，**都不读 `stopTasks`** | `manualExecControls()` `app.js:1732-1752`；`autoControls()` `app.js:1345+` |
| 6 | 展开/折叠就是调 `renderTaskGroups()` | 点卡片头部 `app.js:1301-1317`（调用于 `:1316`）；点 ▸ 箭头 `app.js:1408-1413` |
| 7 | 恢复靠下一次轮询 → `setRunning()` 的 `else` 分支 | `app.js:2832-2848`；轮询间隔 `app.js:3844-3851`（运行中 1200ms / 空闲 5000ms） |

⇒ **展开任何一张卡都会重建整个列表** → 所有卡片的执行按钮一起失去停止态 → 下一次轮询才补回来。
这解释了用户"闪的是**别的**卡片"的观感，也是"仍在运行中却显示成没执行"的由来。

### 证据链（视频实测）

用本机 ffmpeg 抽帧 + 对按钮区域做 `scale=1:1` 平均取色（30fps）得到颜色时间线：

- `6.700s 粉 → 6.733s 蓝`：**硬切、无过渡** ⇒ 是 DOM 节点被**替换**（新节点不参与 CSS transition）。
- `5.767s → 5.833s 蓝变粉`：**平滑渐变** ⇒ 是 `classList.toggle` 原地改类名，被 `.btn{transition-all}`（`web/assets/input.css:862`，150ms）平滑了。

两个特征交替出现，正是"重建抹掉 → 轮询补回"的循环。2fps 抽帧里那几张"蓝色按钮 + 停止执行文字"
是抓到了 150ms 过渡的**中间帧**，**不存在**"文案与类名不同步"的第二个 bug。

### 已排除的误判

- ❌ Tailwind 摇树：`.task-stop` / `.task-exec` 在 `web/assets/styles.css` 里**都存在**。
- ❌ 乐观更新：`app.js` 里不存在"本地先置为可执行"的代码；乐观方向恰恰相反（提交成功后**加入**停止态，见 `app.js:2998/3062/3121`）。
- ❌ 轮询竞态：`pollOnce` 有 `state.polling` 并发守卫（`app.js:3762-3763`），两次轮询不会互相覆盖。
- ❌ 后端误报空闲：`/api/status` 的 `running` 恒等于"队列非空"（`webapi/state.py:245`），不会在排队时误报空闲。

### 顺带查出的同源问题（同一根因的不同脸）

- **S1**｜`pollOnce` 里 `app.js:3800` 那次 `renderTaskGroups()`（主线最高通关变化时）会在
  `setRunning()` 刚恢复之后再抹一次 → 又要等一整轮轮询。
- **S2**｜`renderTaskGroups()` 共 **15 个调用点**（勾选 `app.js:1378`、纳入一键执行 `:1444`、
  全选/全不选 `:983`、**改参数 `:2419/2424/2426/2432`——数字输入框每敲一个字都会重建**、
  切区域、宴席刷新 `:1694`…），全都不补状态。
- **S3**｜`state.stopTasks` 是**纯内存** → **刷新页面就丢**；任务还在跑/排队却显示「执行」。
- **S4**｜单卡「停止执行」`stopCurrentRun` **不传 `run_id`**（`app.js:1808`）→ 服务端停的是
  "当前正在跑的那一批"，**哪怕你点的那张卡还在排队**。"按钮说停 A、实际停 B"。

---

## 二、术语先对齐（重要）

这个工具**没有任何远程服务器**。本方案里的用词一律按工程惯例：

- **「服务端」= 本机跑的那个 Python 进程**（`web_server.py` → uvicorn，默认 `127.0.0.1:8765`；
  要让手机连得显式 `--host 0.0.0.0 --token`，见 `web_server.py:5-13/134-140`）。
- **「持久化」= 往本机工程根写 JSON**（`.selection_state.json` / `.spend_state.json` /
  `.banquet_state.json` / `.login_signin_state.json`，全部已 gitignore，路径见 `config.py:881/919/930`）。
- **「多端」的真实范围**：①同一个 Python 进程被多个页面连（EXE 壳窗口 / 浏览器 / 换个端口再开）——
  真实存在；②`--host 0.0.0.0` 时手机浏览器连 PC —— 同一个进程、同一个队列；
  ③**安卓 APP 自己跑一个 Python（Chaquopy），彼此独立、不共享队列**。

⇒ 下文"多页面"指的是 ①②；**不要**说成"另一台设备"。

---

## 三、决策记录（访谈 3 轮，全部已拍板）

| 编号 | 议题 | 结论 | 被否方案与理由 |
|---|---|---|---|
| **D1** | 修复层次 | **两层都做**：①渲染层带上状态（治闪烁）②消除不必要的整组重建（治滚动回弹 / 输入被打断） | 只做①：用户明确要"轻量化问题一起做" |
| **D2** | 状态真源 | **搬服务端**：从 `/api/status` 派生 | 留前端内存：刷新场景治不了（S3） |
| **D3** | 验收判据 | **写进 `tools/verify_web_ui.py`**，三条 | 只靠人眼看视频：瞬间态容易漏、容易看错 |
| **D4** | 展开体落地形态 | **展开时插入控件、折叠时移除节点**（不用 `hidden` 隐藏） | 始终构建+`hidden`：会打破既有断言 `verify_web_ui.py:244/290`（"未展开 `.task-exec`==0 / 展开==1"，全量计数、不看可见性），等于削弱已有防线 |
| **D5** | 单卡停止口径 | **改**：按该卡所属批次的 `run_id` 精确停（S4） | 维持"停当前正在跑的那批"：按钮说的和做的不一致 |
| **D6** | 服务端派生口径 | **认一切 pending/running 的同名任务**（不看 `run_id` 归属） | 只认"我提交的批次"：刷新后 `submittedRunId`/`batchRunId` 同样丢失（`app.js:2931` 写 / `:3060` 读），治不了刷新 |
| **D7** | 乐观值 | **保留**：点下去立刻变「停止执行」，下一次轮询起以服务端为唯一准绳 | 纯等服务端：轮询空窗最多 1.2s 无反馈 |
| **D8** | 展开态持久化 | **一并搬服务端** | 维持"刷新一律折叠"：见下方 ⚠️ 待确认项 T1 |
| **D9** | 展开态落在哪 | **另开独立小模块 + 独立文件**（`services/ui_prefs.py` + `.ui_prefs.json`） | 并进 `selection_state`：`services/selection_state.py:131-132` 版本不符会**整份丢弃**，bump 版本会**清掉用户当前全部勾选与参数一次**；且违反工程"一个关注点一个文件"的既有范式（daily/banquet/spend/login_signin 都是各自独立一份） |

### ⚠️ 待确认项 T1（需要你点头，默认按你已选的 D8 执行）

`app.js:1396-1397` 有一条**明确写下的既有设计决策**，原文：

> 展开状态记在 `state.expanded`（只在内存里）：**刷新页面后一律折叠** ——
> 否则"上次点开的那张卡"看起来像"它还在排队执行"。

也就是说：**"刷新一律折叠"是当初为了防"误以为还在排队"而刻意做的**，不是遗漏。
你选的 D8 会反转它。我的判断是**反转是合理的**——因为本次（D2+D1）之后，
"一张卡有没有在执行/排队"由按钮自己的「停止执行」红描边态如实表达，
不再需要靠"卡片是否是展开的"去猜。落地时我会同步改掉这段注释。
如果你更想保守（保留刷新折叠），说一声，D8/D9 一并撤掉即可，其余不受影响。

---

## 四、改动方案

原则一句话：**「执行中 / 排队中」是服务端事实，前端不自己记；前端只提供"提交到第一次轮询之间"的乐观过渡值。**

### 4.1 前端渲染层（治闪烁的根，对应 D1①）

- 抽出**唯一规则**并让两个地方共用：
  - 一个纯判定函数（如 `isTaskActive(name)`），输入任务名，输出"该显示停止态吗"。
  - 一个施加函数（如 `applyExecButtonState(button, name)`）：`classList.toggle("task-stop", …)`
    + `textContent` + `title`。**所有**"改按钮外观"的地方只走它。
- **调用点两处**：
  1. **构建按钮时**（`manualExecControls()` `app.js:1732`、`autoControls()` `app.js:1345`）——
     本次的关键补丁，让"任何渲染路径造出来的按钮天生是对的"；
  2. `syncExecButtons()`（`app.js:1792`）遍历时原地更新（现状保留）。
- `renderTaskGroups()` 末尾补一次 `syncExecButtons()` 作为**冗余保险**（成本极低）。
  ⚠️ 注意：补这一句之后，**正确性已经不依赖它**了 —— 这是刻意的：真正的修复是"渲染自带状态"，
  不是"渲染完再补救"。
- 顺手修掉 `syncExecButtons()` 里 `button.title = stopping ? "中断正在执行的任务" : button.title;`
  这个 `else` 分支的自赋值空操作（`app.js:1799`）。

### 4.2 状态真源搬到服务端（对应 D2 / D6 / D7）

- **服务端不需要改**：`/api/status` 已经把"正在跑那一批的步骤 + 排队批次的 pending 步骤"
  拼进 `steps`，且每项带 `run_id`
  （`webapi/state.py:250-255` 聚合、`:159` 输出字段、`:313` 赋值）。
- 前端把 `state.stopTasks`（Set of 任务名）换成两个东西：
  - `state.activeTasks`：**每次轮询**由 `steps` 派生 —— `status ∈ {pending, running}` 的步骤 → 任务名。
    允许多张卡同时处于停止态（现状也允许多张）。
  - `state.optimisticRuns`：提交成功后的**短命**过渡集合，在**下一次拿到 `/api/status` 时无条件清空**
    （此时服务端必然已经反映了真实状态，所以不会残留）。
  - 最终判定 = 两者**并集**。
- ⚠️ **安全性前提（必须写测试锁住）**：`steps` 里还包含最近 10 批**已完成**的步骤
  （`webapi/state.py:250-251` 的 `_recent[-10:]`）。这条判据依赖"已完成步骤的 `status` 只会是
  `ok/fail/skipped`，永远不可能是 `pending/running`"。加一条服务端测试把这条钉死。
- **收益**：S3（刷新丢状态）自动消失；多页面（术语见第二节）看到同一份事实；
  顺带得到 `任务名 → run_id` 的映射，供 4.3 用。

### 4.3 单卡「停止执行」精确停（对应 D5）

- `stopCurrentRun(task)`（`app.js:1806-1819`）：先查该任务所属批次的 `run_id` →
  `POST /api/stop {run_id}`（服务端已支持：`webapi/app.py:312-329` `StopPayload{run_id}`、
  路由 `:1257`、分发 `:1306-1307` → `webapi/state.py:422-423` → `_stop_specific` `:447-501`，
  三分支：当前批→退化为普通停 / 排队中→摘队列标 skipped / 已结束→`stopped:false`）。
  查不到 `run_id` 再退回空 body（老语义）。
- 回显文案对齐 `stopManualBatch` 的措辞（`app.js:3084` 已有"它还在排队，已直接撤销"）。
- ⚠️ **已知限制（写进代码注释）**：`/api/stop` 的响应**不回显 `run_id`**
  （`webapi/state.py:440-445/477/495-501`，只有 `stopped/cancelled_queued/skipped/message/queued_runs`），
  所以前端无法凭响应确认"撤的是不是我那批"，只能靠自己派生的映射。若将来要闭环，需要在服务端回显。

### 4.4 消除不必要的整组重建（对应 D1② / D4）

新增一个"只动这一张卡片"的切换函数（如 `toggleCardExpanded(taskName)`），展开/折叠时：
1. 切换卡片节点上的 `.task--expanded` / `.task--on` 类；
2. 切换参数区 `paramsBox` 的 `hidden`（该区**本来就是"始终构建 + 切 hidden"**，`app.js:1244-1247`，不动它）；
3. **插入 / 移除**执行控件节点 —— 即 `manualExecControls(task)` 或
   （宴席卡）`bbqMealControls(task)` 的产物。`app.js:1259-1263` 现在只在 `expanded` 时构建它们，
   改造为"挂在切换函数里插入/移除"。
   ⚠️ **必须是"移除"而不是"隐藏"**，否则打破 `verify_web_ui.py:244/290`（见 D4）。
4. ⚠️ **必须保留"展开宴席卡且缓存过期时才拉一次窗口"这个时机**（现于 `app.js:1307-1309`，
   条件 `task.name === "bbq_energy" && banquetCacheStale()`）。注释里明确警告 32007/32008 是
   **风控可见流量、不能无脑每次都发** —— 绝不能因为改成"构建隐藏体"而变成每次重建都发。
5. 卡片节点已带 `data-task`（`app.js:1330`），定位卡片无需新加属性。

**改参数不再整组重建**（消 S2 的输入打断）：
- `renderParam` 各分支现在调 `renderTaskGroups()`（如下拉 `app.js:2419/2424/2426/2432`）。
  改为调用一个**局部刷新**（只重绘钻石页与三处状态行：`renderDiamondList()` / `renderAutoStatus()` /
  `renderManualBatchStatus()`，即 `renderTaskGroups()` 现在做的那几件事里**除"重建卡片"之外**的部分）。
  ⚠️ 实现时先确认 `renderDiamondList()` 不会反过来触碰任务卡片 DOM。
- 参数值本身写进 `entry.params` 后由现有 `saveSelection()` 防抖双写（`app.js:669-681/698-714`），无需改。

**勾选框不再整组重建**：
- `autoCheckbox`（`app.js:1378`）、`manualPickControl`（`app.js:1444`）、`setAllBatchPicks`（`:983`）
  改为只更新勾选框自身 + 状态行；"勾上就立刻跑一次"的编排（`app.js:1387`）**原样保留**。

**明确保留整组重建的路径**（它们重建**也不会再闪**，因为 4.1 兜住了）：
换活动/素材区域（参数集合真的变了）、`loadTasks` 首屏装配、`setRunning()` 的"队列跑空"分支
（`app.js:2837-2840`）、`pollOnce` 里主线通关变化（`app.js:3800`）。
> 注：S1 那条（`app.js:3800`）在 4.1 落地后自然消失，**不需要**单独处理。

### 4.5 展开态服务端持久化（对应 D8 / D9）

**服务端（新增，照抄 `services/spend_state.py` 的骨架）**
- 新文件 `services/ui_prefs.py`。骨架直接对齐 `services/spend_state.py`：
  `_FILE_VERSION` / `_lock` 线程锁 / `_default()` / `load()`（缺失或损坏→静默降级只留 WARNING，
  理由同 `spend_state.py:66-99`）/ `save()`（整份原子替换）/ 对外两个动作
  （`expanded_tasks()` 读、`set_expanded(list)` 写并回显）。
- 文件结构：`{"version": 1, "expanded": [...任务名...], "updated_at": "..."}`。
- **白名单**：写入与读出都只保留 `services/task_spec.py` 里存在、且 `group == "manual"`、
  且未 `hidden` 的任务名（防手改坏文件、防过期任务名长期堆积）。
- `config.py` 新增 `UI_PREFS_FILE`（env 名 `CHERRYTALE_UI_PREFS_STATE`，默认
  `PROJECT_ROOT / ".ui_prefs.json"`），风格对齐 `config.py:919-935` 的 `SPEND_STATE_FILE`。
- `.gitignore` 增加 `.ui_prefs.json`（现有同族在第 72-78 行）。

**接口层（`webapi/app.py`）**
- 新增请求体模型 `UIPrefsPayload{expanded: list[str]}`，放在 `SelectionPayload`/`StopPayload` 同处
  （`app.py:299-329`）；登记进 `payload_models`（`app.py:1247-1258`）+ POST 分发（`app.py:1296-1307` 同款）；
  新增 handler（对齐 `WebApp.set_spend` `app.py:1063-1074` 的薄写法）。
- **读**走 `GET /api/tasks` 的返回体（`app.py:411-447`），加一个 `"ui_prefs"` 键 ——
  理由与 `selection`/`spend` 放在那里一致（首屏装配一次拿全）。

**前端（`web/assets/app.js`）**
- `state.expanded`（`app.js:124`）**保持为内存态**（渲染只读它，避免把网络依赖带进渲染路径）。
- 在 `loadTasks` 的合并点（`app.js:3896-3942`，那里已经有 `selection`/`spend` 的"服务端优先、
  本地兜底"范式）加一路：服务端有记录 → 回填 `state.expanded` + 写 localStorage；
  服务端没有 → 读 localStorage 兜底。
- 每次切换后 debounce 双写（照 `scheduleSelectionSync` `app.js:698-714` 的 500ms 防抖）。
- **同步改掉 `app.js:1396-1397` 那段注释**（见待确认项 T1）。

---

## 五、验收方案（对应 D3）

### 5.1 `tools/verify_web_ui.py` 新增内容

**前置动作（必须，否则会误红）**：脚本开头先把展开态**重置为空并写回服务端**。
理由：展开态一旦持久化，上一次运行留下的展开态会让 `verify_web_ui.py:244`
（断言"未展开时 `.task-exec` 恰好 0 个"）失败 —— 等于给脚本引入了环境依赖。
做法与它现有的"强制打开演练开关"（`verify_web_ui.py:249-259`）同款。

**三条新判据**（脚本用无头 Edge + CDP，`CdpSession.evaluate` `verify_web_ui.py:161-174`
可以直接取回任意 JS 表达式的值）：

1. **闪烁照妖镜**（本次 bug）：提交一次演练执行 → 等目标卡按钮变成「停止执行」→
   **再展开另一张卡** → 断言前一张卡的按钮 `className` 与 `textContent` **逐字节不变**。
   （这样设计是因为：折叠态下 `.task-exec` 节点本就不存在，所以必须拿"另一张已展开且处于停止态的卡"来比。）
2. **防回弹**（防 D4 改坏）：展开/折叠前后 `window.scrollY` 不变、正在输入的那个框仍是
   `document.activeElement`。
3. **防刷新丢状态**（防 D2 改坏）：提交后按钮立刻是「停止执行」；页面 `location.reload()`
   后再等一次轮询，**仍是**「停止执行」。

**既有判据一条都不放宽**：`verify_web_ui.py:230/237/244/290/297/357-363/370-376/393-397/450/471` 全部保留。
⚠️ 该脚本现有判据里有一条 `#btn-run` 数量为 0、`.task-exec` 恰好 1 个之类的**容易被新增控件误红**
（该风险在 `notes/manual_batch_exec_plan.md:43/205` 已记录）→ 本次新增控件（执行控件节点的插入/移除）
落地后要重跑一遍确认不是被误红，而是真的符合预期。

### 5.2 服务端新增测试

- `tests/test_ui_prefs.py`：照 `services/spend_state.py` 对应的测试范式（缺文件 / 坏 JSON /
  版本不符 / 字段类型不符 / 白名单过滤 / 并发读写）。
- `/api/status` 派生判据的服务端测试：**已完成批次的步骤 `status` 只会是 `ok/fail/skipped`，
  永远不会是 `pending/running`**（4.2 的安全性前提）。造一个"队列里有排队批次"的场景断言 `steps` 形态。

### 5.3 完整验证顺序（对应"全跑"）

| 步 | 命令 / 动作 | 备注 |
|---|---|---|
| 1 | `node --check web/assets/app.js` | 语法守卫（`AGENT.md:163`） |
| 2 | 起 `web_server` + 无头 Edge(9223)，跑 `python tools/verify_web_ui.py 9223 8767` | 前端交互回归；建议经 `tools/safe_run.py` 转一手 |
| 3 | 检查是否动了 CSS：本次预计**只复用现成类名**，若确实新增了类名则重跑 `npx tailwindcss -i ./web/assets/input.css -o ./web/assets/styles.css --minify` | ⚠️ 判据：改完拿目标类名 `grep -c` 一下 `web/assets/styles.css`，**0 次 = 被摇掉了**（本工程踩过两次） |
| 4 | 无头 Edge `--dump-dom` 抓渲染后 DOM 对照 | `AGENT.md:163` |
| 5 | 全量 pytest：`.venv_backup_314/Scripts/python.exe -m pytest tests --basetemp="tmp/pytest-base-$(date +%H%M%S)"`（⚠️ 本机 `.venv/Scripts` 是空的；⚠️ basetemp 每次要唯一，见 T-D；⚠️ 不要再加 `-q`，见 T-F） | 本次实测 **1818 passed / 16 skipped**（基线 1780/10 是更早的树，差额见 §8.1） |
| 6 | `tools/sync_android_assets.py`（**单独一条命令**、且**必须前台**，见 T-G） | 改 `web/` 后必须同步，否则安卓端还是旧代码；半途失败要重跑到完整再打包 |
| 7 | APK 打包 | **按 2026-10-06 起的约定：由你手动打包**，产物 `dist/android/CherrytaleTool-<版本>-beta.apk` |

**人工验收**：按你录视频的**同一条路径**复看一遍（反复点「主线关卡通关」的折叠箭头，
看其它卡的按钮是否纹丝不动）。

---

## 六、风险与对策

| 编号 | 风险 | 对策 |
|---|---|---|
| R1 | 局部更新改坏既有交互（输入焦点丢失 / 滚动回弹） | 验收判据 2 直接兜住；这是本次改动的首要回归面 |
| R2 | `activeTasks` 派生误判（把**已完成**步骤当成在跑） | 依赖"已完成步骤 status 恒非 pending/running"，加服务端测试钉死（5.2） |
| R3 | 展开态持久化反转既有设计 | 见待确认项 T1；落地时同步改注释 |
| R4 | `verify_web_ui.py` 引入环境依赖（脏展开态） | 脚本开头重置展开态（5.1 前置动作） |
| R5 | 乐观值残留 → 按钮永远显示「停止执行」 | 规则定为"下一次拿到 `/api/status` 就无条件清空乐观集合" |
| R6 | 改了 `web/` 忘记同步安卓 | 验证清单第 6 步（单独命令） |
| R7 | 宴席卡的联网读取时机被改坏（风控流量放大） | 4.4 第 4 点：切换函数里显式保留 `banquetCacheStale()` 判断与拉取 |

---

## 七、回滚

全部改动集中在 4 个文件 + 1 个新文件：

- 改：`web/assets/app.js`（主要）、`webapi/app.py`、`config.py`、`tools/verify_web_ui.py`、`.gitignore`
- 增：`services/ui_prefs.py`、`tests/test_ui_prefs.py`、`notes/exec_button_flicker_plan.md`（本文）

**开工前**：先记下当前 `git rev-parse HEAD`（或先提交一次现状），退回用
`git checkout -- <文件>` / `git stash` 即可；新增文件删除即可。**不涉及任何数据迁移、不动任何状态文件**。

**建议的实施顺序**（每一步都可单独验证、可单独回滚）：
1. 4.1 渲染层带状态（**这一步就治好闪烁**）→ 复看视频路径 → 提交
2. 4.4 消除整组重建 → 跑验收判据 2 → 提交
3. 4.2 + 4.3 状态搬服务端 + 精确停止 → 跑验收判据 3 → 提交
4. 4.5 展开态持久化（含 T1 确认）→ 跑验收判据 1 + 前置动作 → 提交
5. 一次性跑完 5.3 的 7 步

---

## 八、验证结果（2026-10-16 实跑）与这一步新查清的坑

### 8.1 结果

| 步 | 结果 |
|---|---|
| 1 `node --check web/assets/app.js` / `common.js` | ✅ 通过 |
| 2 `tools/verify_web_ui.py`（无头 Edge + CDP） | ✅ **23/23 判据通过**（rc=0），含新增 12/13/14 |
| 3 目标类名 `grep -c web/assets/styles.css` | ✅ `task-stop`(3 条规则) / `task-exec` / `task--expanded` 都在；`task-exec-slot` 是纯 DOM 标记、本就无 CSS → **无需重建 Tailwind** |
| 4 渲染后 DOM 对照 | ⚠️ `--dump-dom` 已失效（见 T-E）→ 改用 **CDP 取真实 DOM 普查**：`task=8 toggle=8 expandedCard=1 autoExec=10 pickbox=6 btnRun=0 btnRunManual=1 bbqCard=1 htmlLen=50298`，`conn=conn--busy` |
| 5 全量 pytest | ✅ **1818 passed / 16 skipped**，rc=0（`--basetemp` 用唯一目录，见 T-D） |
| 6 `tools/sync_android_assets.py` | ✅ 12 项全同步；`app.js / common.js / ui_prefs.py / app.py / config.py` 与桌面端 **sha256 一致**（必须**前台**跑，见 T-G） |
| 7 APK 打包 | ⏸ 按约定由用户手动执行 |

**pytest 与基线的差额**：本次改动新增 **+13** 条（`tests/test_services_ui_prefs.py` 12 +
`tests/test_webapi_state_queue.py` 1）；另有 6 条因 `captures/` 未同步（"抓包文件不在"）
由 passed 转为 skipped；其余来自**更早会话未提交**的用例（如
`tests/test_manual_batch_exec.py` 12 条）。基线 1780/10 是更早的树状态，
不是同一份用例集 —— 比对时以"**零失败、零 error**"为准。

闪烁照妖镜三条（本次 bug 的直接判据）值得单独记一句：`sameNode=True`、
`clsBefore==clsAfter`、`textBefore==textAfter` —— 展开另一张卡时已展开卡的执行按钮
**节点没被换、配色文案逐字节没变**；折叠/再展开 `sameNode=True`、`scrollSame=True`。
这就是"按钮不再闪"的机器证明。

### 8.2 新查清的坑（都在这一步踩到，全部会影响下次）

**T-A ⚠️ 最耗时的一个：`getApiOrigin()` 会把页面指回 8765。**
`web/assets/common.js:155-171` 规定"端口不是 8765 / 8000（安卓包内服务器）时，
API 一律指向 `<host>:8765`"。校验脚本为了不撞用户正在用的 8765 而跑在 8767 ——
于是页面把 `/api/health` 发到**没人监听的 8765**：`conn--bad`「服务不可用」→
`boot()` 抛错被 `catch` 吞掉 → `loadTasks()` 压根没跑 → `#tasks-manual` 是个**空 div**。
症状极具误导性：`readyState=complete`、**零 JS 异常**、`/api/tasks` 手测 200，
只有"任务列表是空的"。排查花了半小时。
**定式**：以非 8765 端口跑前端校验时，页面 URL **必须**带
`&api=http://127.0.0.1:<端口>`；脚本现已自己 `Page.navigate` 到 `CANONICAL_URL`
（含该参数），不再依赖"Edge 是被哪个 URL 拉起来的"。

**T-B ⚠️ 三条老判据（步骤顺序 / 一键执行顺序 / skipped）改前就是红的 —— 已用基线对照证实。**
`webapi/state.py::status()` 的 `steps` 是**扁平化**的：`_recent[-10:]`（**已跑完的历史批次**）
+ 当前 + 排队。而老断言拿**整条串**跟 `sweep_activity,daily_box` 做全等比较 ——
只要服务端进程里跑过别的东西就永远不成立。而"别的东西"必然存在：脚本开页时前端会
触发一次「启动」自动任务，`.daily_state.json` 缺失时 `auto_done` 恒空 ⇒ **每次都重跑**。
连带后果是重试循环把同一批**重复提交 3 次**（实测步骤列表里出现 3 份 `top_pvp_battle,daily_box`）。
**证据**：把 `app.js / app.py / verify_web_ui.py` 换回改动前的备份各跑一次 → 同样这 3 条红。
**修法**：按**最后出现的那个 `run_id`** 分组取步骤（`LATEST_RUN_STEPS_JS`），
老服务端无 `run_id` 时自动退化成旧行为。

**T-C ⚠️ dry-run 一步只要 ~0.2 秒（4 步整批实测 0.79 秒）→ "需要有一批在跑"的判据会空转。**
单批场景下前端 1.2s 的轮询根本抓不住：界面没有 `.task-stop`、服务端也没有
pending/running，两边都是空集，"怎么比都一致"（假通过）；点「停止执行」更是
"没东西可跳"（假红）。
**定式**：先 `queue_streak()` **连点「一键执行」几次**。按钮的"执行/停止"双态靠
`state.running && state.batchRunId` 判定、而 `state.running` 是轮询来的 ⇒
连点间隔远小于一个轮询周期，每次点击都走"提交"分支 → 队列排起 N 批。
排队批次的步骤**一直是 pending 且每批同名** ⇒ "哪些任务处于活动态"这个**名字集合**
在若干秒内恒定 ⇒ 前端轮询能稳定对齐，竞态被消掉（不是"多等一会儿等它变绿"）。
另加了"必须 `active > 0` 才算比过一次"的条件，专治"两边都是空集"的假通过。

**T-D ⚠️ `pytest --basetemp` 复用同一目录 → 242 个 setup error。**
第二次跑时 `tmp/pytest-base` 已存在，pytest 要删它，而**宿主把 `shutil.rmtree` 换成了
安全删除垫片**（工程里搜不到 `_should_bypass_safe_delete`，是宿主注入的）⇒
抛 `SystemExit: 1` ⇒ 之后每个用例都 ERROR，看起来像"大面积炸了"。
**定式**：`--basetemp="tmp/pytest-base-$(date +%H%M%S)"`，每次唯一。

**T-E ⚠️ `--dump-dom` 在当前 Edge 构建上输出 0 字节。**
`--headless=new` 与 `--headless=old` 都试过，`--virtual-time-budget=9000` 也给了，
HTML 仍是 0 字节、stderr 也空 —— 但同一个 server 日志显示页面确实执行了
（跑起了 wallet/login 任务）。⇒ 该 flag 已失效。
**替代**：用 `CdpSession` 直接读真实 DOM（见 9.1 第 4 步的普查）。
`AGENT.md:163` 那条"无头 Edge 抓渲染后 DOM"的写法需要更新。

**T-F `pytest.ini` 的 `addopts` 里已有 `-q`；命令行再加一个 `-q` = `-qq`，会把结尾的
`N passed / M skipped` 汇总行**整条**隐藏掉（输出看上去"跑完了但没数字"）。

**T-G 安全删除闸门要**前台**才能拿到交互确认。**
`tools/sync_android_assets.py` 用 `shutil.rmtree` 重建 `models/`（55 个）与 `web/`
（254 个），都超闸门阈值 50 ⇒ 垫片写 `signal-call_*.json`（`confirmRequired`）等宿主确认。
**两次后台调用都被拒**（只同步到 `client/ crypto/` 就 `SystemExit`，rc=1 ——
安卓侧一度处于"半新半旧"的不一致态），**改前台后一次通过**。
⇒ 这条命令**必须前台跑**（或在会弹确认的时机跑）。另：半途失败时**务必重跑到完整**
再打包 APK，否则会把新旧混合的代码打进包。

---

## 九、遗留 / 未决

- **T1**：反转"刷新一律折叠"（`app.js:1396-1397`）—— 等用户点头，默认按 D8 执行。
- `/api/stop` 响应不回显 `run_id`（4.3 已知限制）：本次不处理，若将来要闭环需在服务端回显。
- 单卡「停止执行」现在允许多张卡同时处于停止态（现状亦然，非本次引入）。
- 备用方案留档：展开体"始终构建 + `hidden` 隐藏"被否（见 D4），
  若将来 `verify_web_ui.py` 的计数判据改成"可见性过滤"，可重新评估。
