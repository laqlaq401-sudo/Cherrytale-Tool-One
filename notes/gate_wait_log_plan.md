# 计划：去掉「正在等待其他请求释放运行闸门」这行日志

> 2026-10-06 · **已实施**（用户选定：方案 A + 门槛改 60 秒 + 一并删「等到了」）

## ✅ 最终实现（2026-10-06 15:2x）

用户决策：① 方案 A，但门槛从 30 秒改成 **60 秒**；② `✔ 等到了运行闸门` **一并删**。

`webapi/state.py::_acquire_gate_interruptible()`：

- 删掉 3 秒那行 `⏳ 正在等待其他请求释放运行闸门…` 与 10 秒续报；
- 删掉 `✔ 等到了运行闸门（共等待 N 秒）`；
- 删掉 `reported` 变量；
- **新增模块级常量** `_GATE_WAIT_LOG_EVERY_SECONDS: Final[float] = 60.0`
  （只影响日志，不影响等待本身）；
- 唯一残留日志：`waited >= next_report` 时写一行
  `⏳ 已等待运行闸门 N 秒，有其他请求（刷新信息面板等）正在占用，跑完就会自动继续`，
  之后每 60 秒续报；
- `_heartbeat(...)` 与 `while not token.requested` 结构**原样保留**。

### ★ 一个必须记住的坑：不能用 `waited % 60 < 0.2`

第一版写成 `waited % _GATE_WAIT_LOG_EVERY_SECONDS < 0.2`，**浮点累加会重复命中**：
`waited` 一路 `+= 0.2` 会落成 `180.00000000000003`，于是 180 秒那次
**同一个整点连续写两行**（实测 300 秒内多出 2 行，见
`tests/test_webapi_app.py::TestGateWaitIsQuiet::test_three_minutes_writes_three_lines`）。
改成显式阈值累加：

```python
next_report = _GATE_WAIT_LOG_EVERY_SECONDS
...
if waited >= next_report:
    next_report += _GATE_WAIT_LOG_EVERY_SECONDS
    self._log(...)
```

### 回归测试（新增，原先**没有**任何网）

`tests/test_webapi_app.py::TestGateWaitIsQuiet`，4 条，全部**不睡觉**
（patch `state.time.sleep` 成纯计数替身，计数到第 N 次就释放闸门）：

| 用例 | 断言 |
|---|---|
| `test_below_threshold_stays_silent` | 等 10 秒 → 日志 **0 行**，但心跳有、且含「运行闸门」 |
| `test_past_threshold_writes_exactly_one_line` | 等 80 秒 → **恰好 1 行**，且含 60 |
| `test_three_minutes_writes_three_lines` | 等 180 秒 → **恰好 3 行**（60/120/180），钉死"不重复" |
| `test_cancel_during_wait_is_observed` | 已喊停 → 立刻返回 `False`，不写日志、不打心跳 |

⚠️ 驱动技巧的语义陷阱：`fake_sleep` 在**第 ticks 次**放闸，而那一轮循环的
日志检查发生在放闸之前，所以"要落在 180 秒整点"必须给 `ticks=901` 而不是 900。
测试里 `_drive()` 的 docstring 已写明。

### 验证结果

- `pytest tests/test_webapi_app.py` → **48 passed**
- 全量 `pytest` → **1760 passed, 10 skipped, 0 failed**
- `py -3.14 -u tools/sync_android_assets.py` → 完成 12 项同步（已把新 `webapi/` 打进
  安卓 assets；安卓侧仍是旧 APK，需自行 `gradlew assembleDebug` 重封）

### 未做

- 未改 `webapi/app.py`：`_heartbeat` 保留，`note_work_progress` docstring 第 121-122 行
  「兜底：`state` 在 worker 等待闸门期间也会周期性调一次」**仍然成立**，无需改。

---

## 以下为改动前的调研记录（保留备查）

## 一、现状

出处唯一：`webapi/state.py::RunState._acquire_gate_interruptible()`（约 567-603 行）。

```python
waited = 0.0
reported = False
while not token.requested:
    if self._gate.acquire(blocking=False):
        if reported:
            self._log(f"✔ 等到了运行闸门（共等待 {waited:.0f} 秒），开始执行")
        _heartbeat("已获取运行闸门")
        return True
    time.sleep(0.2)
    waited += 0.2
    _heartbeat(f"正在等待运行闸门（已等 {waited:.0f} 秒）")
    if waited >= 3.0 and not reported:                 # ← 用户嫌吵的就是这行
        reported = True
        self._log("⏳ 正在等待其他请求释放运行闸门（已等 N 秒）……"
                  "只读请求（刷新信息面板等）跑完就会自动继续", level="WARNING")
    elif reported and waited % 10 < 0.2:               # ← 每 10 秒的续报
        self._log(f"⏳ 仍在等待运行闸门（已等 {waited:.0f} 秒）……", level="WARNING")
return False
```

### 两条通道，要分清楚

| 通道 | 内容 | 用户能看到吗 |
|---|---|---|
| `self._log(...)` → `LogStream` | 上面那两行 `⏳ …` | **能**，直接刷在日志面板（本例要删的就是它） |
| `_heartbeat(...)` → `note_work_progress()` → `_ACTIVITY["last_note"]` | `正在等待运行闸门（已等 N 秒）` | **看不到**（无日志输出）。它只做两件事：刷新看门狗 `last_progress`、把 `last_note` 留给**真正挂死时**的堆栈转储使用 |

⇒ 「删除日志输出」**不等于**「删除等待逻辑」。`_heartbeat` 必须留。

### 为什么当初会加这两行

`_acquire_gate_interruptible` 的 docstring（2026-10-06）写得很清楚：当时用户报
「点了执行卡片一动不动」——只读请求占着闸门 + Android 网络超时 30s×N，等待期间
**完全静默**，看门狗又只看 `inflight`（当时判据已改为 `inflight or run_active`），
于是现场一片空白。加这两行的目的是「让用户至少看到它在等什么」。

现在用户明确表示不需要，属于**产品取舍反转**，不是修 bug。

## 二、影响面（改动前必须确认）

1. **没有测试钉死这两行。** `grep 等待其他请求 / 运行闸门 / 仍在等待` 只命中
   `webapi/state.py` 自身；`tests/` 下唯一相关的是 `test_webapi_app.py:369` 的一句
   docstring（讲排队 ≠ 并发），不涉及日志文本。
   ⇒ 删掉不会红测试，但也意味着**没有回归网**，要靠下面第 3 步补。
2. `reported` 变量只在「要不要打印」上用到。删日志后它变成死变量，必须一并删掉，
   否则 lint / 阅读时是噪声。
3. `waited` 变量仍有两个用途，**不能删**：
   - `_heartbeat(f"正在等待运行闸门（已等 {waited:.0f} 秒）")`；
   - `waited % 10` 那个 10 秒续报（如果保留续报的话，见方案 B）。
4. `✔ 等到了运行闸门（共等待 N 秒）` 这行是**成对**的：删了「在等」，留着「等到了」
   会显得突兀（用户没看到等，却看到"等到了 12 秒"）。建议一并处理，见方案 A。
5. `webapi/app.py::note_work_progress` 的 docstring 第 121-122 行明确写了
   「兜底：`state` 在 worker 等待闸门期间也会周期性调一次」——如果保留 `_heartbeat`
   就无需改；若连带 `_heartbeat` 一起删，`run_active > 0` 仍能让看门狗看到这批运行，
   但 `last_note` 会失去"在等闸门"这个关键线索，挂死转储会变得含糊。**不推荐**。
6. **不进 DEBUG 级别**：即使降级为 `DEBUG` 也仍会进日志流
   （`logstream.LogStreamHandler` 级别是 `INFO`，`DEBUG` 会被产生端过滤掉——
   实测口径：根日志器级别未降到 DEBUG 时不会输出，看起来"干净"了，
   但只要有人调低了全局级别就会突然冒出来）。属于隐性复发风险，见方案 C。

## 三、可选方案

### 方案 A（推荐）：彻底删掉两行用户可见日志，只留心跳

改 `_acquire_gate_interruptible`：

- 删除 `reported` 变量与两处 `self._log(...)`（`⏳ 正在等待…` / `⏳ 仍在等待…`）。
- 保留 `while not token.requested` + `time.sleep(0.2)`（可中断语义不变）。
- 保留 `_heartbeat(f"正在等待运行闸门（已等 {waited:.0f} 秒）")`（看门狗的唯一线索）。
- 顺带删掉 `✔ 等到了运行闸门（共等待 N 秒）`（成对性，见影响面 4）。
- 同步更新 docstring 的「★ 2026-10-06：等待超过 3 秒就写一行进度」段落，
  改为说明「**只打心跳、不写日志**，因为静默等待是用户明确选择；
  现场信息由看门狗转储提供（`last_note`）」。

效果：等待期间日志面板**零输出**；真挂死时仍会看到
`【看门狗】有 1 个请求 / 1 个运行 N 秒无任何进展，疑似挂死，最后进展：正在等待运行闸门（已等 N 秒）`。

风险：回到「完全静默」的观感。缓解 = 方案 A′。

### 方案 A′（推荐 + 一个折中）

在方案 A 基础上**只保留 10 秒续报那一档，并提高门槛到 30 秒**：

- 3 秒那档（用户明确嫌吵的）删掉；
- 30 秒起每 30 秒一行 `⏳ 还在等运行闸门（已等 N 秒）……`。

理由：正常的只读请求占用（刷新信息面板）是几秒级，**永远触发不了 30 秒档**；
而真出问题时（Android 网络超时 30s×N）用户仍能看到一行，不至于一片空白。
如果用户要的是"一个字都不要"，就选方案 A。

### 方案 B：整体删除（不推荐）

连 `_heartbeat` 一起删。代价：`_ACQUITY["last_note"]` 丢失"在等闸门"这一关键线索，
挂死转储只剩「1 个运行 N 秒无进展」，排障时无法区分"卡在网络上"和"卡在闸门上"。
`webapi/app.py:121` 的 docstring 也要跟着改。**除非用户明确要求，否则不做。**

### 方案 C：降级到 DEBUG（不推荐）

把 `level="WARNING"` 改成 `"DEBUG"`。表面安静，但：
① 前端 `/api/logs` 仍可能收到（取决于根级别）；② 日后有人调全局级别就会复发；
③ 语义上"DEBUG"意味着"给开发者看"，而这句话本来就没什么开发价值。
不如直接删。

## 四、推荐落地清单

1. `webapi/state.py::_acquire_gate_interruptible`
   - 删 `reported`、删两处 `self._log(...)`、删 `✔ 等到了…`（或按 A′ 保留 30 秒档）；
   - 保留 `_heartbeat(...)` 与 `while not token.requested` 结构；
   - 改 docstring 对应段落。
2. `tests/test_webapi_app.py` 增一条回归（因为目前没有网）：
   - 造一个场景：先同步占住闸门（如起一个 `run_inline` 的慢只读请求），
     再让 worker 提交一批；断言等待 5 秒后日志流里**不出现** `⏳`，
     且运行仍能正常完成（`_wait_for_run_to_finish` 已存在可复用）。
   - 反过来再断言一次 `_heartbeat` 生效：`_ACTIVITY["last_note"]`
     含「运行闸门」。（直接读 `webapi.app._ACTIVITY` 即可。）
3. 跑测试：
   `C:\Users\Anqi Liu\.workbuddy-ai\binaries\python\envs\cherrytale\Scripts\python.exe -m pytest tests/test_webapi_app.py -q`
   （改动只碰 `webapi/`，但闸门相关断言在 test_webapi_app.py，全量跑一次也便宜。）
4. ★ 改完 `webapi/` 必须跑 `py -3.14 -u tools/sync_android_assets.py`
   （注意：若同一 turn 已跑过 pytest 触发 safe-delete 闸门，要单独开一条命令）。

## 五、待用户决定

- **A 还是 A′？**（"完全静默" vs "30 秒才出一行"）
- `✔ 等到了运行闸门（共等待 N 秒）` 是否一并删？（建议删）
- 是否需要第 2 步那条回归测试？（建议要，否则以后容易被人"顺手加回来"）
