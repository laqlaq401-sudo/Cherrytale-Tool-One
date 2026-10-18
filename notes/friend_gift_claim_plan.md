# 好友赠礼「一键领取」独立自动任务 —— 实施方案

> 2026-10-18 · **已实施并真机验收**（15:05 两个真实账号路径均验证）
> 拍板结果：① **每次启动都领**（不登记 `once_per_day`）② 图标**新增 `gift_in`** ③ `daily_task_auto` 那步改名「好友赠礼（全送）」
> 起因：做日常任务「好友赠礼 3 次」时，抓包里同时录到了「一键领取好友赠礼」的操作。
> 用户要求：把「领取」单独做成一个自动任务，并加入前端。

---

## 〇、一句话结论

同一份抓包里，「一键全送」与「一键领取」**是同一个报文（10009）**，只差 `actionType`：

- `actionType=4` → 一键全送（已由 `tasks/daily_task_auto.py` 实现）
- `actionType=3` → **一键领取（本次要做的）**

模型层 `models/friend.py` 早已把枚举和报文建好（`OPERATION_GAIN_ALL_GIFT = 3`），
缺的只是「一个独立任务 + UI 规格 + 图标」这三样，**不需要新增任何协议字段**。

---

## 一、抓包实证（唯一事实来源）

证据文件：`captures/好友礼物 一键赠送 一键领取.saz`
核对工具：`tools/extract_saz_posts.py`、`tmp/deep_tree.py`（均只读）

| 会话 | 请求 | 响应 | 结论 |
|---|---|---|---|
| `raw/09` | 10009，子包 13 B：`#1 actionType=4`、`#2 playerID=-1`（uint64 全 1） | 10010，18 B：`errorCode=0`、`friendList` 空、`itemList` 1 组 | 一键**全送**（现有实现） |
| `raw/13` | 10009，子包 13 B：`#1 actionType=3`、`#2 playerID=-1` | 10010，143 B：`errorCode=0`、`friendList` **25 个 playerID**、`itemList` 1 组 | 一键**领取**（本次要做） |

`raw/13` 的道具明细（唯一的 `#3` 组）：

```
GetObjClass.itemList[0] = { itemSid=252721373, itemID=200000002, itemAmount=67 }
                                              └ 200000002 = 体力
```

两个直接推论：

1. **一键操作的目标就是 `playerID=-1`**，不需要逐个好友发；
2. 响应自带「处理了哪些好友」（`friendList`）与道具明细（`itemList`），
   **不需要先拉 10001 好友列表** —— 少一个包，就少一处失败面。

> ⚠️ `itemAmount=67` 是「本次获得」还是「领取后持有量」**尚未实证**
> （对照 `models/reward.py` 的口径表：10010 不在已实证名单里）。
> 因此用户可见文案**一律不报具体数量**，见 §三-3。

---

## 二、现状与缺口

| 位置 | 现状 |
|---|---|
| `models/friend.py:47-52` | `PACKET_PURPOSE[10009]` 只写「好友操作（一键全送 = actionType 4）」 |
| `models/friend.py:54-61` | 枚举齐全：`OPERATION_GAIN_ALL_GIFT=3` / `OPERATION_SEND_ALL_GIFT=4` / `ALL_GIFT_TARGET=-1` |
| `models/friend.py:80-90` | `FriendOperationRes{errorCode, friendList, itemList}` 已建模，`itemList` 是 `list[GetObjClass]` |
| `models/friend.py:1-8`（模块文档） | 明确写着「本任务只做**全送**；领取是用户自己的操作，不在此列」—— 本次正是要把这句改写 |
| `tasks/daily_task_auto.py:766-787` | `_step_friend_gift` 硬编码 `OPERATION_SEND_ALL_GIFT`，只负责日常任务 249000051「送 3 次」 |
| `services/task_spec.py:926+` | 任务清单唯一来源；auto 组默认勾选、`once_per_day` 控制"每游戏日只自动跑一次" |
| `web/assets/app.js:1310-1366` | `TASK_ICONS` 任务名→图标；未登记 = 该卡片不画图标 |
| `webapi/state.py:753-796` | 每个任务结束写状态；**只有 `ok=True` 且 `once_per_day` 才记账**，演练不记 |

缺口只有三处：**任务文件、注册表/规格表登记、前端图标**。

---

## 三、设计

### 1. 任务定义

| 项 | 值 |
|---|---|
| 任务名 | `friend_gift_claim` |
| 中文标题 | 好友赠礼一键领取 |
| 分组 | `auto`（纯领取、零消耗 → 默认勾选） |
| `requires_auth` | `True` |
| `spends_diamond` | `False`（本任务连花费路径都没有） |
| 参数 | **无**（不暴露任何档位/开关） |
| 默认勾选 | `True` |
| 每游戏日一次 | `True` |

### 2. 执行流程（单包，无状态）

```
execute()
 ├─ dry_run → 只打印 10009 的 describe() 与安全边界，一包不发
 └─ 否则：
      packet = FriendOperationPacket(actionType=3, playerID=-1)
      res    = guarded_send(..., include_defaults=True)   # 10009 → 10010
      ├─ errorCode == 0 且 friendList 非空 → ok=True，报「领到 N 位好友」
      ├─ errorCode == 0 且 friendList 为空 → ok=True，报「当前没有可领取的赠礼」
      └─ errorCode != 0                    → ok=True（软成功），报错误码
```

白名单（照 `tasks/top_pvp_status.py:38-39` 的先例，任务内定义**比模型层更窄**的集合）：

```
CLAIM_ALLOWED_PACKET_IDS = {"FriendOperationPacket": 10009}
```

即：本任务**结构上只能发 10009**，拉列表（10001）也不在其中。

### 3. 判定与文案（口径纪律）

| 场景 | ok | 用户可见文案 |
|---|---|---|
| 领到 | `True` | `好友赠礼一键领取成功（25 位好友 · 获得 1 件道具）` |
| 无待领 | `True` | `好友赠礼：当前没有可领取的赠礼` |
| 服务端非 0 | `True` | `好友赠礼：服务端返回 code=-X（按无可领取处理）` |
| 网络/鉴权异常 | `False` | 由基类把 `CherrytaleError` 转成失败结果 |

- 道具文案统一走 `models/reward.py::reward_phrase(res.itemList, scope=QuantityScope.UNKNOWN)`
  → 只会得到「获得 N 件道具」，**不出现具体数字**
  （`itemAmount` 口径未实证，见 §一 的警告）；
- 原始 `itemID/itemAmount` 只写 `logger.debug`，供排障，不进运行视图；
- `errorCode != 0` 判 `ok=True` 与 `top_pvp_box`（`tasks/top_pvp_box.py:107-117`）一致：
  「今天没东西可领」不是故障，不该在界面上打红叉。

### 4. 记账（每游戏日一次）

- `task_spec` 里登记 `once_per_day=True`；
- 服务端 `_on_step_finish` 在**成功**后记一笔，当天后续启动不再自动跑；
- 「执行」小按钮走手动路径，不受限制（想补领随时可点）。

**用户已拍板：不做每游戏日记账，每次启动都领**（好友可能在你开工具之后才送礼，
每天只自动跑一次会漏；领取零成本，重复发最多得到"没有可领取"）。
因此 `once_per_day=False`，服务端不写 `.daily_state.json`。

### 5. 与现有任务的关系

- `tasks/daily_task_auto.py` **不动**：它负责「送 3 次」补齐日常任务，与「收」互不替代；
- 不把领取塞进 `daily_task_auto`：用户要的是**独立卡片、可单独勾选、可单独执行**；
- 两者同一天各跑各的，没有共享状态，不存在抢资源。

---

## 四、改动清单

### 新增

| 文件 | 内容 |
|---|---|
| `tasks/friend_gift_claim.py` | `FriendGiftClaimTask`（`@register_task`，约 90 行，含演练分支） |
| `tests/test_tasks_friend_gift_claim.py` | 5 个用例，见 §五-1 |
| `notes/friend_gift_claim_plan.md` | 本文件 |

### 修改

| 文件 | 改动 | 为什么 |
|---|---|---|
| `models/friend.py` | 模块文档去掉「领取不在此列」；`PACKET_PURPOSE[10009]` 改为「好友操作（3=一键领取 / 4=一键全送）」 | 文案已过时，会误导下一个读者 |
| `services/task_registry.py` | `BUILTIN_MODULES` 加 `tasks.friend_gift_claim`；`load_builtin_tasks()` 加一行 import | 不在清单里 = 网页上任务变灰（`notes/web_layer.md` 六-1 的真实缺陷） |
| `services/task_spec.py` | `TASK_SPECS` 在 `mail` 之后插入一条：`group="auto"`、`default_checked=True`、`once_per_day=True` | 卡片、表单、默认勾选、记账全由它派生 |
| `web/assets/app.js` | `TASK_ICONS` 加 `friend_gift_claim: "gift_in"`；`ICON_PATHS` 加一个 `gift_in`（礼盒 + 向下箭头） | 未登记 = 卡片没图标；复用 `gift` 会与「特惠礼包」同屏撞图 |
| `README.md` | 任务表加一行；总数 27 → **37**、自动任务 19 → **21** | 文档与清单同源。⚠️ README 本来就落后：规格表当前是 **36 个**（auto 20 / manual 12 / diamond 4），README 写 27/19。本次只修正总数 + 追加本任务一行，完整对齐另开一轮 |

### 明确不动

- `tasks/daily_task_auto.py`（一键全送仍归它）
- `models/friend.py` 的报文类结构（字段已够用，无需新增）
- `main.py` / `services/runner.py`（无参数任务不需要 CLI 旗标与 `task_kwargs_for` 透传）
- 前端渲染逻辑（任务卡、复选框、执行按钮、二次确认全由 `/api/tasks` 规格驱动，**零改动**）

---

## 五、测试与验证

### 1. 单测（新文件）

1. `test_sends_gain_all_gift_with_minus_one` —— 只发 10009，`actionType==3`、`playerID==-1`；
2. `test_success_reports_friend_and_item_counts` —— 25 好友 + 1 组道具 → `ok=True`，`friend_count==25`；
3. `test_empty_result_is_soft_success` —— `errorCode=0` + 空列表 → `ok=True`，文案含「没有可领取」；
4. `test_nonzero_error_is_soft_success` —— `errorCode=-1` → `ok=True`，`data["error_code"]==-1`；
5. `test_dry_run_sends_nothing` —— 演练零发包，`sent_packets == ()`。

（照 `tests/test_tasks_daily_task_auto.py` 的假 `GameClient` 写法，不联网。）

### 2. 规格一致性（自动被现有测试守住）

`tests/test_services_task_spec.py::TestCoverage::test_every_registered_task_has_spec`
—— 注册了任务却没登记规格会直接红。

### 3. 本地验证

```bash
node --check web/assets/app.js
"<py>" -m pytest tests/test_tasks_friend_gift_claim.py tests/test_services_task_spec.py \
  -p no:cacheprovider --basetemp="tmp/pytest-base-$(date +%H%M%S)"
python main.py run friend_gift_claim --dry-run
```

### 4. 真机（已完成，2026-10-18 15:05）

```bash
python main.py run friend_gift_claim     # ① 领到：5 位好友 · 获得 1 件道具（10010 子包 45 B）
python main.py run friend_gift_claim     # ② 已领完：errorCode=0 + 空 friendList（子包 20 B）
```

**实测结论：「无可领」= `errorCode=0` + 空 `friendList`**（不是错误码）。
§三-3 的三分支设计因此全部有实测支撑；非 0 分支保留作兜底。

### 5. 前端（已用无头 Edge + CDP 读 DOM 验证）

- auto 组卡片 **10 → 11 张**；新卡片标题「好友赠礼一键领取」、`checked=true`、
  `.task-title svg` 有 6 条 path 且 `stroke=currentColor`（`gift_in` 生效）、
  `.task-auto-exec` 存在；
- `/api/tasks` 回报 `default_checked=true`、`once_per_day=false`；
- **不会写 `auto_done`**（每次启动都领的必然结果）—— 卡片不会置灰，属预期。

### 6. 安卓同步（`tasks/` 与 `web/` 都在 `SYNC_DIRS`）

```bash
python tools/sync_android_assets.py      # 必须前台跑（后台会因安全删除确认而半途中断）
sha256sum 对比桌面端 vs android/app/src/main/python/   # 收尾核对
```

### 7. 全量回归（已跑）

**2005 passed / 16 skipped**（2026-10-18 15:06，`pytest tests`，含本次新增 5 例），无失败。

---

## 六、拍板结果（2026-10-18 用户确认）

| # | 问题 | 结论 |
|---|---|---|
| 1 | 「每天自动领一次」还是「每次启动都领」？ | **每次启动都领** —— `once_per_day=False`，不记账 |
| 2 | 图标复用 `gift` 还是新增？ | **新增 `gift_in`**（礼盒 + 向下箭头），已确认与「特惠礼包」不撞图 |
| 3 | 是否改 `daily_task_auto` 那步文案？ | **改** —— 报告项名与演练输出统一为「好友赠礼（全送）」（返回行仍保留"一键全送"，既有测试断言它） |

---

## 七、风险

| 风险 | 缓解 |
|---|---|
| 「无可领」返回码未实证 | ✅ 已实证：`errorCode=0` + 空 `friendList`；非 0 分支仍保留作兜底 |
| `itemAmount` 口径未实证 | 用户文案不报数字（`QuantityScope.UNKNOWN`），实测文案为「获得 1 件道具」 |
| 好友在启动之后才送礼 | ✅ 用户拍板「每次启动都领」；卡片「执行」按钮也能手动补 |
| 忘记登记注册表 → 网页灰显 | 规格一致性测试 + `/api/tasks` 真机检查双重守门 |
