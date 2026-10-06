# 降临/活动关卡 · 体力扫荡 抓包档案

> 数据来源：`captures/降临扫荡抓包1.saz`（用户实测，2026-09-21，扫荡 1 次 + 5 次各一发）。
> 本文件记录**字节级结论**，目的是：下次不必重新解一遍抓包。
> 对应实现：`models/activity_stage.py` + `tasks/sweep.py`（任务名 `sweep_activity`）。

## 一、整份抓包里只有 4 个网关 POST（**这本身就是结论**）

```bash
PYTHONIOENCODING=utf-8 python tools/extract_saz_posts.py --match 降临
```

```
== 降临扫荡抓包1.saz（29,536,962 B）==
  · raw/02  请求 GetAllActivityStage(11025)     179 B → 响应 GetAllActivityStageRes(11026) 13,598 B
  · raw/03  请求 GetSectionByAreaPacket(11003)  184 B → 响应 GetSectionByAreaRes(11004)      938 B
  · raw/08  请求 SweepSectionPacket(11009)      188 B → 响应 SweepSectionRes(11010)          187 B   ← 扫 1 次
  · raw/10  请求 SweepSectionPacket(11009)      188 B → 响应 SweepSectionRes(11010)          339 B   ← 点 5 次，实发 4
```

同一份 SAZ 的 HTTP 会话统计（**没有第 5 个包**）：

```bash
PYTHONIOENCODING=utf-8 python -c "import glob,zipfile,re,collections;z=zipfile.ZipFile([f for f in glob.glob('captures/*.saz') if '降' in f][0]);h=[(m.group(1),m.group(2)) for n in sorted(z.namelist()) if n.endswith('_c.txt') for m in [re.search(r'^(GET|POST) https?://([^/ :]+)', z.read(n).decode('latin-1','replace'), re.M)] if m];print(len(h), collections.Counter(h).most_common())"
# 10 [(('GET','pfdd.88kongque.com'),6), (('POST','game-ct-labs.ecchi.xxx'),4)]
```

⇒ **扫荡过程没有任何战斗数据上传**：进入战斗、结算、战报、存证一个都没有，
6 个 GET 全是 CDN 资源。与 `SpecialSectionData.cheatCheck=0`（该关不开反外挂校验）互相印证。

## 二、三个接口的形状（字段编号 = dump.cs 声明顺序）

```
11025 GetAllActivityStage              请求体 = 0 字节（空包）
      → 11026 GetAllActivityStageRes   #1 areaStateList(16 条) / #2 memoryAreaStateList(28) /
                                       #3 permanentAreaStateList(37)
11003 GetSectionByAreaPacket{areaID}   → 11004 GetSectionByAreaRes{sectionStateList}
11009 SweepSectionPacket{sectionID, times, useSweepTicket}
      → 11010 SweepSectionRes{errorCode, rewardList, costList}
```

`ActivityAreaStateClass`（**14 字段**，实测 `11026` 元素顶层正好 14 个字段 ✅）：
`areaID / passedCount / extraChallengeCoin / banner / startTime / endTime / bannerCenter /
areaStars / receiveStarReward / weeks / sectionState / progressStr / isMemoryUnlock /
memoryStoreFinish`

`SectionStateClass`（**12 字段**，实测 `11004` 元素顶层正好 12 个字段 ✅）：
`sectionID / sectionState / starCount / limitItemCount / mapBonusState / colorEgg /
extraRefreshCount / extraRefreshCoin / weekDay / hcgBonusState / fullStarBonusState /
stageDistributeID`

> 字段清单以 `python tools/extract_proto_fields.py --show <类名>` 为准；
> 上面两行括号里的「字段数 = 实测字段数」是**独立验证**（模型数与线上字节数一致，
> 说明生成器的「编号 = 声明顺序」在本板块同样成立）。

**关卡轮换 ⇒ 接口不变**：`11009` 是按 `sectionID` 扫任意 Special 关卡的通用包，
每期只换数据（`areaID` / `sectionID`）。所以「自动读当期可扫关卡」= 每次运行重跑
`11025 → 11003`，**不需要重新抓包、也不需要缓存关卡号**。

## 三、逐字段字节图（`--decode` 的输出，含嵌套）

```bash
PYTHONIOENCODING=utf-8 python tools/extract_saz_posts.py --match 降临 --decode raw/10
```

```
请求：11009 SweepSectionPacket
  #1 sectionID       = 129110539     ← 与 11004 返回的关卡号一致（第 9 关）
  #2 times           = 4             ← ⚠️ 用户点的是「5 次」，客户端只发了 4
  #3 useSweepTicket  = 1
响应：11010 SweepSectionRes
  #1 errorCode  = 0
  #2 rewardList = 5 个元素（4 个非空 + 末尾 1 个空）
  #3 costList   = 1 个元素，内含 2 件
```

`GetObjClass` 的字段 1 是 `itemList: repeated ItemClass`，每件 `ItemClass` 三个字段：
**`itemSid`(实例编号) / `itemID`(道具 ID) / `itemAmount`(数量)**。实测取值：

| | raw/08（times=1） | raw/10（times=4） |
|---|---|---|
| `rewardList[0]` | 663259191 / 200300339 / **1050** | 同前两个 / **1450** |
| `rewardList[1]` | 252721379 / 200000050 / **20198609** | 同前两个 / **20200609** |
| `rewardList[2]` | 252721375 / 200000017 / **1446205** | 同前两个 / 1446205 |
| `rewardList[3]`（仅 raw/10） | — | … / 20202609 |
| `costList[0]` | 252721373 / **200000002（体力）** / **160** | 同前两个 / **0** |
| `costList[1]` | 252900055 / **200000016（扫荡券）** / **893** | 同前两个 / **889** |

常量出处（`models/const_ids.py`，来自客户端 `RPS_Const`）：
`ENERGY_ID = 200000002`、`SWEEPTICKET_ID = 200000016`、`COINS_ID = 200000050`、`PLAYER_EXP_ID = 200000017`。

## 四、★ 关键推导：这些数量是「操作后的持有量」，不是「本次变动量」

三条独立证据交叉印证（**这是本板块最容易搞错的一点**）：

1. **体力**：`160 → 0`，差 160；而 `SpecialSectionData.energyCost = 40`，
   `4 次 × 40 = 160` ✅。若 160 是"消耗量"，则 1 次就得消耗 160（与表里的 40 矛盾）。
2. **扫荡券**：`893 → 889`，差 **4 = times** ✅（每次 1 张）。若 893 是"消耗量"则无法解释。
3. **奖励列表逐次递增**：raw/10 的 4 个非空元素依次是活动币
   `1450 → 1850 → 2250 → 2650`（每次 +400）、金币 `20200609 → 20202609 → …`（每次 +2000）；
   而 raw/08（1 次）是 `1050`，`1050 + 400 = 1450` ✅ 与 raw/10 首元素严丝合缝。

⇒ `rewardList` / `costList` 是**服务端顺手回传的最新库存**（客户端拿它刷新 UI）。
对本项目的两个直接用处：

- **不必再查背包接口**就知道"还剩多少体力 / 多少扫荡券"（取最后一次即可）；
- 做账目核对时**不能**把它当成"本次收益"，否则会重复累加。

## 五、「点 5 次为什么发 4」——次数是客户端按体力削出来的

> ★ **先记一条最容易搞错的结论：「扫荡 N 次」= 一个 `11009`（`times=N`），
> 不是 N 个包。** 本抓包里"点 1 次"与"点 5 次"各只产生 **1 个** 11009
> （raw/08 `times=1`、raw/10 `times=4`）。要扫更多就是**重复点按钮**，每次一个包 ——
> 这一点由 `captures/素材关卡扫荡.saz` 直接坐实：**三个 `times=5` 的 11009**
> （raw/08/10/12，同 sectionID `129010056`、同 `useSweepTicket=0`，中间不重发 11003）
> 就是"玩家连点三次扫荡 5 次"的真实形态。
> 复核命令：`python tools/extract_saz_posts.py --match 素材 --decode raw/10`。

| 观测 | 数值 | 说明 |
|---|---|---|
| 每关体力消耗 | `energyCost = 40` | `SpecialSectionData[129110539]` |
| 第 1 次扫荡后体力 | 160 | 原 200（−40） |
| 用户点「5 次」时的最大可扫 | `160 // 40 = 4` | 客户端把 `times` 由 5 改成 **4** 才发出 |
| 扫完 4 次后体力 | 0 | 恰好用光 ✅ |

⇒ 我方实现口径：`times = min(用户要求, 体力 // energyCost, config.SWEEP_MAX_TIMES)`，
并把"被削"这件事打印出来（与实测行为一致）。

⚠️ **诚实标注**：用户提到"5 次与 VIP 等级有关"。实测 `VipData.speicialStageExtraTimes`
在 **VIP0~VIP15 全部为 0**（该列不是扫荡上限），所以本板块**不依赖 VIP 表**，
只把「用户要求 / 体力 / 硬上限」三者取最小值。上限的确切来源登记为 Q-018。

## 六、字段编号验证：`repeated` 字段是最容易踩的坑

`tools/extract_proto_fields.py --show <类名>` 给出的「字段顺序 = 编号」在本板块**成立**，
但有三个字段在线上是 **repeated**（同一编号出现多次），dump.cs 里它们是 `List<int>`：

| 类 | 字段 | 线上证据 | 模型必须写成 |
|---|---|---|---|
| `ActivityAreaStateClass` | `receiveStarReward` | raw/02 元素里 tag 9 出现 **3 次**（`48 01 ×3`） | `list[int]` |
| `SectionStateClass` | `starCount` | raw/03 元素里 tag 3 出现 **3 次**（`18 01 ×3`） | `list[int]` |
| `SectionStateClass` | `limitItemCount` | 同上（dump.cs 里是 `List<int>`） | `list[int]` |

**踩坑记录**：第一遍是按「属性名 + `get; set;`」读 dump.cs 的 —— 而 repeated 字段是
`public List<int> xxx { get; }`（**没有 `set;`**），于是这三行被过滤掉了，模型里写成了 `int`。
后果是解码**静默丢掉前两个值**（不报错、看着还挺正常）。所以现在钉了两条断言：

- `test_area_element_repeated_field_is_a_list` 直接断言 `[1, 1, 1]`；
- `test_models_match_generated_field_table` 断言「模型字段集合 == 生成表字段集合」，防止再漏。

字段数与线上的交叉验证（**独立于生成器的第二条证据**）：
`11026` 元素顶层刚好 **14** 个字段、`11004` 元素顶层刚好 **12** 个字段，与生成表逐一对齐。

顺带记下两个实测取值：`weeks = -1`（**不是"2 周"**，轮换周期只能从
`startTime`/`endTime` 相差 14 天看出来）、`extraChallengeCoin = ItemClass{-1,-1,-1}`
（"没有"用 **-1 占位**，不是空消息）。

## 七、实现与验证命令

| 层 | 文件 |
|---|---|
| 协议 | `models/activity_stage.py`（11025/11026、11003/11004、11009/11010 + 白名单/禁发清单） |
| 配置 | `models/game_config.py`：`SpecialSection` / `SpecialArea` / `sweep_block_reason()` |
| 业务 | `tasks/sweep.py`（任务名 `sweep_activity`） |
| 字节夹具 | `protocol_samples/sweep_samples.py`（本档案用到的 6 段关键字节，可直接复算） |

```bash
# 单测：字节级解析 + 屏蔽规则 + 次数三道闸门 + 白名单
python -m pytest tests/test_activity_stage.py tests/test_sweep_task.py -q

# 只读：列出当期开放区域（只发一个 11025，零消耗）
python main.py run sweep_activity

# 只读：选定区域后列出"可扫 / 被挡（附原因）"
python main.py run sweep_activity --area 可疑

# 演练（不联网、不发包；打印白名单与闸门）
python main.py run sweep_activity --area 2 --times 5 --dry-run

# 真扫：该区每个可扫关卡各扫 1 次
python main.py run sweep_activity --area 2 --times 1

# 只扫某一关（抓包里那关：第 9 关，体力 40）
python main.py run sweep_activity --area 2 --times 5 --section 129110539
```

## 八、待确认（已登记到 `notes/open_questions.md`）

| 编号 | 待确认的事 |
|---|---|
| Q-018 | 「一次最多能扫几次」的**上限来源**（客户端 UI 逻辑？服务端？）。实测 `VipData.speicialStageExtraTimes` 在 VIP0~15 全为 0，所以本板块不依赖它 |
| Q-019 | `SectionStateClass.sectionState` 的取值语义（实测只见 `2`）——"是否已通关"到底看哪个字段 |
| Q-020 | `areaID ↔ 降臨活動/常駐降臨` 标签的**权威映射**（`SpecialAreaData.tagID` 列全空；目前只能靠 `areaID` 前缀 `12821x` 或区域名关键字） |
| Q-021 | `costList` / `rewardList` 的"持有量"语义（抓包三点交叉印证，尚未真机复验） |
| Q-022 | 服务端 `11010.errorCode` 的非 0 取值分别代表什么（体力不足 / 券不足 / 关卡不可扫） |

## 九、真机实测（2026-09-21 21:19，账号 `<redacted>` / #113 伊莉莎白一世 / role 10000001）

```bash
python main.py run sweep_activity --area 可疑          # 只读：9 关全部可扫
python main.py run sweep_activity --area 128110053 --times 1 --section 129110539
# 第 1/1 关 129110539（關卡9）：扫 1 次 → 道具 200300339×28350、200000050×37193710、200000017×1446205
#                             ；体力剩 87；扫荡券剩 169
```

| 验证项 | 结果 |
|---|---|
| `11025` 当期区域表 | ✅ 16 个区域（含"英雄降臨【奈菲瑟】EX"等），结构与抓包一致 |
| 名称关键字选区域 | ✅ `--area 可疑` → **128110053**（与抓包同一个区域） |
| `11003` 关卡表 | ✅ 9 关（129110531~129110539），与配置表体力 8/10/12/15/20/25/30/35/**40** 逐一对应 |
| 屏蔽规则 | ✅ 可扫 9 / 被挡 0（本区没有零体力或禁扫关卡，符合预期） |
| `11009` 真扫 | ✅ `errorCode=0`、奖励 3 件、任务 `ok=True` |
| 轮换无需抓包 | ✅ 直接由 `11025`+`11003` 读出当期关卡，全程未用抓包数据 |

### ★ Q-021 就此结案：`costList` 确实是「操作后持有量」

本次响应回传的是 **体力剩 87、扫荡券剩 169**。若它是"消耗量"，值必然是
`energyCost`（40）或 40 的整数倍 —— 而 **87 不是 40 的倍数**，所以只能是**余额**。
这与抓包里的三点交叉证据（体力 160→0 差 160 = 4×40、券 893→889 差 4 = times、
奖励逐次递增 1450→1850→2250→2650）完全一致。

⇒ `SweepSectionRes.energy_left / sweep_ticket_left` 的语义**已由真机确认**，
"用响应回传的剩余体力削下一关次数"这条链路也因此成立。

> 顺带记录：非登录任务不需要 `CHERRYTALE_SERVER_ID`（会话文件里已有区服）；
> 但**登录**时若账号在该服有多个角色，必须 `CHERRYTALE_SERVER_ID=113` 显式指定，
> 否则会被自动挑到别的区（本次第一次登录就被挑进了 #167，已用显式指定纠正）。

## 十、Q-019 结案：`sectionState` = 0 未通关 / 2 已通关（2026-09-21 只读实验）

零消耗实验（只发 `11025` + `11003`，不发 `11009`）：

```bash
python main.py run sweep_activity --area 128210047   # 英雄降臨【間宮麻理沙】，进度 0/6（未通关）
# 关卡共 1 个：可扫 0 个、被挡 1 个
#   ✘ 129210521：未通关（sectionState=0，已通关是 2）—— 确实想扫它请加 --allow-unpassed
```

| 区域 | 状态 | `sectionState` |
|---|---|---|
| `128110053` 可疑的工作（进度 5/5，**已通关**） | 9 关 | 全是 **2** |
| `128210047` 英雄降臨【間宮麻理沙】（进度 0/6，**未通关**） | 1 关（129210521） | **0** |
| 抓包里那个账号（已通关） | 9 关 | 全是 2 |

⇒ **`0` = 未通关、`2` = 已通关**。目前只见过这两个取值，所以判据写成**白名单式**
（只有 `== 2` 才算已通关，见 `tasks/sweep.py::PASSED_SECTION_STATE`）：
将来若出现新取值（如 3 = 满星通关），最坏结果是"少扫"，不会"误扫"。

### 随之新增的通关护栏（默认开启）

| 行为 | 说明 |
|---|---|
| 默认只扫**已通关**关卡 | 未通关的会**列在"被挡"里并写明原因**，但不发 `11009` |
| 逃生门 | `--allow-unpassed`（对应 `only_passed=False`）才允许扫未通关关卡 |
| 为什么必须客户端做 | 服务端只按 `sectionID` 结算，**不会**替你挡"还没打过的关卡" |

```bash
python main.py run sweep_activity --area 128210047                             # 列出 → 未通关，被挡
python main.py run sweep_activity --area 128210047 --times 1 --allow-unpassed   # 显式放行
```

回归用例：`tests/test_sweep_task.py::TestPassedGuard`（默认挡 / 混合区跳过 / 显式放行 三例）。

## 十一、Q-018 / Q-020 / Q-022 三项结案（2026-09-21，静态枚举 + 配置表元数据）

### 11.1 Q-018：「一次最多扫几次」= 客户端固定三档 `{1, 5, 10}`

`dump.cs` **L530313** 找到客户端枚举：

```csharp
public enum StageModule.eSweepType // TypeDefIndex: 11666
{
    One  = 1;
    five = 5;
    Ten  = 10;
}
```

⇒ 扫荡面板**没有任意次数输入**，只有这三档；"5 次"就是 `five`。
错误码枚举里的 `VipNotEnough = -9` / `NoAnymonthCard = -10` 说明某些档位或关卡有
VIP / 月卡门槛（门槛值未定，也不影响本项目 —— 我们只发档位内的次数，再按体力削峰）。

**落到实现**（`models/activity_stage.py`）：

| 项 | 值 |
|---|---|
| `SWEEP_TIME_CHOICES` | `(1, 5, 10)` |
| `normalize_sweep_times(7)` | `(5, "…已把 7 向下归到 5")` —— **向下取**（多扫一次就多扣资源） |
| `normalize_sweep_times(99)` | `(10, "…最高 10 次…")` |
| `config.SWEEP_MAX_TIMES` 默认 | **10**（= 客户端最高档，同时充当防手误的硬上限） |

现场验证（零消耗）：`--times 7 --dry-run` →
`档位归一：客户端档位只有 [1, 5, 10] 三档（eSweepType），已把 7 向下归到 5`。

### 11.2 Q-022：`11010.errorCode` 的完整语义（客户端枚举）

`dump.cs` **L530264**：`public enum StageModule.eSweepErrorCode`（TypeDefIndex 11663）：

| 码 | 名字 | 含义 |
|---|---|---|
| 0 | Success | 成功 |
| -1 | NoClearance | **未通关** —— 正是「只扫已通关」护栏挡下的那一类（服务端也会拒） |
| -2 | NoEnergy | 体力不足 |
| -3 | NoTicket | 扫荡券不足 |
| -4 | NoDreamStone | 钻石不足 |
| -6 | DataError | 数据错误 |
| -7 | TimesIsFull | 次数已满 |
| -8 | DailyClose | 该活动今日关闭 |
| -9 | VipNotEnough | VIP 等级不足 |
| -10 | NoAnymonthCard | 没有月卡 |
| -11 | NoOpenForSweep | 该关卡未开放扫荡 |

（枚举里没有 `-5`。）已全部写进 `models.activity_stage.ERROR_NOTES`，
`describe_error_code()` 现在能翻译这些码 —— 只有 `0` 经真机实测，
其余标注为"来源＝客户端枚举，尚未逐个在真机触发"。

### 11.3 Q-020：区域↔标签的映射列**存在但数据为空** → 不可自动判定，也不需要

`Cherrytale Asset/TextAsset/DataRelative` 是"表关联元数据"，里面明确写着：

```
SpecialAreaData|tagID|SpecialAreaTagData|tagID        ← 区域 → 标签
SpecialSectionData|areaID|SpecialAreaData|areaID      ← 关卡 → 区域
```

但**这两列在数据里都是空的**（`SpecialAreaData.tagID` 379 行全空；
`SpecialSectionData.areaID` 1633 行全空）⇒ 客户端不可能靠它们分组，
本项目也**没有**可靠的"自动判定某区属于降臨活動"的依据。

**因此维持现状**（也是更安全的做法）：

- `--area` 支持 **areaID / 清单编号 / 名称关键字**（`--area 可疑`、`--area 英雄降臨`）；
- **不做**"自动挑降临区"的猜测 —— 挑错区会真扣资源，而多敲一个关键字不会；
- 观察到的 areaID 前缀规律（`12821x` = 英雄降臨那一类）只作为**人读参考**记在这里，不进代码。

> 顺带说明：`11003` 会直接返回某区的关卡列表，所以**业务上根本不需要**这条映射 ——
> 这也是 Q-020 从一开始就只是"好奇"、而非"阻塞"的原因。

