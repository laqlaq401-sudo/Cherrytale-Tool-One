# 日志噪音两处：团体矿「无法占坑」与宴席反复提示 —— 根因与修法

> 2026-10-18 · **仅计划，未改动代码**
> 用户问：① 报了 5 个「无法占坑」是日志多报还是执行逻辑有问题？② 为什么宴席提示每 5 秒反复弹？

---

## 〇、结论速览

| 问题 | 结论 |
|---|---|
| 团体矿"无法占坑 5 个" | **执行逻辑没有问题**，是**日志口径错误**：把"5 个候选**槽位**被跳过"说成了"5 个坑无法占"，与"占坑 3 次（次数上限 4）"并列，看起来像做了 8 件事、超了上限。 |
| 宴席每 5 秒弹一次 | **纯前端日志/重试 bug**：补读失败时先把"欠一次"标记清掉，导致"只提示一次"的判据永远成立；同时重试没有退避或上限。 |

两处都**不涉及协议、不涉及任务执行安全性**。

---

## 一、团体矿：占坑逻辑是对的，报告说错了话

### 1.1 两个数字不是同一件事

| 概念 | 协议字段 | 本次日志 | 说明 |
|---|---|---|---|
| 每日占坑**次数**上限 | `AllianceNewPitRes.teamLimit` | 4（抓包实证；`MAX_TEAM_PITS_PER_DAY=4` 兜底） | 公會等级 1~10 对应 1~4 次，满级也只有 4 次 |
| 矿点 | `teamList[]`（`AllianceTeamPitClass`） | 至少 2 个（209086970 / 209086969） | 一个矿点 = 一组槽位 |
| **槽位** | `pit.detaliClassList[]` | 5 个空槽挑不出角色 | 每个槽位要求**指定角色 + 最低等级** |

日志原文：

```
占坑 3 次；无法占坑 5 个（名册里没有这个角色 ×5；例：矿点 209086970 槽位 0…；
矿点 209086970 槽位 1…；矿点 209086969 槽位 0…（另 2 个见开发者日志））
```

- `占坑 3 次` = 消耗 3 次额度（今日 3/4 或 3/3，取决于之前是否已占）；
- `无法占坑 5 个` = **5 个候选槽位**在"名册匹配"这一步被**跳过、未发包**（`tasks/alliance_mining.py:1098-1120` 的 `continue`）；
- 3 + 5 **不能相加**：一个是"次数"，一个是"槽位"。

### 1.2 为什么确定没发无效包

`unfillable` 分支只有一句 `continue`，`_join_slot`（真正发包 21043）在它之后：

```python
if not candidates_roles:
    unfillable.append(self._unfillable_reason(...))
    continue                      # ← 到此为止，没有任何请求
ok, text, code = self._join_slot(game, pit.sid, slot_index, role_sid)
```

`data` 里 `join_failed`（真正被服务端拒绝的）本次为空，也印证了"5 个是本地筛选，不是服务端拒绝"。

### 1.3 报告为什么读起来像出错

1. **术语错位**：文案写"无法占坑 N 个"，但单位是**槽位**；用户心智里的"坑位"是次数（上限 4）。
2. **明细过多**：`_aggregate` 保留 3 条"矿点 + 槽位 + 需要某角色 ≥N 级"（`_MAX_LISTED_DETAILS=3`）。
   用户既不能现场练角色、也不能换坑，这 3 条只制造焦虑。
3. **缺额度上下文**：没写"今日 X/4"，用户无法判断 3 次是正常收工还是被截断。

### 1.4 修法（只改文案与信息层级，不动筛选逻辑）

| 改动 | 现状 | 改为 |
|---|---|---|
| 计数口径 | `无法占坑 5 个` | `跳过 5 个候选槽位（…）` |
| 额度上下文 | 无 | **不加分母**（用户明确：并非所有账号的上限都是 4，写 `3/4` 反而会误导） |
| 明细 | 运行视图 3 条 + 折叠 | **全部进 `logger.info`**，运行视图一个明细都不留 |
| 用语 | "坑位/槽位"混用 | 用户可见文案统一用**槽位**；"矿点"不再被叫作"坑位" |

预期效果（按本次日志）：

```
团体矿：占坑 3 次；跳过 5 个候选槽位（名册里没有这个角色 ×5）
```

一行读完，不再像失败，也不会出现一个可能错误的分母。

### 1.5 不建议改的地方

- 不要"按矿点去重"把 5 条压成 2 条 —— 明细进 DEBUG 后，运行视图只留计数，就已经没有噪音了；
- 不要给"名册里没有这个角色"自动降级去占其它坑 —— 角色条件是硬性的，服务端是最终裁判；
- 不要因为用户提"最多 4 个"就把 `MAX_TEAM_PITS_PER_DAY` 改成"同时槽位数" —— 它是每日**次数**，改错会重演 2026-10-05 那次的"连吃 15 个 -8"。

---

## 二、宴席提示：不是"任务在跑"，是运行刚结束时的只读快照占了闸门

### 2.1 时间线还原（按用户日志）

```
21:45:04  最后一个任务（日常/周常宝箱）完成 ← 正式批次结束
21:45:04  pollOnce 看到 running: true → false：
          ① loadPlayer({full})      ← 没 await，发起 /api/player
          ② refreshMainStageStatus() ← 另一个 run_inline
          ③ retryBanquetAfterRun()   ← 第 1 条 ⏳
21:45:09  第 2 条 ⏳（5 秒节流到点）
21:45:14  第 3 条 ⏳
21:45:19  第 4 条 ⏳
21:45:24  第 5 条 ⏳
（约 21:45:25 之后 /api/player 跑完，宴席补读成功 —— 但不再有日志行）
```

`/api/player` 内部**串行**跑 5~6 个只读任务（`webapi/app.py:809-826`：
wallet / roles / arena_status / wudou_status / yimo_status，full 时再加 top_pvp_status），
实测这一串就是 **20 秒级**；它们与 `/api/banquet/status` 抢**同一把全局闸门**
（`webapi/state.py:520-553` → `run_inline`）。

### 2.2 根因（三层，缺一不可）

**① 提示"只出现一次"的判据失效**

`web/assets/app.js:2176-2183`：

```js
const firstNotice = !state.banquetPending;   // ← 靠 pending 判"是不是第一次"
state.banquetPending = true;
...
if (firstNotice) appendRun("⏳", "任务运行中，……");
```

而 `retryBanquetAfterRun()`（`app.js:2240-2251`）在**发请求之前**就把它清了：

```js
state.banquetPending = false;      // ← 清掉
state.banquetRetryAt = Date.now() + 5000;
ensureBanquetStatus();             // ← 再请求 → 又 409
```

于是每一轮都是"第一次" → 每 5 秒写一条。

**② `state.running` 看不到 `run_inline`**

前端的 `state.running` 只来自 `/api/status`（正式批次）；`run_inline`（信息面板、宴席、
主线状态）**刻意不写运行状态**（`webapi/state.py:534-536` 的注释）。
所以"闸门被只读快照占着"在前端看来等于"空闲" → 补读照发 → 必 409。

**③ 补读与快照并发，且没有退避**

`pollOnce`（`app.js:4523-4537`）：

```js
loadPlayer({ full: touchedTopPvp });   // ← 没有 await
refreshMainStageStatus();
...
retryBanquetAfterRun();                // ← 与上面两个 run_inline 同时抢闸门
```

`retryBanquetAfterRun` 的文档写着"只补一次、且至少隔 5 秒"，但实现是**每 5 秒无限重试**（注释与代码不符）。

### 2.3 修法

#### P0（前端，必做，改动集中在 3 个函数）

1. **`pending` 只在成功时清除**：
   - `retryBanquetAfterRun()` 不再先清 `banquetPending`；
   - 只有 `refreshBanquetStatus()` 成功（`applyBanquetMeals` 之后）才清。
2. **自动获取完全不写日志**（用户要求）：删掉那条 `⏳ 任务运行中，宴席时段会在这次运行结束后自动读取。`，
   409 只更新卡片状态与内部记账；手动点「刷新」时的**真失败**（网络/令牌）仍如实报 ✘。
3. **指数退避 + 上限**：`5s → 15s → 45s → 60s`（上限），最多 4 次；成功后重置。
   （`banquetRetryAt` 继续用，另加 `banquetRetryDelay` / `banquetRetryCount`。）
4. 给 `refreshBanquetStatus(button, { silent = true })` 加参数：自动调用默认静默，
   只有手动「刷新」传 `silent: false`。

#### P1（前端，本次一并做，消除"必然相撞"）

把宴席补读挂到只读快照**与主线状态都完成之后**，而不是与它们并发：

```js
void loadPlayer({ full: touchedTopPvp })
  .finally(() => refreshMainStageStatus())   // 同样是 run_inline，必须串起来
  .finally(() => { state.banquetRetryAt = 0; retryBanquetAfterRun(); });
```

（两个函数内部都已 catch，不会把异常抛出来。）这样正常运行结束后闸门已空，
补读一次就成功，P0 的退避只作为兜底。

#### 不建议

- 不要给 `/api/banquet/status` 开后门绕过闸门 —— 32007 是风控可见流量，混着发比串行更糟；
- 不要把 `/api/player` 的 6 个只读任务改成"并发" —— 同一会话并发发包更容易被服务端判异常。

---

## 三、改动清单（预计）

| 文件 | 改动 | 规模 |
|---|---|---|
| `web/assets/app.js` | ① `retryBanquetAfterRun` 不清 pending ② 删掉 ⏳ 日志 + `silent` 参数 ③ 退避与上限 ④ `loadPlayer → refreshMainStageStatus → 补读` 串行 | ~25 行 |
| `web/assets/app.js` 常量区 | 退避基数/上限/次数常量 + `state.banquetRetryDelay` / `banquetRetryCount` | ~6 行 |
| `tasks/alliance_mining.py` | 报告文案：`跳过 N 个候选槽位`；明细全进 `logger.info`；`_aggregate` 只留分类计数 | ~15 行 |
| `tests/test_tasks_alliance_mining.py` | 改 `test_unfillable_summary_is_aggregated_not_dumped`：断言新文案、断言运行视图无明细、`data.unfillable` 仍 6 条 | ~8 行 |

**前端零新依赖、零 DOM 结构变化**，不涉及 Tailwind 类名。

---

## 四、验收

1. 单测：挖矿报告文案与 data 全量；
2. `node --check web/assets/app.js`；
3. 真机（宴会）：跑一次 6 个任务的批次 → 结束后**一条 ⏳ 都不出现**，且卡片时段自动刷新出来；
4. 真机（挖矿）：跑一次团队矿，报告应为
   `团体矿：占坑 N 次；跳过 M 个候选槽位（原因 ×M）`，运行视图无明细，
   `data.unfillable` 仍含全部明细；
5. 双端：`tools/sync_android_assets.py` 前台跑 + sha256 对比（`tasks/`、`web/` 都在同步目录）。

---

## 五、拍板结果（2026-10-18 21:53）

| # | 问题 | 结论 |
|---|---|---|
| 1 | 宴席是否做 P1？ | **做** —— 并且把 `refreshMainStageStatus()` 一起串进链里（它也是 run_inline） |
| 2 | 挖矿明细留不留示例？ | **一条都不留** —— 全进开发者日志；运行视图只留"跳过 N 个候选槽位 + 分类计数" |
| 3 | 报告是否显示 `N/4`？ | **不显示** —— 并非所有账号上限都是 4，分母会误导；"占坑 N 次"就够 |
| 4 | 宴席自动获取的日志？ | **一条都不写** —— 自动获取完全静默；只有手动「刷新」的真失败仍然报 ✘ |

---

## 六、实施记录（2026-10-18 22:0x）

### 6.1 挖矿（`tasks/alliance_mining.py`）

- 删除 `_MAX_LISTED_DETAILS`（不再列样例）；
- `_aggregate()` 只返回**分类计数**（`名册里没有这个角色 ×6`）；
- 汇总文案：`无法占坑 N 个（…明细…）` → **`跳过 N 个候选槽位（…计数…）`**；
- 明细（矿点 / 槽位 / 需求等级）全部改走 `logger.info`；
- `TaskResult.data["unfillable"]` 保留全量，前端/排障不受影响。

### 6.2 宴席（`web/assets/app.js`）

- 删除 `⏳ 任务运行中，宴席时段会在这次运行结束后自动读取。`（**一条都不再有**）；
- `retryBanquetAfterRun()` **不再提前清** `banquetPending`，只有成功才清
  （这正是"每 5 秒一条"的根因）；
- 指数退避：`5 → 15 → 45 → 60` 秒，最多 4 次，之后静默放弃；
- 新增 `state.snapshotBusyUntil`：信息面板快照在飞时补读直接跳过；
- `pollOnce` 改为串行链：`loadPlayer → refreshMainStageStatus → 补读`
  （三个 run_inline 抢同一把闸门，旧写法必然并发 409）；
- `refreshBanquetStatus(button, { silent = true })`：自动全静默，
  手动「刷新」传 `silent: false`，只有**真失败**（网络/令牌）才写 ✘。

### 6.3 验证

| 项 | 结果 |
|---|---|
| 全量测试 | **2007 passed / 16 skipped** |
| `node --check web/assets/app.js` | 通过 |
| 无头 Edge 加载页面 | `errs: []`（无未捕获 JS 错误）、自动任务卡 **11 张** |
| 运行日志 | **不含**旧文案（`noisyLine: false`） |
| 双端同步 | `alliance_mining.py` / `app.js` / `index.html` sha256 一致 |

⚠️ **验证副作用**：加载页面时前端按"启动触发"真的跑了一批自动任务
（`mail` / `friend_gift_claim` / `free_gift` / `alliance_sign_in`，均在服务被 kill 前开始；
`.daily_state.json` 未生成，说明没有 once_per_day 记账）。这些都是零成本领取类，
但属于"验证方式不当"——以后起页面验证必须先开演练模式，或断网/假接口。
