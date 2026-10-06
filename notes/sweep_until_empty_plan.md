# 扫荡改造计划：档位改为「1次 / 5次 / 扫荡到清空体力」

> 状态：**待评审，未写任何代码**。评审通过后再进入实现。
> 关联：`tasks/sweep.py`、`tasks/material_stage.py`、`services/task_spec.py`、
> `models/activity_stage.py`、`config.py`、`web/assets/app.js`。
> 用户已拍板的两点：① 清空体力**只作用于指定关卡**（未指定则报错）；
> ② 素材关卡**也要加类似档位**，且因素材**不消耗体力**，档位名叫
> **「扫荡到次数耗尽」**（与活动关的「扫荡到清空体力」区分）。

---

## 0. 结论先行

| 项 | 现状 | 改造后 |
|---|---|---|
| UI 档位 | `0 / 1 / 5 / 10`（`services/task_spec.py::TIMES_PARAM`，活动+素材共用） | `0 / 1 / 5 / 扫荡到清空体力`（活动）/ `0 / 1 / 5 / 扫荡到次数耗尽`（素材） |
| 活动关"清空体力" | 无 | 反复发 **5 次**扫荡，直到体力不足以再扫 |
| 素材关"次数耗尽" | 无 | 反复发 **5 次**扫荡，直到服务端回 `TimesIsFull(-7)` |
| 券是否挂钩 | 活动关**已挂钩**（`useSweepTicket=1`）；素材关**不挂钩**（=0，符合实测） | 保持，并增加"扫后核对券是否真的扣了" |

核心动机：满足"远超 10 次"的扫荡需求，同时**不用一次性发大数**（一次 5 次、
多轮循环），降低风控暴露面。

---

## 0.5 ★ 抓包复核：「扫荡 N 次」是一个包还是 N 个包？

**结论：一次「扫荡 N 次」= 一个 `11009`，字段 `times=N`。要扫更多就是重复点按钮，
每次一个包（形状完全相同）。**

复核命令与结果（`tools/extract_saz_posts.py`，venv 见文末）：

```bash
python tools/extract_saz_posts.py --match 降临      # 整份 29MB 只有 2 个 11009
python tools/extract_saz_posts.py --match 降临 --decode raw/08   # times=1
python tools/extract_saz_posts.py --match 降临 --decode raw/10   # times=4
python tools/extract_saz_posts.py --match 素材      # 3 个 11009，全部 times=5
```

| 抓包 | 会话 | `#1 sectionID` | `#2 times` | `#3 useSweepTicket` | 说明 |
|---|---|---|---|---|---|
| 降临 | raw/08 | 129110539 | **1** | 1 | 点「1 次」→ **一个包** |
| 降临 | raw/10 | 129110539 | **4** | 1 | 点「5 次」→ **一个包**，且被体力削成 4 |
| 素材 | raw/08 | 129010056 | **5** | 0 | 点「5 次」→ 一个包 |
| 素材 | raw/10 | 129010056 | **5** | 0 | **再点一次**「5 次」→ 又一个包 |
| 素材 | raw/12 | 129010056 | **5** | 0 | **再点一次** → 第三个包 |

两条独立结论：

1. **「N 次」不是一个包发 N，而是一个包带 `times=N`。** 降临 raw/10 的 `times=4`
   证明：客户端算的是 `times = min(玩家选的档位, 体力 // energyCost)`
   —— 所以**非档位值（如 4）也会被客户端发出**，属正常行为。
2. **素材抓包的三个 `times=5` 包，就是"玩家反复点扫荡 5 次"的真实形态**
   （同 `sectionID`、同 `times`、形状完全一致，中间**不重发** 11003）。
   这正是我们要做的"反复进行 5 次扫荡"循环——**已有真实抓包作为范本，可逐字节对齐**。

⇒ 因此本计划的"清空体力 / 次数耗尽"循环，**每个轮次就是一发 `times=5` 的 11009**，
与游戏行为严格一致；绝不能把 50 次塞进一个包。

---

## 0.6 最终实现决策（2026-10-04，**已落地**）

| 决策点 | 结论 |
|---|---|
| 每轮次数 | **固定 5**（一个 `times=5` 的包），**不按体力预判** |
| 停止条件 | **完全靠服务端错误码**（`-2` 体力不足 / `-3` 券不足 / `-7` 次数已满 …）命中即停、如实记日志 |
| 起始体力 | **不读取**（不新增任何查询包）；第一轮直接发 5，体力不够就由服务端拒绝 |
| 作用范围 | 活动关**必须** `--section`（只对单关清空）；素材关天然单关 |
| 轮间隔 | 随机 **1~2 秒**（`config.sweep_interval()`；`CHERRYTALE_SWEEP_INTERVAL` / `_MAX` 可调） |
| 安全上限 | `config.SWEEP_UNTIL_EMPTY_MAX_TIMES` = **200**（= 2000 / 10） |
| 扫荡券 | 保留"扫前 2007 读券、0 张不发"；素材关不用券（`useSweepTicket=0`） |

## 0.7 ★ 修订（2026-10-05，**已落地**）：每轮按体力削峰

**为什么改**：上一版"固定发 5、不预判体力"在**体力不足 5 次**时会发出注定被拒的包
（体力只够 3 次仍发 5 → 服务端回 `-2 NoEnergy`）。这与实测客户端不符 ——
客户端是 `times = min(档位, 体力 // energyCost)` **每次现算**（抓包 raw/08 点 1 次、
raw/10 因体力只剩 160 而把 5 削成 4，见 `notes/sweep_capture.md` §五）。

| 决策点 | 旧（0.6） | 新（0.7） |
|---|---|---|
| 每轮次数 | 固定 5 | `min(5, 当前体力 // energyCost)`，**每轮现算** |
| 启动体力来源 | 不读 | **复用已有的只读 2007**（同一条响应里就有体力，零新增发包） |
| 逐轮体力来源 | 不用 | 每轮成功后用 `11010` 回传的余额**滚动覆盖** |
| 本地收尾 | 无 | 算出 `round_times <= 0` 时**不发包**、直接收尾（不再发注定被拒的包） |
| 停止条件 | 完全靠服务端 | 服务端仍是权威；本地只多一层"明知不够就不发" |
| 素材关 | 同规则 | **不改**（素材关不耗体力、靠每日次数，且读不到剩余次数 → 无削峰信息源） |

**落点文件**：`tasks/sweep.py`（改动主体）、`tests/test_sweep_task.py`。
**未改**：`tasks/material_stage.py`、`models/*`、`config.py`、`services/*`、`web/*`。

**新建/改写的行为契约**：
- 首轮：`times = min(5, 背包体力 // energyCost)`；
- 之后每轮：`times = min(5, 上一轮响应的 energy_left // energyCost)`；
- `times <= 0` → 本地收尾，**不再发 11009**；
- 任务成功判据增加"最后一轮业务成功"（撞安全上限 / 本地收尾都算成功）。

**落点文件**：`config.py`、`models/activity_stage.py`、`services/task_spec.py`、
`tasks/sweep.py`、`tasks/material_stage.py`、`web/assets/app.js`、`main.py`。

---

## 1. 现状（已逐处核对代码，不是推测）

- **UI 档位**：`TIMES_PARAM.choices = (0, 1, 5, 10)`（`services/task_spec.py:408`），
  被 `sweep_activity`（`:794`）和 `sweep_material`（`:828`）**共用**。
  `0` 是安全默认值（只读）。
- **后端闸门链**：`tasks/sweep.py::_plan_times` →
  `normalize_sweep_times()`（向下归档到 `{1,5,10}`）→ `config.SWEEP_MAX_TIMES`（=10 硬上限）
  → 剩余体力削峰 → 扫荡券削峰。取最小。
- **"次数"语义**：是**每个关卡**的次数，不是总次数。一次运行会对区域内
  **每个**候选关卡各发一发 `11009`（`times ≤ 10`）。间隔 `SWEEP_INTERVAL_SECONDS=1.0`。
- **哨兵陷阱**：`normalize_sweep_times(-1)` 现在返回 `(0, "")` —— 即"只读"。
  所以 `-1` 若直接喂进去会被**静默当成"不扫"**，必须在最前面拦截。
  `_dry_run()` 同样调用了 `normalize_sweep_times(self.requested_times)`，要一并处理。
- **素材关**：单关卡任务（未指定则自动取该区最高难度），`self.times = max(0, int(times or 0))`
  —— `-1` 会被 `max(0,-1)` 变 0，同样要提前拦截。消耗**每日次数**、不消耗体力，
  `useSweepTicket=0`；`11009` 必须 `include_defaults=True`（10-04 事故）。

---

## 2. 设计

### 2.1 哨兵值

- 新增 `SWEEP_UNTIL_EMPTY: Final[int] = -1`（放 `models/activity_stage.py`）。
- **不放进** `SWEEP_TIME_CHOICES`：该元组保持 `(1,5,10)`，语义是"线上真正会发出的
  times 取值"。哨兵是 UI 概念，绝不直接上网。
  （这样 `tests/test_capture_1003_regression.py:107` 的 `req.times in SWEEP_TIME_CHOICES`
  断言仍然成立，且能继续兜住"哨兵被误发"。）
- 处理顺序：在 `__init__`/`execute` 最前面判断哨兵，**绝不喂给** `normalize_sweep_times`。

### 2.2 活动关：清空体力循环（`tasks/sweep.py` 新增 `_sweep_until_empty`）

```
前置：必须先有 --section（未指定 → 直接失败，文案说明"清空体力需指定关卡"）
起步：读一次 2008，拿【券数】与【体力】（见 2.3）

每轮：
    floor = 体力 // energy_cost
    n = min(5, floor)                  # 每轮最多 5 次，与"反复进行 5 次扫荡"一致
    if n <= 0: 停（体力耗尽）
    if 券数 < n: n = 用 normalize_sweep_times(券数) 向下归档   # 券不足
    if n <= 0: 停（券耗尽）
    发 11009(times=n, useSweepTicket=1)
    用 res.energy_left / res.sweep_ticket_left 滚动更新 体力 / 券
    raise_if_cancelled()               # 每轮都是可中止点
    sleep(SWEEP_INTERVAL_SECONDS)
```

停止条件：体力不足 / 券不足 / `errorCode ∈ {-2 NoEnergy, -3 NoTicket, -7 TimesIsFull, -8 DailyClose}`
/ 用户取消 / 触达安全上限。
安全上限：新增 `config.SWEEP_UNTIL_EMPTY_MAX_ROUNDS`（建议默认 50 轮 = 最多 250 次），
防止异常情况下无限循环。

> 关键：**每轮都是一发独立 `times=5` 的 11009**，而不是一发 `times=50`。
> 这正是"降低风控暴露"的落点，且**已被素材抓包逐字节验证**（见 §0.5：三个
> `times=5` 的包 = 玩家连点三次"扫荡 5 次"）。
>
> 收尾轮：`n = min(5, floor)` 在 `floor < 5` 时取 `floor`（例如只剩 3 次的体力就发
> `times=3`）—— 这也与真实客户端一致：降临 raw/10 里客户端就把玩家点的 5 削成了 4。
> 因此循环能自然把体力排干到"不足以再扫 1 次"为止，无需额外的特殊收尾逻辑。

### 2.3 起始体力来源（**待确认，见 §4**）

目前任务只在扫荡响应里"顺带"学体力，清空模式第一轮就需要起始体力。两条路：

- **首选**：复用已经拉过的 `2008 GetAllItemRes`，读 `ENERGY_ITEM_ID(200000002)`
  （`GetAllItemRes.amount_of`）——**零额外包**。需先确认 2008 是否含体力。
- **备选**：第一轮按 `times=5` 发，靠响应纠正（代价：可能多扫一次）。

### 2.4 素材关：扫荡到次数耗尽（`tasks/material_stage.py`）

- 档位名用 **「扫荡到次数耗尽」**（素材不消耗体力，用"清空体力"会误导）。
- 语义：反复发 `11009(times=5, useSweepTicket=0)`，直到服务端回 `errorCode=-7`。
- `-7` 视为**正常结束**（`ok=True`，"次数已耗尽"），不是失败。
- 起始剩余次数本地拿不到（素材流程不发 11003），所以 `-7` 是唯一可靠停止信号；
  另加最大轮数上限兜底。
- 每次仍必须 `include_defaults=True`。

### 2.5 UI 与参数

- `services/task_spec.py`：档位改为 `(0, 1, 5, SWEEP_UNTIL_EMPTY)`，文案更新。
  **必须拆成两个参数**：`ACTIVITY_TIMES_PARAM` / `MATERIAL_TIMES_PARAM` ——
  同一个哨兵 `-1` 在两处的显示文案不同：
  活动 = **「扫荡到清空体力」**、素材 = **「扫荡到次数耗尽」**，共用会把文案说错。
- `web/assets/app.js::choiceOptionText`：按任务区分 `-1` 的文案
  （活动「扫荡到清空体力」/ 素材「扫荡到次数耗尽」）；
  选中该档但未选关卡时给出提示（活动关）。
- `main.py`：`--times` 帮助文本补充 `-1 = 扫荡到清空体力（需配合 --section）`。

### 2.6 测试

- `tests/test_services_task_spec.py`：档位一致性断言同步（含哨兵）。
- `tests/test_sweep_task.py`：新增用例 —— 多轮清空 / 券先耗尽即停 / 未指定关卡报错 /
  取消中断 / 安全上限生效。
- `tests/test_activity_stage.py`：新增 `SWEEP_UNTIL_EMPTY == -1` 断言，
  并断言 `-1 not in SWEEP_TIME_CHOICES`。
- `tests/test_capture_1003_regression.py`：保持不变（哨兵不入元组，天然被兜住）。

---

## 3. 扫荡券检查（你问的第二点）

### 3.1 结论：**目前是挂钩的**

| 关卡类型 | `useSweepTicket` | 是否查券 | 每扫 1 次扣券 | 依据 |
|---|---|---|---|---|
| 活动/降临关 | **1** | 扫前用只读 `2007` 数券，0 张**一个 11009 都不发** | **1 张** | `config.SWEEP_USE_TICKET_VALUE=1`；抓包 893→889（times=4） |
| 素材/元素关 | **0** | 不查 | 0（`costList` 为空） | `MATERIAL_SWEEP_USE_TICKET_VALUE=0`；10-03 素材抓包 |

即：**活动关扫荡确实与扫荡券挂钩，不存在"只扫不扣券"的现行路径**；
素材关不扣券是真实客户端行为，不是漏洞。

### 3.2 "扫了但没扣券"是否有风控风险

券扣不扣由**服务端**按 `useSweepTicket` 决定，客户端说了不算。所以风险场景是：
对**活动关**发 `useSweepTicket=0`（= 请求"免费扫"）时——

- 若服务端照常结算且**不扣券** → 资源产出与消耗不成比例，属于风控最敏感的
  异常模式之一（刷资源）→ **有风险**；
- 若服务端改为扣钻石或直接拒绝 → 表现为 `errorCode` 失败（`-4 NoDreamStone` / `-3`）。

**现状代码不会走到这条路径**（活动关恒发 1）。

### 3.3 加固建议

1. **扫后核对**：校验 `costList` 里券余量 == 上一轮券数 − times。若服务端回传"券没减"，
   告警并**停止**——这是"没扣券"唯一可检测的信号。
2. 保留"先读后发"的券闸门；清空模式下券不足即停（不要在券为 0 时反复试探）。
3. 真机抽样：定期核对券的实际消耗。

---

## 4. 待你拍板 / 待确认

1. **起始体力来源**：2008 是否含体力道具？含 → 零额外包直接读；不含 → 第一轮发 5 次靠响应纠正。
2. ~~参数是否拆分~~ **已定：必须拆**（活动「扫荡到清空体力」/ 素材「扫荡到次数耗尽」文案不同）。
3. **安全上限默认值**：`SWEEP_UNTIL_EMPTY_MAX_ROUNDS` 取 50 轮（=250 次）是否合适。
4. **是否存在每日扫荡总次数上限**（`TimesIsFull=-7` 的触发条件）——影响清空模式能否跑满体力。

---

## 5. 落地顺序（评审通过后）

1. `models/activity_stage.py`：加哨兵常量 + 断言。
2. `services/task_spec.py`：档位与文案（是否拆分参数）。
3. `tasks/sweep.py`：拦截哨兵 + `_sweep_until_empty` 循环。
4. `tasks/material_stage.py`：拦截哨兵 + "扫到 -7" 循环。
5. `config.py`：安全上限常量。
6. `web/assets/app.js` + `main.py`：文案。
7. 测试补齐 → 真机验证（先 `--dry-run`，再小号单关跑 1 轮，再放开）。

---

## 6. 附：本机跑抓包工具的可用方式（踩坑记录）

`.venv/Scripts/python.exe` 在本机跑不起来（报 `failed to open .venv\pyvenv.cfg`，
疑似沙箱读取问题）。可用「基础解释器 + venv 的 site-packages」绕过：

```bash
cd "<项目根目录>"
export PYTHONPATH="<项目根目录>/.venv/Lib/site-packages"
export PYTHONIOENCODING=utf-8
"$LOCALAPPDATA/Python/pythoncore-3.14-64/python.exe" \
  tools/extract_saz_posts.py --match 降临 --decode raw/10
```
