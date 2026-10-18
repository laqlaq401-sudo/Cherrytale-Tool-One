# 「我的角色」只展示 SSR —— 方案计划（2026-10-18，只计划不写码）

需求原话：**信息展示里的「我的角色」只展示 SSR 角色，SR 和 R 排除出去。**

本文件只做侦察与方案，**未改动任何代码**。等用户点头后再动手。

---

## 一、现状数据流（三段，各一段职责）

```
5011/5012 实时名册  ─┐
                     ├─→ tasks/roles.py::RolesTask.execute()   （全量名册，只读）
登录 1004 快照      ─┘        └─ _describe(): 补 name / role_info_id / icon_code，按等级降序
                                └─ TaskResult.data = {source, role_count, roles[...]}

webapi/app.py::player_info()   （/api/player）
    └─ roles = {count, source, fallback_reason, items[...]}

web/assets/app.js::renderInfo() 第 ⑤ 组「我的角色」（约 640~717 行）
    ├─ statCard("角色数量", roles.count, 来源：xxx)
    ├─ role-chip × 前 6 个（ROLE_PREVIEW_LIMIT，app.js:240）
    └─ 「展开全部（共 N 个）」按钮（N = items.length）
```

关键事实：

| 事实 | 依据 |
|---|---|
| 名册里的 `roleID` 是 **RoleMainID**（130xxxxxx），不是 RoleInfoID | `models/role.py::RoleClass` 的 ⚠️ |
| tasks 层**已经**在读配置表（`load_role_id_map`，约 0.10s） | `tasks/roles.py::_load_role_ids` |
| 稀有度判定**已有现成函数**，不必新写 | `models/game_config.py::is_ssr`（1447 行） |
| 属性表加载约 0.23s，两端产物里都有这几张表 | `load_role_attributes()`；`dist/CherrytaleTool/Cherrytale Asset/TextAsset/`、`android/app/src/main/assets/material/...` 均在 |
| `roles` 是 `hidden=True` 的自动任务，界面上没有任务卡 | `services/task_spec.py:1000` |

---

## 二、稀有度判定的硬证据（本次新查，之前只是推断）

`Cherrytale Asset/TextAsset/DrawMachineChanceTitleData` 把稀有度图标与文字直接对上了：

| titleAssetID | title |
|---|---|
| Role_Rarity_1 | N |
| Role_Rarity_2 | R |
| Role_Rarity_3 | SR |
| Role_Rarity_4 | **SSR** |
| Role_Rarity_5 / 6 | Pick UP SSR |

`RoleQualificationData` 里 `qualificationPrint → ui_AssetId` 的对应：

| print | ui_AssetId | 档位 |
|---|---|---|
| 19、20 | Role_Rarity_1 | N |
| 21 | Role_Rarity_2 | R |
| 22 | Role_Rarity_3 | SR |
| **23** | Role_Rarity_4 | **SSR** |
| 24、25 | Role_Rarity_5/6 | Pick UP SSR |

⇒ **`qualificationPrint >= 23` 就是 SSR，含 Pick UP SSR** ——
与项目现有 `models/game_config.is_ssr()`（≥ `SSR_QUALIFICATION_MIN = 23`）
完全一致，`daily_task_auto` 挑"最低等级 SSR"用的也是它。**一套口径，不新造。**

实测（本机 `.session.json` 的 122 条名册）：

```
print=21（R）  24 条
print=22（SR） 12 条
print=23（SSR）86 条   ← 过滤后剩 86 个
```

另外核对过：同一个 RoleInfoID 下所有形态的 `qualificationPrint` **完全一致**
（319 个角色、0 个例外），所以"按形态判定"不会把同一角色判成两档。

---

## 三、推荐方案 A：任务层打标 → 接口层过滤 → 前端只改文案

### 为什么不在 tasks 层直接砍掉

`roles` 任务的本职是"读全量名册"（CLI 也能单跑 `run roles`）。把过滤塞进任务里，
将来想做"看全部 / 只看 SSR"的切换、或别的功能要全量名册，就得改回来。
**"展示口径"放展示层**：任务如实报告 + 打标记，接口决定给界面看什么。

### 三步改动

**① `tasks/roles.py`（补一个字段，不过滤）**

- 新增 `_load_role_attributes()`，容错风格与 `_load_role_ids()` 一致
  （`except (OSError, ValueError)` → 返回 `None`，只 warning，不阻断）。
- `_describe()` 每条多两个键：
  - `rarity`: `"N" / "R" / "SR" / "SSR" / "Pick UP SSR"`，属性表缺失或该 roleID 未收录时 `""`
  - `is_ssr`: `True / False`，无法判定时 `None`（用 `None` 而不是 `False`，
    接口层才能区分"确定不是"与"不知道"）
- `role_count` 仍是**全量**条数（语义不变），排序不变。

**② `webapi/app.py::player_info()`（864~873 行）**

```python
role_items = list(roles.get("roles") or ())
ssr_items = [i for i in role_items if i.get("is_ssr") is not False]  # None 见 §四
"roles": {
    "count": len(ssr_items),          # ← 变成"SSR 条数"（与 items 一致）
    "total_count": len(role_items),   # ← 新增：全量，用来说明排除了多少
    "rarity_filter": "ok" | "unavailable",
    "source": ..., "fallback_reason": ...,
    "items": ssr_items,
}
```

**③ `web/assets/app.js::renderInfo()` 第 ⑤ 组（640~717 行）**

- 卡片标题：`角色数量` → **`SSR 角色`**
- 副标题：`全名册 122（已排除 36）｜实时名册`；
  `rarity_filter === "unavailable"` 时 → `品质表未读到，未过滤｜实时名册`
- 空态（有角色但一个 SSR 都没有）：一行 muted 提示
  `没有 SSR 角色（N 个非 SSR 未显示）`
- `ROLE_PREVIEW_LIMIT`（app.js:234~240）的注释更新：过滤后 SSR 仍可能几十条
  （实测 86），折叠照旧保留；`roleTotal` 自动变成 SSR 条数，
  「展开全部（共 N 个）」的 N 不用额外改。

### 备选方案 B（更省事，但不推荐）

直接在 `_describe()` 里 `continue` 掉非 SSR，`role_count` 变 SSR 数，
webapi/前端几乎零改动。代价：`run roles` 的 CLI 输出也只剩 SSR，
任务语义从"名册"悄悄变成"SSR 名册"，以后想加开关要返工。

---

## 四、边界与降级（必须先说清）

| 情形 | 处理 | 理由 |
|---|---|---|
| 属性表读不到（4 张表任一缺失） | **fail-open**：`is_ssr=None` → 全量透出，`rarity_filter="unavailable"`，副标题明示"未过滤" | 面板变空比"多显示几个"更糟；且必须让用户知道过滤没生效 |
| 名册里某 roleID 不在配置表 | 同上，该条 `is_ssr=None` → 保留 | 新角色早于配置表更新的正常现象 |
| 一个 SSR 都没有 | 显示 `SSR 角色 0` + 空态一行 | 不能只剩一张 0 卡片让人以为坏了 |
| 前端拿到的是旧缓存（无 `total_count`） | 回退：副标题只显示 `来源：xxx` | `total_count` 缺失不报错 |
| 同一 RoleInfo 多个形态同时存在 | 本次**不去重**（实测 122 条 → 122 个 RoleInfo，无重复）；将来出现再议 | 去重规则（留最高星？最高级？）是另一个需求 |

---

## 五、测试与验收

**单元测试**

1. `tests/test_tasks_roles.py`
   - 新增 `patch_role_attributes()` 辅助（仿 `patch_role_id_map`），**必须 patch**，
     否则用例会读真实表、断言随游戏更新漂移。
   - 断言：SSR 角色 `is_ssr is True` / `rarity == "SSR"`；R 角色 `is_ssr is False`；
     未收录角色 `is_ssr is None`。
   - 断言：属性表抛 `OSError` 时任务仍 `ok=True`、`role_count` 不变、
     每条 `is_ssr is None`（守住 fail-open）。
2. `tests/test_webapi_app.py`（877~1020 行那条用例）
   - fake 数据里给两条角色分别 `is_ssr: True / False`；
   - 断言 `count == 1`、`total_count == 2`、`items` 只剩 SSR 那条；
   - 断言 `source` 仍透出（原有断言保留）。

**全量**：`"<py>" -m pytest ... -p no:cacheprovider --basetemp=tmp/pytest-base-<唯一>`
（basetemp 每次必须唯一；不加 `-q`）。基线以当前 HEAD 实跑为准，今天最后一次记录是 **2021 passed / 16 skipped**。

**前端验收（有硬规矩）**

- ⚠️ **起 Web 页面前必须先开演练模式**（或断网/假接口）：boot 会真跑自动任务。
- 判据：`#info-grid` 里 `.role-chip` 数量 == 接口 `roles.count`；
  展开按钮文案的 N == 同一数字；角色数量卡片标题是「SSR 角色」。
- 改完 `web/` 后：重建 tailwind（若新增 class）+ `tools/sync_android_assets.py`
  **必须前台跑**，收尾 `sha256sum` 对比桌面端与 `android/app/src/main/python/`。

---

## 六、已拍板（2026-10-18 22:4x 用户确认）

1. **卡片口径**：只显示 SSR 数（`SSR 角色 86`），**不摆全量、不摆"已排除几个"**；
   副标题仍是「来源：实时名册」。全量数 `total_count` 只在**空态**里用
   （"没有 SSR 角色（全名册 N 个都不是 SSR）"）。
2. **属性表读不到**：**fail-open** —— 全量透出 + 副标题追加
   「角色属性表没读到，本次未按品质过滤」。

## 七、实施记录（2026-10-18）

| 文件 | 改动 |
|---|---|
| `tasks/roles.py` | 新增 `_load_role_attributes()`（`OSError/ValueError` → `None`）；`_describe()` 每条加 `is_ssr`（`True/False/None` 三态）；data 增 `rarity_available`。**不过滤** |
| `webapi/app.py::player_info` | `items` 只留 `is_ssr is not False`；`count` = 该列表长度；新增 `total_count`、`rarity_filter`（`ok` / `unavailable`） |
| `web/assets/app.js` | 卡片标题「SSR 角色」；副标题拼 `rarity_filter` 警示；空态 `.role-empty`；`ROLE_PREVIEW_LIMIT` 注释更新 |
| `web/assets/input.css` | 新增 `.role-empty`（跨满行、虚线边框）→ 必须重建 `styles.css` |
| `tests/test_tasks_roles.py` | 新增 `patch_role_attributes()` + 3 条用例（SSR/SR 标记、整表缺失、单条未收录） |
| `tests/test_webapi_app.py` | 原有用例断言改成"只留 SSR"；新增 fail-open 用例 |

验证：`test_tasks_roles.py` + `test_webapi_app.py` 62 passed；全量见当日日志。
前端用假接口预览（`tmp/preview_role_panel.py` + CDP）实测三种态：

```
正常   chips=6→展开 8，标题「SSR 角色」，值 8，hint「来源：实时名册」，按钮「展开全部（共 8 个）」
空态   chips=0，值 0，无折叠按钮，「没有 SSR 角色（全名册 10 个都不是 SSR）」
fail-open chips=6→展开 10，值 10，hint「来源：实时名册｜角色属性表没读到，本次未按品质过滤」
```

