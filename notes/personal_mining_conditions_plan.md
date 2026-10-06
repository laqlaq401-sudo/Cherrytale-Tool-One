# 个人挖矿「角色条件」建模方案（Q-044 的收口）

> 2026-10-06 凌晨制定。触发：真机 `21047` 一直回 `errorCode=-2`，7 个空闲矿点全灭。
>
> **本文最重要的发现**：矿点要的 7 类角色条件，**数据全都在离线素材里** ——
> 只是**被文本导出抹空了**，藏在 `DataRelative` 这张"关系索引表"里。
> 之前判断"元素/资质拿不到"是**错的**（已撤回）。

---

## 零、结论速览

| # | 结论 | 证据 |
|---|---|---|
| 1 | **每个个人矿点都要求元素 + 资质**（140/140 行），另有 84 行要求职业、64 行要求星级、50 行要求品质、5 行要求种族 | 统计 `AlliancePersonalPitData` |
| 2 | 我们的 `roleSidList` **只按等级从高到低挑**，7 类条件一条都没判 ⇒ 必然被拒 | `tasks/alliance_mining._build_one_key_elements` |
| 3 | **角色侧的元素/资质/职业/种族在文本表里全是 NULL**（`RoleMainData.element` 0/6653 非空、`roleQualification` 0/6653、`RoleInfoData.jobType` 0/587、`race` 0/587） | 逐列统计 |
| 4 | **但它们在 `DataRelative` 里** —— 那是一张「表间引用索引」，逐行对齐源表，把被抹空的引用列原样给出 | **见 §二，已用 160 条独立真值 160/160 验证** |
| 5 | 7 类条件的**比较语义**已从反汇编拿到（见 §三）：元素/资质/职业/种族是 **==**，星级/等级/品质是 **>=** | `NewPitPersonalLimit.IsAchieveLimit` 反汇编 |
| 6 | 客户端挑人函数是 `NewPit.PersonalCanStart(out roleSidList, iFillRole, iCheckIsRoleJoin)` | 反汇编 |

---

## 一、矿点要什么（`AlliancePersonalPitData`，140 行）

| 条件列 | 被使用的行数 | 取值 | 语义（§三） |
|---|---:|---|---|
| `qualificationCondition` | **140 / 140** | 21 / 22 / 23 | **==** `qualificationPrint` |
| `elementCondition` | **140 / 140** | 138000001~138000007（可 `$` 多值） | **==** 角色的 `elementID` |
| `jobCondition` | 84 / 140 | 900000001 / 900000002 | **==** `JobData.jobID` |
| `starCondition` | 64 / 140 | 2 / 4 | **>=** 星级 |
| `qualityCondition` | 50 / 140 | 1 | **>=** 品质档 |
| `raceCondition` | 5 / 140 | 900001001 | **==** `RaceData.id` |
| `lvCondition` | 0 / 140 | —— | **>=**（本作没用上） |

`*ConditionNumber` 是"要有几个角色满足这条"，与 `roleMaxCondition`（本坑总人数）独立。

**本次真机失败的 7 个坑**（`tmp/failed-request_21047_*.hex` 解出）：

| 模板 | 名称 | 人数 | 资质 | 元素 | 职业 | 星级 | 种族 |
|---|---:|---:|---|---|---|---|---|
| 935000114 | 牛刀小試 | 2 | 23×1 | 水×1 | — | — | — |
| 935000117 | 初露鋒芒 | 3 | 23×1 | 風×1 | — | ≥4 | — |
| 935000118 | 初露鋒芒 | 3 | 23×1 | 土×1 | — | ≥4 | — |
| 935000126 | 錦上添花 | 5 | 23×2 | 風×1 | 900000001 | ≥4 | — |
| 935000131 | 驚天動地 | 6 | 23×2 | 風×1、土×1 | 900000001 | ≥4 | — |
| 935000132 | 驚天動地 | 6 | 23×2 | 土×1、水×1 | 900000002 | ≥4 | — |
| 935000136 | 雷霆萬鈞 | 6 | 23×1 | 土×1 | 900000001 | ≥4 | 900001001 |

⇒ **每一个坑都要元素 + 资质**。只按等级挑人，命中概率约等于 0。

---

## 二、数据在哪：`DataRelative`（★ 本次的核心发现）

`Cherrytale Asset/TextAsset/DataRelative`（3.9 MB，987 行）是一张
**表间引用索引**：每两条一行 —— 第 1 行是关系声明 `源表|字段|目标表|目标字段`，
第 2 行是**逐行对齐源表**的引用值列表（`|` 分隔）。

它之所以长成"没有正常表头"的样子，是因为它**本来就不是普通表**：
它是配置系统用来解析 `NULL` 引用列的反查索引。项目的 `read_table` 按
"统一列数"解析它当然会失败 —— 之前没人意识到这一点，就把它跳过了。

### 2.1 我们需要的四条关系

```
RoleMainData|element|RoleElementData|elementID                              → 6653 个值
RoleMainData|roleQualification|RoleQualificationData|roleQualificationID    → 6653 个值
RoleInfoData|jobType|JobData|jobID                                          →  587 个值
RoleInfoData|race|RaceData|id                                               →  587 个值
```

外加 `RoleMainData|info|RoleInfoData|roleID`（6653 个值）——
**它就是 `RoleMainData.info` 那一列**，用来把 RoleMain（形态）关联到 RoleInfo（角色）。

### 2.2 ★ 验证：160/160 精确相同

`DrawSingleData`（抽卡表）里有 160 行 `objectID`（RoleMainID）**同时**给出了
`element`。这是一份**完全独立**的真值：

```
★ 抽卡真值验证：160 / 160 精确相同
```

⇒ 这条映射是**实测验证过**的，不是推断。

### 2.3 元素分布（合理性佐证）

| elementID | 元素 | 角色数 |
|---|---|---:|
| 138000001 | 風 | 1320 |
| 138000002 | 土 | 860 |
| 138000003 | 水 | 820 |
| 138000004 | 火 | 891 |
| 138000005 | 無 | 80 |
| 138000006 | 光 | 882 |
| 138000007 | 闇 | 1060 |
| | **合计** | **5913 / 6653**（其余是 BOSS/特殊形态） |

---

## 三、条件怎么判：`IsAchieveLimit` 反汇编

`AllianceNetWorkModule.NewPitPersonalLimit.IsAchieveLimit(RoleMain)`（Offset `0x20C40A0`）
按 `m_pitLimitType`（`ePitLimitType`）分派，逐条读出来：

| 枚举 | 值 | 读的角色字段 | 比较 |
|---|---:|---|---|
| `Qualification` | 0 | `QualificationSetting[+0x14]`（`qualificationPrint`） | **`jne → false`（==）** |
| `Element` | 1 | `ElementSetting[+0x10]`（`elementID`） | **`jne → false`（==）** |
| `Job` | 2 | `InfoSetting.get_JobID()` | **`jne → false`（==）** |
| `Star` | 3 | `StarLv()` | **`jl → false`（>=）** |
| `Lv` | 4 | `Level()` | **`jl → false`（>=）** |
| `Quality` | 5 | `eQuality()` | **`jl → false`（>=）** |
| `Race` | 6 | `InfoSetting[+0x38]` | **`cmove`（==）** |

⚠️ **元素比的是 `elementID`，不是 `elementType`** —— 而矿点表里的
`elementCondition` 给的正是 `138000001..138000007` 这些**基础 elementID**。
所以必须用 §二 的 `DataRelative` 拿到角色的 elementID 才能比。

---

## 四、客户端怎么挑人：`NewPit.PersonalCanStart`

`AllianceNetWorkModule.NewPit.PersonalCanStart(out List<int> iLztRoleSid, bool iFillRole,
bool iCheckIsRoleJoin = True)`：

1. `GetAllRole()` 拿全部角色；
2. `iCheckIsRoleJoin` 时排除**已经在别的坑里**的角色
   （`IsRoleJoinPit_Personal` / `IsRolePiting_Personal`）；
3. 排序：`OrderBy(eRear)` 再 `ThenByDescending(int)`（后排/战力之类的偏好键）；
4. 对每条条件取 `NeedCount` 个满足它的角色；
5. 补足到 `RoleMaxCondition` 个。

配套的 `NewPit` 字段：`m_dic_newPitLimit_Personal`（按类型分组的条件）、
`m_lzt_useRoleSid`（本坑已用角色）、`m_roleMaxCondition`。

---

## 五、实施方案

### 5.1 数据层（`models/game_config.py`）

1. `TABLE_DATA_RELATIVE` + `load_data_relative()` → `{关系声明: [值, …]}`；
2. `RoleAttributes`（`element_id` / `qualification_print` / `job_id` / `race_id`）+ `load_role_attributes()`
   → `{roleMainID: RoleAttributes}`（用 `info` 关系接 RoleInfoData 拿 job/race）；
3. `PersonalPitCondition` 扩成 7 类条件齐备（每类 `(值元组, 数量元组)`）；
4. 条件判定：`PersonalPitCondition.matches(attrs, level, star, quality)` 逐条按 §三 的语义比。

### 5.2 任务层（`tasks/alliance_mining.py`）

`_build_one_key_elements` 改为：
1. 先按条件为每个坑挑人（满足每条 `NeedCount`），再补足 `roleMaxCondition`；
2. 跨坑不复用角色（已有 `used_role_sids`）；
3. **判不了就别发**：任何一条条件挑不满 ⇒ 该坑不进请求，并如实报缺
   （比发一个必被拒的编队好）。

### 5.3 测试

- `DataRelative` 解析 + **160/160 抽卡真值**钉死（这是全案地基）；
- 7 类条件的比较语义各一条（含"元素比 elementID 而不是 elementType"这条反例）；
- 挑人：条件挑满、跨坑不复用、挑不满就不发。

---

## 六、待确认（真机）

1. `-2` 是否真的由此消除 —— 跑一次 `alliance_mining_personal` 看 21048 的 errorCode；
2. 排序偏好（`eRear`/战力）是否影响服务端判定 —— 大概率不影响（服务端只看条件），
   但若仍被拒，按 §四 第 3 步对齐排序键；
3. `qualityCondition` 比的是"当前品质档"，而 `RoleClass` 不带品质 ——
   本项目用 `RoleMainData.quality`（该形态的基础档）近似，**可能偏保守**。

---

## 七、一句话交接

> 个人矿的 7 类条件**数据全在** `DataRelative`（文本导出抹空的引用列都在那儿，
> 逐行对齐源表）。元素比的是 `elementID` 且是 `==`，星级/等级/品质是 `>=`。
> 之前"元素拿不到、只能按等级兜底"的结论**已撤回**。

---

## 八、实施记录（2026-10-06 凌晨落地）

### 8.1 改了什么

| 文件 | 改动 |
|---|---|
| `models/game_config.py` | 新增 `TABLE_DATA_RELATIVE` / `REL_*` 关系键 / **`load_data_relative()`** / `_relation_aligned()`（行数不符就报错）/ `RoleAttributes` + **`load_role_attributes()`** / `BASE_ELEMENT_NAMES`；`PersonalPitCondition` 扩成 7 类条件齐备 + `PitConditionTerm`（含 `matches()` 与可读 `label`）；`load_personal_pit_conditions()` 解析多值条件（`$` 拆成多条） |
| `tasks/alliance_mining.py` | 新增 `_role_attributes()`（惰性读表，读不到返回 `None` 而不是空表）/ `_pick_roles_for_pit()`（按条件挑人，与客户端 `PersonalCanStart` 同构）/ `_no_startable_reason()`；`_build_one_key_elements()` 改为**按条件挑人**，挑不满整坑不发；模块头与 `_dry_run` 文案同步 |
| `tests/test_game_config_pit_conditions.py` | **新增**（19 例）：`DataRelative` 解析 + **160/160 抽卡真值**钉子 + 7 类条件语义（含"元素比 elementID 不是 elementType"的反例钉子）+ 属性表覆盖 + 模板条件解析 |
| `tests/test_tasks_alliance_mining.py` | **新增 4 例**：按元素条件挑人 / 条件不满足整坑不发并报出缺哪条 / 条件角色跨坑不复用 / 属性表读不到不许撒谎 |

### 8.2 行为对照（改前 → 改后）

| 场景 | 改前 | 改后 |
|---|---|---|
| 挑人依据 | 只按等级从高到低 | **按坑的 7 类条件**，再补足 `roleMaxCondition` |
| 条件凑不满 | 照样提交（必被拒 → `-2`） | **整坑不发**，报出缺的是哪条条件 |
| 报缺文案 | 「没有可成行的矿点（见分配告警）」 | 「没有可成行的矿点：矿点 41 条件：元素=水（138000003）、资质=23 —— 条件「元素=水（138000003）」需要 1 个角色，名册里只凑到 0 个，已跳过（不提交残阵）」 |
| 属性表读不到 | —— | **不阻断、不撒谎**：明确报"读不到角色属性表" |

### 8.3 测试与同步

- `tests/test_game_config_pit_conditions.py`（19 例）+ `tests/test_tasks_alliance_mining.py`（37 例）
  + `tests/test_game_config_roles.py`（12 例）**全绿**；全量 `pytest` **0 失败**。
- `tools/sync_android_assets.py` 已跑：配置表 **38 张**（`DataRelative` 4.0 MB 已进包，
  表扫描器从新增的 `TABLE_DATA_RELATIVE` 常量自动识别）。

### 8.4 仍未真机验证

1. **`-2` 是否真的消除** —— 跑一次 `alliance_mining_personal` 看 21048 的 errorCode。
   若仍被拒，按 §六 第 2/3 条对齐排序偏好与品质口径。
2. **`qualityCondition` 的口径**：比的是"当前品质档"，而 `RoleClass` 不带品质，
   本项目用 `RoleMainData.quality`（该形态的基础档）近似 —— **可能偏保守**
   （50/140 个模板要求 `品质≥1`，而基础形态的 quality 是 0）。
   若真机上"条件都满足却仍被拒"，优先怀疑这一条。
3. **排序偏好**（客户端按 `eRear` + 战力排序）本项目未复刻 —— 服务端大概率只看条件，
   但若被拒需对齐。
