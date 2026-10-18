# 主动任务：单卡勾选框 + 顶部一键批量执行（最终方案 + 实施记录）

> 需求：**每个参与一键执行的主动任务卡片右侧加一个和自动任务一样的勾选框；主动任务分区顶部加「一键执行」**。
> 一~九节是定稿方案（结论经 grilling 逐条确认，事实依据附文件:行号）；
> **第十节是 2026-10-16 的实施记录**（含实施期才暴露的四个坑）。

---

## 一、已定决策（18 条，不再改动）

| # | 决策 |
|---|---|
| 1 | 勾选框在卡片**右侧**，与自动任务同款 `input[type=checkbox].toggle`（全局选择器 `input.css:795`，零新 CSS） |
| 2 | 勾选**不展开参数区**（`bodyOpen` 逻辑不动：主动任务仍是"箭头展开才看参数"） |
| 3 | 勾选**不触发**任何自动执行（不调 `runAutomation`） |
| 4 | 顶部工具行：状态行 + `全选` + `全不选` + `一键执行（N）` |
| 5 | 全选/全不选只作用于"参与一键执行且 `enabled`"的卡片 |
| 6 | 一键成员 = **6 个**：`bbq_energy` → `sweep_activity` → `top_pvp_battle` → `sweep_material` → `market_buy` → `alliance_donate` |
| 7 | `buy_energy`、`push_main_stage` **不参与**，且**不显示勾选框**（保持现状：只有展开箭头） |
| 8 | 顺序：宴席第一，其余按规格表声明顺序（用 `batch_order` 表达，前端零特例） |
| 9 | 一键 = **一次 `/api/run` 提交**（不是 N 次），这样 `stop` 才能"停掉当前之后所有任务" |
| 10 | 每个任务后各跟一次 `daily_box`（照抄 `runManualTask` 拼法）；**宴席项不带宝箱** |
| 11 | 不额外设计任何宝箱逻辑 —— 只是把单卡的拼法重复 N 遍 |
| 12 | 一键按钮运行中变「停止执行」；停止 = 从当前任务开始，之后所有任务不执行 |
| 13 | 停止按 `run_id` 精确撤批（在跑→中断；排队→移出队列），不会误伤别的批次 |
| 14 | 一键执行**不做二次确认**（与主动任务既有原则一致） |
| 15 | 一键执行后**保留**勾选，不自动清空 |
| 16 | 除宴席外，**不做**任何"今天做过没"的判断（扫荡次数/市集买满/捐献上限全交给服务端） |
| 17 | 宴席窗口状态改为"**登录/切区后自动拉一次 + 缓存 + 跨边界失效**"，不再需要手动点「刷新」 |
| 18 | `alliance_donate` 像宴席一样：**执行成功后**本游戏日置灰，一键里跳过 |

---

## 二、关键事实依据（改动前先读这几条）

1. **宝箱不是服务端行为**，是前端 `runManualTask`（`app.js:2630-2631`）拼的 `[该任务, daily_box]`；`onBbqMealClick`（宴席专用按钮）**刻意不串宝箱**（`app.js:1337-1340`：语义要单一）。⇒ 一键必须自己拼，否则一次宝箱都不领。
2. **`/api/stop` 只中断"当前这一批"**（`webapi/state.py:398-432`）：把该批 `pending` 步骤标成 `skipped`，**队列里其他批次照跑**；取消检查点在 `services/runner.py:851` 的任务边界。⇒ 一键必须一次提交。
3. **`runner.validate_request`（`services/runner.py:621-658`）不查任务名重复** ⇒ 一次提交里可以放两个 `bbq_energy`（`meal=morning`/`evening`）。
4. **服务端已有"成功后按名单记账"的挂钩**：`webapi/state.py:705` 对 `is_once_per_day(任务名)` 调 `daily_state.mark_completed`；账本经 `/api/tasks`（`app.py:402`）与 `/api/status`（`state.py:272`）下发 → 前端 `state.autoDone`（`app.js:3420/3312`）。宴席是另一套：`banquet` → `state.banquet`（`app.js:3422/3314`），实时窗口另走 `/api/banquet/status` → `state.banquetMeals`（`app.js:1373`）。
5. **⚠️ 该记账不看 `result.ok`**（`state.py:705`）—— 失败也记一笔。直接复用会让"捐献失败后当天置灰、无法重试"。
6. **宴席窗口无法纯本地推算**：起止时刻与期号 `groupID` 只在 32008 响应里（`models/bbq.py:58-79`、`tasks/bbq.py:143/266-319`）；仓库只有 10-02/10-03 一天样本，`notes/bbq_second_meal_plan.md` L149-151 自认"是否每天重发未验证"。本地只有空档 06:00–06:10 与换日 06:10（`models/daily_reset.py:61-68/160-189`）。
7. **宴席"吃过没"是本地记账**（`services/banquet_state.py`，`identity_key` 分桶、`banquet_day` 06:10 换日）；`daily_state` 是游戏日 05:00 换日。
8. **前端宴席按钮可点性判据**（`app.js:1283-1286`）：`!eaten && edible && stateKey ∈ {ready, makeup, unknown}`；`eaten` 时按钮与邀请开关都锁死。后端 `state`/`edible`/`invitable` 是唯一窗口口径来源（`tasks/bbq.py:961-1048`，前端不自己推窗口规则 —— 见 `app.js:101` 注释）。
9. `tools/verify_web_ui.py` 现有三条判据会与新控件冲突：主动任务卡各 1 个 `.task-toggle`（L222）、`#btn-run` 数量为 0（L229）、展开后 `.task-exec` 恰好 1 个（L236/282）。

---

## 三、成员与顺序：由规格表驱动（前端不抄名单）

沿用 `once_per_day` 的既有原则（`task_spec.py:1060-1063`："名单只有这一个来源"），给 `TaskSpec` 加两个字段：

```python
batch_eligible: bool = False          # 参与「一键执行」（显式登记才进，默认不进）
batch_order: int | None = None        # 批次内排序；None = 按声明顺序
```

登记（`services/task_spec.py`）：

| 任务 | batch_eligible | batch_order |
|---|---|---|
| `bbq_energy` | True | **-1**（宴席第一） |
| `sweep_activity` | True | None |
| `top_pvp_battle` | True | None |
| `sweep_material` | True | None |
| `market_buy` | True | None |
| `alliance_donate` | True | None |
| `buy_energy` / `push_main_stage` / 其余 | **不登记**（默认 False） | — |

排序：`sorted(members, key=lambda t: (t.batch_order if t.batch_order is not None else 0, 声明索引))` —— 稳定排序，宴席（-1）在最前，其余保持声明顺序。
`describe_tasks()`（`task_spec.py:1289` 附近）把两个字段一并下发。

---

## 四、前端改动

### 4.1 卡片右侧勾选框

- `renderTask()`（`app.js:980-1062`）：主动任务头部改为三段 `[manualToggle, info, manualCheckbox]`，容器仍 `task-head--manual`；`.task-info` 是 `flex-1 min-w-0`，第三段自然靠右，**不需要新 CSS**。
- 头部已有的点击展开 handler（`app.js:1032-1048`）会跳过 `button, input, select, a, label` ⇒ 点勾选框不会顺带展开，无需改动。
- 新增 `manualCheckbox(task, entry)`（与 `autoCheckbox` 同结构、不同语义）：
  - 只在 `task.batch_eligible === true` 时渲染（`buy_energy`/`push_main_stage` 因此没有勾选框）；
  - `checked = entry.checked`、`disabled = !task.enabled`、`aria-label = "纳入一键执行：<标题>"`；
  - `change` → `entry.checked = wanted` → `saveSelection()` → `renderTaskGroups()`；**不调 `runAutomation`**。

### 4.2 顶部工具行

`web/index.html` 的 `#panel-manual`（L156-162），在 `p.hint` 之后、`#tasks-manual` 之前插入：

```html
<div class="manual-batch-bar">
  <p id="manual-batch-status" class="auto-status">主动任务：读取中…</p>
  <div class="manual-batch-actions">
    <button id="btn-select-all" class="btn btn--ghost btn--compact" type="button">全选</button>
    <button id="btn-select-none" class="btn btn--ghost btn--compact" type="button">全不选</button>
    <button id="btn-run-manual" class="btn btn--primary" type="button">一键执行</button>
  </div>
</div>
```

`renderManualBatchStatus()`（在 `renderTaskGroups()` L832 末尾、勾选 change、`setRunning()` L2473 里各调一次）：

- 未登录 → `主动任务：需要先登录`（`--warn`）
- 待执行 0 → `主动任务：未勾选任何任务`（`--warn`）；若勾了但全部已执行过 → `主动任务：勾选的任务本日都已完成`
- 运行中 → `主动任务：运行中…`
- 否则 → `主动任务：将执行 N 个（<标题、标题…>）`

`#btn-run-manual` 文案 `一键执行（N）`，`disabled = (待执行数 === 0)`；**运行中不禁用**（排队是既有设计）。

### 4.3 `runManualBatch()`

```
1. 若宴席被勾选且 state.banquetMeals 为空或已过期
   → await refreshBanquetStatus()（失败则记 state.banquetUnavailable = true）
2. jobs = []
   ├ 宴席（勾选时）：按"卡片可点性判据"产出 0~2 项
   │    [{name:"bbq_energy", params:{...entry.params, meal:"morning"}}]
   │    [{name:"bbq_energy", params:{...entry.params, meal:"evening"}}]
   │    —— 早在前晚在后；不带 daily_box
   └ 其余勾选项（按 batch_order 顺序）：
        {name, params} + {name:"daily_box", params:{}}
3. jobs 为空 → 提示 + return
4. await submitRun(jobs, {reason:"主动任务一键执行（N 个）", origin:"manual"})
5. 成功 → state.batchRunId = 本次 run_id；state.stopTasks 加入本批全部任务名；syncExecButtons()
```

- `submitRun`（`app.js:2561`）需要把 `result.run_id` 存下来（目前只在 `console.info` 里用过）→ 加一行 `state.lastRunId = result.run_id`。
- **宴席提交判据**（与卡片按钮完全一致，避免"按钮灰着但一键却提交了"）：
  - `eaten = state.banquet.meals.includes(meal)` → 有则跳过；
  - `stateKey` 取 `state.banquetMeals` 里该 meal 的 `state`：`ready` / `makeup` → 提交；`gap` / `not_started` / `expired` → 跳过；
  - 数据缺失（`unknown`）→ **跳过**（按决策 17 的降级，见 4.5）。
- 宴席邀请开关：`meal="morning"` 带 `invite_morning`、`meal="evening"` 带 `invite_evening`（就是卡片里那两个开关，`app.js:1247/1291`）。二者任一为真会让本次运行申请 `BBQ_INVITE` 用途（`services/runner.py:139-143`），花钻石仍需过「允许花钻石」总闸；总闸关着时任务层降级为免费档自己吃（`_eat_free`），不报错 —— **行为不变**。

### 4.4 停止

- 一键按钮运行中变「停止执行」（复用 `.task-stop` 样式；`syncExecButtons()` L1441 现在只遍历 `.task-exec, .task-auto-exec`，把 `#btn-run-manual` 一起纳入）。
- 点击 → `POST /api/stop {"run_id": state.batchRunId}`；卡片按钮的 `stopCurrentRun`（L1460）**不传 run_id**（语义仍是"停手上的这一件"）。
- 结果提示复用 `appendRun`：`⏹ 已请求中断：<reason>`／`⚠ 这一批已经结束了`。

### 4.5 宴席：登录时自动拉 + 缓存 + 边界失效

- **拉取时机**（新增，替代现在"展开卡片才拉"的 `app.js:1042-1044`）：
  - `loadTasks()` 完成后，若 `state.session.has_session` 且 `state.banquetMeals` 为空 → `refreshBanquetStatus()`；
  - 登录成功 / 切区成功后同样拉一次（换了角色区服，期号可能不同）。
- **缓存失效**：后端 `_meals_view` 每项新增 `expires_at`（毫秒，= 该顿 `state`/`edible`/`invitable` 下一个可能变化的时刻）。实现：候选 = `[各 interval 的 startDateTime/endDateTime 中 > now 的] + [下一个 06:00、下一个 06:10]`，取最小。
  前端 `state.banquetExpiresAt = min(各项 expires_at)`；`Date.now() >= expiresAt` ⇒ 作废重拉。
  ⇒ 这样既不会白发包（32007/32008 是风控可见流量），也不会拿过期窗口去判"能否吃"。
- **降级**（补拉失败/未登录）：**跳过宴席**，其余 5 个照跑，状态行写一句 `宴席状态未知，已跳过`。

### 4.6 捐献置灰

- `manualExecControls(task)`（`app.js:1390-1401`）：`button.disabled = !task.enabled || state.autoDone.has(task.name)`。
- 展开区加一行说明（复用 `.task-note`）：`本游戏日已捐献（明天 05:00 后恢复）`。
- 一键执行里跳过 `state.autoDone.has("alliance_donate")` 的项。
- 置灰生效时刻：执行成功后 `/api/status` 轮询带回 `auto_done`（`state.py:272` → `app.js:3312`），跑完 `setRunning(false)` 会重渲染（`app.js:2481`）。

### 4.7 注释修正

`collectTasks()`（`app.js:2180-2186`）那段"勾选这个标记现在**只对自动任务有意义**（主动任务没有复选框了）"必须改写：勾选对两个分组都有意义（自动 = 每日自动执行；主动 = 一键执行的候选集）。

---

## 五、后端改动

### 5.1 `services/task_spec.py`

- `TaskSpec` 加 `batch_eligible` / `batch_order`（见第三节）。
- `alliance_donate` 加 `once_per_day=True`（决策 18 的记账来源）。
  - 副作用检查：`ONCE_PER_DAY_TASKS` 是 `frozenset`，`tests/test_services_daily_state.py:93-105` 只有正向断言 + `daily_box`/`bbq_status` 两个反例 ⇒ 不会红。
  - `ONCE_PER_DAY_TRIGGERS`（"只在第一次登录工具时执行"）只作用于 `group="auto"`，`alliance_donate` 是 manual ⇒ 无影响。
  - ⚠️ 语义说明必须写进注释：对 manual 任务，`once_per_day` 表示"每日一次的本地记账（用于置灰与一键跳过）"，**不参与自动执行编排**。
- `describe_tasks()` 下发新字段。

### 5.2 `webapi/state.py`

- **`_on_step_finish`（L705）加 `result.ok` 门槛**：
  ```python
  if not dry_run and result.ok and task_spec.is_once_per_day(result.name):
  ```
  这是修正一个现存缺陷（失败也记账）。副作用：现有 6 个 `once_per_day` 任务失败后，当天会重跑一次 —— 是修正，不是回归。
- **`stop()` 加可选 `run_id`**（L398）：
  - `run_id is None` → 现有行为（中断当前批次，其余队列照跑）；
  - `run_id` = 当前正在跑的那批 → 走现有中断路径（`cancellation.request_cancel` + 标 `skipped`）；
  - `run_id` 还在 `self._queue` 里 → 从队列移除该批，返回 `{"stopped": True, "cancelled_queued": True, "message": "已撤销排队中的这一批。"}`；
  - 找不到（已跑完）→ `{"stopped": False, "message": "这一批已经结束了。"}`。
  - 注意：移除排队批次时也要把进度视图里属于该批的 `pending` 步骤标成 `skipped`（否则前端会看到永远 pending 的步骤）。

### 5.3 `webapi/app.py`

- `_route` 的 `payload_models`（L1215-1222）加 `"/api/stop": StopPayload`；`StopPayload` 字段全部可选（`run_id: str | None = None`）。
- 兼容性已验证：`parsed = json.loads(body) if body.strip() else {}`（L1251）⇒ 旧前端 POST 空 body 仍是 `{}`，不会 422。
- `stop_run(payload=None)` 透传 `payload.run_id`。
- `docs()`（L1184）里 `/api/stop` 的说明改成"协作式中断当前批次，或按 run_id 撤销指定批次"。

### 5.4 `tasks/bbq.py`

- `_meals_view`（L961-1048）每项新增 `expires_at`（毫秒）—— 供前端缓存失效。`window_text`/`start_text`/`end_text` 保持不动。
- 顺手校正 `services/banquet_state.py` docstring 里"06:00 换日"与实际常量 `BANQUET_RESET_HOUR=6 / MINUTE=10`（06:10）的不一致。

---

## 六、边界与风险

1. **宴席"是否吃过"只在工具自己吃成功时记账**：你在手机上手吃的，工具不知道 → 一键会再提交一次（服务端拒绝，日志多一条无效结果）。无解（32008 没有完成位），接受。
2. **一次提交 12 个步骤可能很久**（主线已排除，最坏是扫荡 + 荣耀之巅）。停止随时可用（决策 12/13）。
3. **一次提交里两个 `bbq_energy`**：`_on_step_start/_on_step_finish`（`state.py:664-699`）按"第一个 pending/正在跑的同名步骤"匹配，两次会分别命中；`app.js:3337-3339` 的 `find(s => s.name === "bbq_energy")` 只取第一项（早宴席）的 `data.meals` 就地更新 `state.banquetMeals` —— 两次的窗口数据来自同一份 32008，无实质差别。
4. **`#btn-run-manual` 不能用 `#btn-run`，勾选框不能用 `.task-exec`** —— 否则 `tools/verify_web_ui.py` 的既有判据会误红（事实 9）。
5. **宴席邀请与钻石总闸**：一键里带邀请开关时受总闸约束，总闸关则降级免费档（决策 15 已确认接受）。
6. **Tailwind**：`web/assets/input.css` 新增 `.manual-batch-bar` / `.manual-batch-actions` 后必须重建；⚠️ **注释里不要写 Tailwind 类名字面量**（content 扫描会把注释算进去，凭空生成死 CSS）。
7. **改 `web/` 后必须**跑 `tools/sync_android_assets.py`，且要**单独一条命令**（safe-delete 闸门按 turn 累计，跑过 pytest 同 turn 再跑会被拦）。

---

## 七、改动清单

| 文件 | 改动 |
|---|---|
| `services/task_spec.py` | `TaskSpec` 加 `batch_eligible`/`batch_order`；6 个任务登记；`bbq_energy.batch_order=-1`；`alliance_donate.once_per_day=True`；`describe_tasks` 下发新字段 |
| `webapi/state.py` | `_on_step_finish` 加 `result.ok` 门槛；`stop(run_id=None)` 支持精确撤批 |
| `webapi/app.py` | `StopPayload` + 路由表登记；`stop_run` 透传；docs 文案 |
| `tasks/bbq.py` | `_meals_view` 加 `expires_at` |
| `services/banquet_state.py` | 仅 docstring 校正（06:00 → 06:10） |
| `web/index.html` | `#panel-manual` 插入 `.manual-batch-bar` |
| `web/assets/app.js` | `manualCheckbox()`；`renderManualBatchStatus()`；`runManualBatch()`；`renderTask` 头部加勾选框；`renderTaskGroups`/`setRunning` 挂钩；`submitRun` 记 `run_id`；`syncExecButtons` 纳入一键按钮；`stopCurrentRun`/`runManualBatch` 的停止路径；宴席拉取时机改到登录/切区；宴席 `expires_at` 失效；`manualExecControls` 加置灰；`collectTasks` 注释改写 |
| `web/assets/input.css` | `.manual-batch-bar` / `.manual-batch-actions`（改完重建 tailwind） |
| `tools/verify_web_ui.py` | 更新 L10-11 描述；新增判据（见第八节） |
| 测试 | 新增：`batch_eligible` 一致性（必须 enabled + group=manual；`batch_order` 只允许出现在 `batch_eligible` 上）、`stop(run_id=)` 三种情形、`_on_step_finish` 失败不记账、`_meals_view` 的 `expires_at` |

## 八、验证与回归清单

1. `tools/verify_web_ui.py` 判据更新：
   - 主动任务里 `batch_eligible` 的卡片各有 **1 个**勾选框；`buy_energy`/`push_main_stage` **0 个**；
   - `#btn-run-manual` 存在，且未勾选时 `disabled`；
   - 勾 2 个任务后点一键，`/api/status` 步骤顺序 = 宴席（若可吃）+ `任务A, daily_box, 任务B, daily_box`；
   - 点「停止执行」后，剩余步骤状态为 `skipped`（不是 `fail`）。
2. `C:\Users\Anqi Liu\.workbuddy-ai\binaries\python\envs\cherrytale\Scripts\python.exe -m pytest -q`（全量；重点 `test_services_task_spec.py` / `test_services_runner.py` / `test_services_daily_state.py` / `test_webapi_app.py`）。
3. `npx tailwindcss -i ./web/assets/input.css -o ./web/assets/styles.css --minify`。
4. `py -3.14 -u tools/sync_android_assets.py`（**单独一条命令**）。
5. 真机/双端手测：登录后宴席状态自动出现（无需点刷新）；吃完早宴席后按钮置灰且一键跳过；捐献成功后置灰且一键跳过；一键跑 2 个任务 + 停止；一键批次排队时点停止只撤自己那一批。

## 九、明确不做（范围外）

- 不做主动任务的二次确认弹窗。
- 不做"扫荡次数是否用尽 / 市集是否买满"之类的本地业务判断。
- 不做勾选的自动清空。
- 不改宴席的窗口规则与补吃语义（只是改变"何时去问"）。
- 不把 `push_main_stage` / `buy_energy` 纳入一键执行（将来若要，只需在规格表登记 `batch_eligible=True`）。

---

## 十、实施记录（2026-10-16）

按第七节清单全部落地。全量测试通过（仅缺凭据的用例 skip），
`npx tailwindcss … --minify` 已重建、`tools/sync_android_assets.py` 已同步 12 项。

### 与方案的两处**新增**（都是为了可验证性/正确性）

1. **任务卡片加了 `data-task="<任务名>"`**（`app.js::renderTask` 的根 div）。
   以前只有内部按钮带 `data-task`，于是"某张卡里的控件"只能按位置去数
   （`querySelectorAll('.task-exec')[0]`）。而一键执行那几条校验判据必须点
   **指定任务**的勾选框，否则结果随机。现在 `verify_web_ui.py` 用
   `.task[data-task="X"] .task-actions--pick input` 精确定位。
2. **新增 `state.submittedRunId`**，**不是**复用 `state.lastRunId` ——
   后者是"结果流水已经渲染到哪一批"的去重游标（`renderRunResults`），
   语义完全不同；合并会让结果流水重复或漏行。

### 实施期暴露的四个坑（都有守卫了）

1. **`/api/docs` 直接 500**：`docs()` 的 HTML 是 **f-string**，正文里写
   `{"run_id": "..."}` 会被当成格式说明符（`ValueError: Invalid format specifier`）。
   正文里的花括号必须写成 `{{ }}`（CSS 段本来就是这么写的，接口说明段漏了）。
   守卫：`tests/test_webapi_app.py::TestReadOnlyEndpoints::test_docs_are_mounted_under_api`。
2. **测试夹具的 `runner.prepare` 替身必须提供 `close()`**。
   返回裸 `object()` 会让 `_execute_one` 的 `finally` 在 `context.close()` 处抛
   `AttributeError` —— 而 `finally` 里排在它后面的正是
   `cancellation.uninstall_token(token)`，于是**取消令牌泄漏**：本文件之后的
   所有用例都会莫名其妙地被"用户点了停止执行"打断。
   （实测症状：全量跑时 `test_material_stage` / `test_services_cancellation`
   三处失败、单独跑全绿 —— 因为新文件按字母序排在它们前面。）
3. **别在一键里复用 `state.lastRunId`**（见上面第 2 点）。
4. **`alliance_donate` 的置灰要等轮询回来**：`/api/status` 的 `auto_done`
   更新 `state.autoDone`，而重渲染发生在 `setRunning(false)` 那一下
   （`app.js` 的 `pollOnce`）。所以"跑完立刻变灰"是成立的，但**不是**点击后
   同步变灰 —— 别在别处再写一套乐观置灰。

### 顺带修正

- `services/banquet_state.py` 的模块 docstring 原写"06:00 换日"，与常量
  `BANQUET_RESET_HOUR=6 / MINUTE=10`（**06:10**）不符，已校正并注明 06:00–06:10
  是空档期、仍算前一宴席日。

### 复核要点（改这块时先看这几条）

- **成员/顺序只改规格表**：`batch_eligible` / `batch_order`，前端零特例。
- **一键必须一次提交**：拆成 N 批就再也停不掉后面几个（见第二节事实 2）。
- **宴席提交判据必须与卡片按钮一致**（`batchBanquetMeals` 抄的是
  `bbqMealPanel` 的 `ready` / `makeup` 口径），否则会出现"按钮灰着、一键却跑了"。
- **`expires_at` 由后端给**（`tasks/bbq.py::_expires_at_ms`），前端只做一次比较
  —— 前端不自己推窗口规则（`app.js` 顶部的注释早就写死了这条）。
- 改完 `web/` 记得重建 tailwind + `sync_android_assets.py`。
