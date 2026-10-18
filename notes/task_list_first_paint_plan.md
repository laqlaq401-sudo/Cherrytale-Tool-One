# 任务清单首屏「读取中」修复方案（2026-10-19）

> 状态：**已实施（P0-1 / P0-2 / P0-3 / P1 全部落地）**，验证结果见文末「六」。
> 契约已归档到 `notes/web_layer.md` §10.12。
> 症状：打开程序 →「自动任务」「主动任务」两栏只有一行「…读取中…」，卡片全空；
> 等一会儿才一次性全部出现。

## 一、根因

### 1.1 两栏的卡片只有一条渲染路径

- `web/index.html:152` / `:168` 是**写死**的占位文案（v1.0.0 就有，不是新增）。
- `#tasks-auto` / `#tasks-manual` 是空容器，内容全部由
  `renderTaskGroups()`（`app.js:1138`）一次性画出；
  两行状态文字也由它调 `refreshDerivedViews()` → `renderAutoStatus()` /
  `renderManualBatchStatus()` 重写。
- 所以在 `renderTaskGroups()` 执行前，界面上**只可能**剩那两行「读取中…」。

### 1.2 谁把首帧挡住了

`loadTasks()`（`app.js:4768-4865`）末尾的调用链：

```
reconcileSelection()          // 纯本地
await ensureActivityCache()   // 可能 1~2 次 run_inline（每周三 20:00 后 / 首次）
await ensureMaterialTree()    // 本机读配置
await ensureBanquetStatus()   // ★ 真实游戏服 32007→32008，独占运行闸门
renderTaskGroups()            // ← 首帧在这里
```

`ensureBanquetStatus()` 是 **2026-10-16**「宴席时段登录后自动读一次」时插进来的
（见 `notes/manual_batch_exec_plan.md` §4.5），位置在 `renderTaskGroups()` **之前**。

### 1.3 为什么每次启动都必然等

- `state.banquetMeals` 只存在内存（`app.js:125`），刷新页面即丢；
- `banquetCacheStale()`（`app.js:1294`）见空数组就返回 true；
- ⇒ **每次打开都必发一次 32007/32008**，没有"命中缓存直接过"的可能。

### 1.4 实测与放大器

- `tmp/webquiet.log`：`21:58:49 开始 bbq_status` → `21:58:51 下一个任务`
  ⇒ 这一趟 ≈ **2 秒**，正好是用户看到的停顿。
- 同日志 `21:58:39 → 21:58:42`：活动区域缓存过期那次 ≈ **3 秒**（每周才一次）。
- `common.js:174-206` 的 `call()` **没有超时**：游戏服慢/挂住时，
  「读取中」可以无限长；同时 `boot()` 是 `await loadTasks()` 之后才提交自动任务，
  这 2 秒也会把"进主界面自动执行"一起往后推。
- `webapi/app.py:924-959` + `webapi/state.py:520-569`：`/api/banquet/status`
  走 `run_inline`，和真实运行共用一把闸门（2026-10-18「只读让行」后运行中直接 409）。

### 1.5 结论

「读取中」不是文案改动，是**首帧被一次真实联网读宴席绑住了**。
v1.0.0 里也有 activity/material 两个 await，但通常命中缓存（几十毫秒），
所以看不出来；10-16 这次把一次 ~2 秒的网络往返塞进首帧路径，才变成肉眼可见。

## 二、修复方案（推荐：先画后补）

### P0-1 `loadTasks()` 先出清单

```js
reconcileSelection();
state.hydrating = true;        // 新增：只影响宴席卡文案
renderTaskGroups();            // 首帧：只依赖 /api/tasks（本机，~20ms）
await ensureActivityCache();   // 顺序保持不变（同一把闸门，不能并行）
await ensureMaterialTree();
await ensureBanquetStatus();
state.hydrating = false;
refreshHydratedCards();        // 只刷受影响的卡片 + 两行状态
```

- 保持三个 await 的**顺序**与闸门语义不变：它们都走 `run_inline`，
  并行发会互相 409（写优先），反而把候选读打坏。
- 不把三个 await 挪出 `loadTasks()`：`boot()` 紧接着要提交自动任务，
  读请求必须先于写请求完成，否则自动批会把它们全挡成 409。

### P0-2 新增 `refreshHydratedCards()`

```js
function refreshHydratedCards() {
  let rebuilt = false;
  for (const task of state.tasks) {
    if (task.hidden) continue;
    const needsOptions = (task.params || []).some((p) => p.options_source);
    if (needsOptions && !refreshCard(task.name)) rebuilt = true;
  }
  if (rebuilt) renderTaskGroups();   // 兜底：极端情况下整组重建一次
  else refreshDerivedViews();
}
```

- 只重建"参数里有 `options_source`"的卡片（活动区域/关卡、素材区域/关卡）。
- 宴席卡不在名单里：`refreshBanquetStatus()` 内部已经 `syncBbqPanel()` +
  `refreshDerivedViews()` 就地更新，不需要重建。
- 避免"整组重建两次"：`notes/exec_button_flicker_plan.md` 的教训（滚动跳动、按钮态闪回）。

### P0-3 宴席卡文案不撒谎

`bbqMealsHeadText()`（`app.js:1983`）现在只看 `state.banquetLoading`；
首帧时补水还没开始，会短暂显示「尚未读取宴席时段（点刷新）」。
加一条：`state.hydrating` 为真时也显示「正在读取…」。

### P1 只读请求加超时（建议同批做）

- 只给这三个调用加 `timeout`（`AbortController`，建议 8s），不做全局默认超时。
- 超时按"读失败"处理：活动/素材记 error；宴席置 `banquetPending` 交给
  `retryBanquetAfterRun()` 退避补读。
- 目的：游戏服抖动时，`loadTasks()` 不会无限挂起，自动任务也照常提交。

## 三、不做的事

1. **不并行**发 activity/material/banquet —— 同一把闸门会互相 409。
2. **不删**宴席自动读取（用户明确要"打开就知道能不能吃"）。
3. **不整组重渲染**两次（会闪、会跳滚动、会把「停止执行」打回「执行」）。
4. 不把两行「读取中…」从 HTML 删掉：首帧后会被真实状态覆盖，留着是无 JS 时的兜底。

## 四、验收

1. **Mock 后端**（临时放 `tmp/`，不入库）：
   - `/api/banquet/status` 延迟 2s、`/api/activity/preview` 延迟 1s；
   - `/api/tasks` 里 `default_checked=false`、`auto_done` 覆盖全部任务名
     ⇒ `runAutomation` 空转，绝不真发包；
   - 页面用 `index.html?flat=1&dev=1&api=http://127.0.0.1:<port>` 打开。
2. **CDP 计时**：每 50ms 采样 `#tasks-auto .task` 数量；
   - 断言 <300ms 出现卡片；
   - 2.5s 后断言活动/素材下拉已有候选，且卡片节点未被整批替换、`scrollTop` 不变。
3. `python tools/verify_web_ui.py` 回归（class 计数判据）。
4. 真机只在**演练模式**下看首屏（按项目规矩：直接开真服会跑自动任务）。

## 五、影响面

- 只改 `web/assets/app.js` 三处；后端零改动。
- 首帧：2~5s → ~20ms；自动任务提交时间不变（仍等三个只读完成）。
- 回滚：还原 `loadTasks()` 中 `renderTaskGroups()` 的位置与新增函数即可。

## 六、实施结果（2026-10-19 凌晨）

实际改动（比计划多了两处小项）：

| 文件 | 改动 |
|---|---|
| `web/assets/app.js` | ① `loadTasks()`：`reconcileSelection()` 后立刻 `renderTaskGroups()`，三个 `ensure*` 顺序 await（单个失败不互相带倒），`finally` 里清 `state.hydrating` 并调 `refreshHydratedCards()`；② 新增 `refreshHydratedCards()`（只刷带 `options_source` 的卡片 + `syncBbqPanel` + 派生视图，找不到卡片才整组重建兜底）；③ `bbqMealsHeadText()` 补 `hydrating` 分支；④ `renderCandidateParam()` 补水期显示「（正在读取…）」；⑤ `READ_TIMEOUT_MS = 8000` + `isTimeout()` + `refreshBanquetStatus` 超时按欠读处理并记 `banquetPendingReason`（timeout/busy 分开文案）；⑥ `state` 新增 `hydrating` / `banquetPendingReason`。 |
| `web/assets/common.js` | `call()` 新增**可选** `timeout`（默认 0 = 不超时，行为与旧版完全一致）；超时抛 `error.timeout = true`、`status = 0`。 |
| `web/index.html` | 资源版本号 → `?v=20261019_1`。 |
| 安卓侧 | `tools/sync_android_assets.py` 已前台跑完，`sha256sum` 三对文件一致。 |

验证（`tmp/verify_first_paint.py`，mock 后端 + CDP，不入库）：

- 常规：活动延迟 1.2s / 素材 0.6s / 宴席 2.0s → **卡片 294ms 出现**（此前要等全部读完），
  此时下拉 =「（正在读取…）」1 项、宴席卡 =「正在读取…」；
  补水完成后下拉 3 项、宴席卡回到「宴席日 …」真实状态，未整组重建。
- 超时（`HANG_BANQUET=1`，宴席延迟 20s > 8s 上限）：13s 后宴席卡显示
  「读取超时，稍后自动补读」，页面不再无限转圈。
- `node --check` 两个 JS 通过；`tests/test_webapi_app.py` + `test_services_task_spec.py`
  109 passed。

遗留（可选，未做）：超时后文案仍是"自动补读"而非人工入口——已有「刷新」按钮兜底；
`boot()` 仍等补水完成才提交自动任务（保持闸门"读先于写"），极端网络下自动任务
最多晚 8 秒提交。
