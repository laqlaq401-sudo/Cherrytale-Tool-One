# 工会挖矿「占坑配额」修复方案（只做计划，不改代码）

> 2026-10-05 晚制定。触发：23:49 那次真机运行 —— 团体矿 **3 次占坑成功 + 15 次
> `errorCode=-8`**，个人矿 7 个空闲矿点全灭（`-2`）。
> 用户要求：**「一个一个去发送服务端请求」要避免，只把能填进坑的数据发出去。**
>
> 本文所有结论都标了证据来源。**未实测确认的部分一律显式写出。**

---

## 零、结论速览

| # | 结论 | 证据强度 |
|---|---|---|
| 1 | **ID 换算修复生效了** —— 3 次占坑成功，且报缺文案已经能报出角色名（金禮子 / 愛多絲 / 羊媽媽…）与三种不同原因 | 实测（本次日志） |
| 2 | 团体矿有**每日总次数上限**：`TeamCount < TeamLimitCount` 才允许占坑 | **反汇编实证** |
| 3 | 每个坑里**我最多占 1 个槽位** | **反汇编 + 游戏内文案双证** |
| 4 | 一个角色**已经在别的坑里**时，不能再占新坑 | **反汇编实证**（跨运行未实现） |
| 5 | 上一轮把 `teamLimit` 判成"按坑的限制、不能当配额用"是**误读**，据此删掉本地闸门是**错的** —— 直接造成今天 15 次无效请求 | 反汇编实证（见 §二） |
| 6 | 个人矿 `-2` **不是**配额问题，是已知的「角色条件未建模」（Q-044 遗留） | 10.3 抓包 `personalLimit=8` 而 8 个坑全空闲 |

**一句话**：团体矿的问题不是"服务端不认"，而是**我们把服务端的规则全都没在本地判**——
超了每日次数、同一个坑连占两个、角色已经在别处，服务端当然一个个拒。

---

## 一、这次日志告诉我们什么

```
✔ 23:50:35 工会团体挖矿 · 24.41s · 团体矿：占坑 3 次；
   矿点 …（33 条 unfillable，分因文案）…；
   矿点 206024537 槽位 0：errorCode=-8；…（共 15 条 -8）
```

| 现象 | 解读 |
|---|---|
| **占坑 3 次成功** | ID 换算（`RoleIdMap`）真的修好了 —— 这是上一轮那个 bug 的直接验证 |
| **报缺文案分了三因** | 「名册里没有这个角色」×18 / 「最高只有 N 级」×14 / 「已被本轮其它坑位占用」×1 —— 新文案按预期工作 |
| **3 成功 + 15 × -8** | 顺序上是"先成功后全失败"，**不是**随机失败 → 指向**累计型限制** |
| **33 条 unfillable** | 说明筛选本身是对的：这 33 个槽位确实占不了，没发请求 |
| **个人矿 7 空矿全灭 `-2`** | 一次批量请求整体被拒，属 Q-044（角色条件）遗留，本次**不动** |

### 顺手发现的一个可疑点（建议一并确认）

有 8 个槽位的报缺是「名册里有 1 个，**最高只有 1 级**」，另有 6 个是 31/35/44 级差几级。
如果这些"1 级"不是玩家真有的 1 级复制品，而是**名册来源不对**（比如退回了登录快照、
或 5012 的 `roleLV` 解码错位），那就还有一个隐藏问题。
**判据**：看 `roles` 任务的 `source` 是 `live` 还是 `snapshot`，以及名册里各角色的等级分布。
（本次挖矿日志里没打 `source` —— 见 §四 改动 5。）

---

## 二、根因：客户端自己的"能不能点 ＋ 号"判据，我们一条都没实现

### 2.1 客户端在哪里判

`AllianceNewPit_MainUI.PitRoleIcon.CheckCanJoinRole(bool iShowMsg)`（Offset `0x20CD5F0`）
就是"点 ＋ 号"前的守卫。反汇编它，调用目标里出现了这五个：

```
0x180EB0270  get_TeamCount()                    ← 今日已用团队矿次数
0x180EB05A0  get_TeamLimitCount()               ← 今日团队矿次数上限
0x180EA6750  GetRoleJoinPitCount_Team(int)      ← 我在**这个坑**里已占几个
0x180EA1070  GetTeamPitRoleCountLimit()         ← 每坑我最多能占几个（配置表来的）
0x180EA7B00  IsRoleJoinPit_Team(int iRoleInfoID)← 这个角色是否已在某个团队矿里
0x180823250  HintViewMessge(...)                ← 不满足时弹提示
```

`PitRoleIcon.IsTeamCountMax()`（Offset `0x20CDAB0`）单独把这层抽出来：
**比较 `get_TeamCount()` 与 `get_TeamLimitCount()`**。

### 2.2 三个限制各自的证据

| 限制 | 客户端实现 | 佐证 |
|---|---|---|
| **每日总次数** | `TeamCount < TeamLimitCount` | `AllianceNetWorkModule` 有 `m_teamCount`(0xA4) / `m_teamLimitCount`(0x94)，配套 `TeamRemainCount`；`UpdatePitTeamCountText()` 读 `TeamRemainCount` + `TeamLimitCount` 渲染「剩余/上限」 |
| **每坑我最多 1 个** | `GetRoleJoinPitCount_Team(pitSid) < GetTeamPitRoleCountLimit()` | 游戏内说明原文：「每個團隊挖礦公會成員**最多僅能上陣1個**自身擁有符合指定條件的角色」 |
| **角色不能已在别处** | `IsRoleJoinPit_Team(iRoleInfoID)` | 同上文案第 2 条；`IsRoleJoinPit_Team` 遍历坑列表找该角色 |

另有文案兜底：「**今日團隊挖礦已達執行上限，無法開始挖礦。**」
（`Cherrytale Asset/TextAsset/LanguageSettings_zhcn` 第 16818 行）

### 2.3 `teamLimit` 到底是什么（★ 上一轮的误判在这里）

| | 上一轮（2026-10-05 白天）的判断 | 实测结论 |
|---|---|---|
| `teamLimit` | "客户端把它建模成**按坑**的 `List<NewPitTeamLimit>`，**不是**全局 int，故不可当配额用" | **`AllianceNewPitRes` 只有 5 个字段**（`errorCode` / `personalList` / `teamList` / `personalLimit` / `teamLimit`）—— `teamLimit` 就是一个 **int** |
| `m_lzt_newPitLimit_Team` | 被当成"按坑的配额表" | 它其实是**坑的槽位列表**：`NewPitTeamLimit` 这个类**只包了一个 `AllianceTeamPitDetailClass`**（即一个槽位），getter 是 `NeedRoleID` / `NeedRoleLv` / `Member` / `MemberPid` —— **跟配额无关** |

⇒ 真正的配额是 `m_teamLimitCount` vs `m_teamCount`，而 `teamLimit` 就是 `m_teamLimitCount` 的来源。

**上一轮据此删掉了本地闸门**（`tasks/alliance_mining.py:666-672`、`models/alliance.py:724-735`
都还留着"不能当配额用"的注释）。今天的 15 次无效请求，就是这次删除的直接后果。

### 2.4 数值自洽性检查

10.3 抓包实测：`personalLimit = 8`、`teamLimit = 4`。
本次 3 次成功 ⇒ 若 `teamLimit = 4` 且**今天已用掉 1 次**，则剩余正好 **3**。

> ⚠️ **这一步是推断，不是实测** —— 日志里没有打 `teamLimit` / `already_mine`。
> 但两个候选解释（`teamLimit=4` 且已用 1；或今天 `teamLimit=3`）都指向同一个修法，
> 所以**不影响方案**，只影响我们对"上限具体是几"的表述。

### 2.5 `-8` 的确切语义：**未证实**（不要编）

`AllianceNetWorkModule` 里只有两个负数枚举，都不含 -8：

- `eResErrCode`：**全是正数** 0~42
- `eBingoErrorCode`（签到用）：-6 / -11 / -27…-33 —— 已解释日志里的 `-32`「尚未连成线或已领过」
- `eOtherSysPacketResErrCode`：`HaveNotBeen_ExitedMyAlliance = -7`

**-8 不在任何工会枚举里。** 而且 -8 在别的模块里含义各不相同
（`StageModule.eSweepErrorCode.DailyClose`、`GrandLineLocked`、`noSpaceBoard`…），
说明**负数错误码是按功能分组的，不能跨功能套用语义**。

⇒ 本方案**不给 -8 起名字**。修法只依赖"客户端自己判过这三条"，不依赖 -8 的含义。

---

## 三、方案（对应你的两条要求）

> 你说的「一个一个去发送…要避免」+「只发送能够填进去坑的数据给服务端」
> = 下面 **A + B + C + D**。

### 改动 A（核心）：恢复每日次数闸门，并在额度用尽时**立刻停手**

```python
allowance = max(state.teamLimit - already_mine, 0)
```

- `already_mine` = `teamList` 里 `memberPid == 我` 的槽位数（已有实现，现在只当观察值）。
  ✅ 已核实字段口径正确：抓包里 `slot.memberPid == slot.member(IconClass).playerID`，
  都是**玩家 ID**（实测 6209807 / 6201999 / …）。
- **额度为 0 时直接返回**「今日团队矿次数已用尽（上限 N，已用 M）」，**一个包都不发**。
- 占满 `allowance` 次成功后**停止**，不再继续尝试。

**边界（防"静默漏做"）**：`teamLimit <= 0` 时**不要**当成 0。
那意味着服务端没下发这个值（老版本/异常），此时**退化为"服务端裁决"**
（= 现在的行为）并打一条 WARNING。宁可多发几次，也不要因为读不到配额就什么都不做。

### 改动 B：每个坑**最多占 1 个槽位**

- 跳过**我已经占了槽位的坑**（`any(slot.memberPid == my_pid)`）。
- 一个坑里本轮**只占 1 个**，占完就换下一个坑。
- 直接对应 `GetRoleJoinPitCount_Team(pitSid) < GetTeamPitRoleCountLimit()`。

> 影响：这**不会**让我们少占坑 —— 同一坑连占 2 个本来就一定会被拒。
> 反而把有限的额度花在**不同的坑**上，更接近"帮公会补满更多坑"的目标。

### 改动 C：失败即停（防御，不猜语义）

即使有 A/B，仍可能因为**我们看不到的状态**（角色早被放在别的坑、玩家刚在游戏里操作过）
被拒。此时**继续发同样的请求没有意义**：

- 记录本轮错误码；**同一个错误码连续出现 2 次**就**停止本轮后续占坑**，
  把该错误码原样报出来（附"疑似已达限制，已停止继续尝试"的说明）。
- 这**不声称**我们知道 -8 是什么，只说"连续被同样理由拒绝，再发也是白费"。

### 改动 D：日志按原因聚合（现在一次刷 50 行）

本次运行视图里：15 条 `⚠ ✘ 占坑失败 …` 单行 + 汇总里**再列一遍**，
再加 33 条 unfillable 明细 —— 共约 60 行，人根本读不了。

建议：

- 汇总**按原因/错误码聚合**：`占坑失败 15 次（errorCode=-8 ×15，已停止继续尝试）`；
  `无法占坑 33 个（名册里没有该角色 ×18、等级不足 ×14、已被本轮占用 ×1）`。
- 逐条明细降到 **DEBUG**（开发者日志仍保留全量，符合项目既有「运行视图纪律」）。
- 顺带把 `team_limit` / `already_mine` / 名册来源（`live`/`snapshot`）打进汇总。

### 改动 E：诊断（**先做这个**，一次真机跑就能定案）

1. 运行视图/DEBUG 打出：`teamLimit`、`already_mine`、`allowance`、成功次数、各错误码计数。
2. 首次占坑失败时，把 **21044 的原始响应字节**转储到 `tmp/`
   （复用 `tasks/packet_guard.py` 的转储机制；目前它只在"响应不是游戏协议"时转储，
   业务错误码不转储 —— 需要扩一个"首次业务失败也转储"的口子）。

> **为什么必须先做 E**：改动 A 的闸门依赖 `already_mine` 口径正确。
> 若实测发现 `already_mine` 恒为 0（例如 `my_pid` 与 `memberPid` 不同源），
> 闸门就会失效。E 的第 1 条一眼就能看出来。

### 改动 F（**需你拍板**，可暂不做）：跨运行记住"角色已放置"

客户端有 `IsRoleJoinPit_Team(roleInfoID)`：**一个角色已经在某个团队矿里，就不能再占新坑**。
我们只在**本次运行内**去重（`used_role_sids`）。所以：

- 同一天跑第二次挖矿任务时，可能把**已经在坑里**的角色再派一次 → 被拒；
- 玩家自己在游戏里放过的角色，我们完全不知道。

两种做法：

1. **不做**（推荐先不做）：把跨运行重复提交的失败交给改动 C 的"失败即停"兜住。
2. **做**：在会话文件里记一份「今天我用过哪些 roleSid」（按游戏日重置），
   下次运行时排除。代价是多一份状态要维护、且与游戏日边界耦合。

---

## 四、分步 TODO

| # | 改动 | 文件 | 备注 |
|---|---|---|---|
| 0 | **诊断先行**：打出 `teamLimit` / `already_mine` / `allowance` / 错误码计数；首次业务失败转储 21044 原始字节 | `tasks/alliance_mining.py`、`tasks/packet_guard.py` | 真机跑一次即可定案 §2.4 与 `-8` |
| 1 | 恢复每日闸门 + 额度为 0 时不发包 + 占满即停 | `tasks/alliance_mining.py` | 改动 A |
| 2 | 每坑最多占 1 个槽位 | `tasks/alliance_mining.py` | 改动 B |
| 3 | 连续同错误码 → 停止本轮 | `tasks/alliance_mining.py` | 改动 C |
| 4 | 汇总按原因/错误码聚合，明细降 DEBUG | `tasks/alliance_mining.py` | 改动 D |
| 5 | 汇总里带名册来源（`live` / `snapshot`） | `tasks/alliance_mining.py` | 顺带排查 §一 的"1 级"疑点 |
| 6 | 修正被误读的注释（`teamLimit` 就是每日配额；`NewPitTeamLimit` 是槽位不是配额） | `models/alliance.py`、`tasks/alliance_mining.py` 模块头与 §改动2 段 | **防复发**，上一轮的错误结论还在代码里 |
| 7 | 测试：闸门 / 每坑 1 个 / 失败即停 / 聚合文案 | `tests/test_tasks_alliance_mining.py` | 见 §五 |
| 8 | 同步安卓副本 | `tools/sync_android_assets.py` | 改了 `tasks/`、`models/` 之后 |

---

## 五、验证方式

**离线（不需要真机）**

1. `allowance = teamLimit - already_mine`：造 `teamLimit=4, already_mine=1` 的状态，
   断言**最多只发 3 次** 21043；`already_mine=4` 时**一次都不发**。
2. `teamLimit=0`（服务端没下发）时**不阻断**，仍然尝试并发 WARNING。
3. **每坑 1 个**：一个坑 3 个空槽 + 名册够 3 个匹配角色 → 只发 **1** 次；
   且**我已经占了的坑**要整坑跳过。
4. **失败即停**：替身连续回两次同一个错误码 → 断言后续不再发 21043，且错误码原样上报。
5. **文案聚合**：33 条 unfillable + 15 条失败 → 汇总里是**聚合数字**，
   明细不出现在 INFO 级（可用现有的日志级别断言）。
6. 全量 `pytest` 必须 0 失败。

**真机（改动 0 之后立刻跑一次）**

7. `python main.py run alliance_mining_team`，核对三件事：
   - `already_mine` 与"我在坑里的槽位数"一致（**闸门的地基**）；
   - `teamLimit - already_mine` 是否等于本轮成功次数；
   - 是否还出现 -8；若出现，21044 的转储里服务端到底回了什么。
8. `python main.py run roles`，看 `source` 与角色等级分布，排查 §一 的"1 级"疑点。

---

## 六、需要你拍板的点

1. **改动 F（跨运行记住"角色已放置"）做不做？**
   建议**先不做** —— 先让 A/B/C 上线，看真机还剩多少失败；如果剩下的失败都是
   "角色已在别处"，再补 F。
2. **额度为 0 时的行为**：直接跳过（`ok=True`，报"今日次数已用尽"），
   还是算任务失败（`ok=False`）？
   建议 **`ok=True` + 说明** —— 与"未入会不算失败"同一哲学，配额用尽也是正常状态。
3. **改动 C 的阈值**：连续 2 次同错误码就停，还是 3 次？（我倾向 2：本次 15 次里有 14 次是白发的。）
4. **`-8` 要不要写进 `ERROR_CODE_NAMES`**：我建议**暂不写**（未证实语义）。
   等改动 0 的转储拿到证据，再决定叫它什么。

---

## 七、一句话交接

> **团体矿的三个限制客户端都自己判了，我们一条都没判**：
> ① 每日总次数 `TeamCount < TeamLimitCount`（`teamLimit` 就是它）；
> ② 每坑我最多 1 个；③ 角色不能已在别的坑。
> 上一轮把 `teamLimit` 误读成"按坑限制"从而删掉闸门 —— 那条结论要**撤回**。
> 个人矿的 `-2` 是另一回事（角色条件未建模，Q-044），本次不动。

---

## 八、实施记录（2026-10-06 凌晨落地）

**你拍板的四点**：① 跨运行记住"角色已放置"**先不做**；② 额度用尽**跳过**
（`ok=True`）；③ 连续 **2** 次同错误码就停，**且运行视图里不出现错误码**；
④ `-8` **不**写进 `ERROR_CODE_NAMES`。
另追加一条口径：**工会挖矿最多只能占 4 个坑位**。

### 8.1 改了什么

| 文件 | 改动 |
|---|---|
| `tasks/alliance_mining.py` | 新增 `CONSECUTIVE_FAILURE_STOP=2`、`MAX_TEAM_PITS_PER_DAY=4`、`UNFILLABLE_REASON_LABELS`、`_MAX_LISTED_DETAILS=3`；新增 `_my_slots()` / `_team_allowance()` / `_aggregate()`；`_join_slot()` 改为返回 `(ok, 文案, 错误码)`；团队矿主流程重写（配额闸门 → 每坑 1 个 → 失败即停 → 聚合汇总） |
| `models/alliance.py` | `teamLimit` / `personalLimit` 注释改成实证结论；**明确写出上一轮的误读与后果**（防复发） |
| `tests/test_tasks_alliance_mining.py` | 改写 2 条"编码了错误决策"的用例（`test_daily_quota_caps_the_number_of_joins` / `test_exhausted_quota_skips_without_sending`）；新增 7 条（每坑 1 个 / 跳过已占坑 / 连续 2 次即停 / 运行视图无裸错误码 / 汇总聚合 / `teamLimit<=0` 不阻断 / 离谱上限夹到 4）；夹具 `team_limit` 默认值 99 → **4**（真实值） |

### 8.2 行为对照（改前 → 改后）

| 场景 | 改前 | 改后 |
|---|---|---|
| 额度用尽（已占 == 上限） | 照发 21043，全被拒 | **一个占坑包都不发**，`ok=True` 说"今日团队矿次数已用尽（上限 4，我已占 4）" |
| 一个坑 3 个空槽、名册够 3 个角色 | 连发 3 次（后 2 次必被拒） | **只发 1 次**，占成就换下一个坑 |
| 我已经占了某个坑 | 还会去试那个坑的空槽 | 整坑跳过 |
| 连续被同一理由拒绝 | 一直发到没有候选为止（真机 15 发） | **第 2 发之后停手** |
| 运行视图里的错误码 | 每条 `⚠ ✘ 占坑失败 errorCode=-8` | **不出现**；只报"占坑被拒 N 次"，码进 DEBUG 与 `data["join_error_codes"]` |
| 33 条 unfillable | 33 行明细 | `无法占坑 33 个（名册里没有这个角色 ×18、等级不足 ×14、…）；例：…（另 30 个见开发者日志）` |
| `teamLimit` 读不到（<=0） | —— | **不阻断**，退化为服务端裁决 + WARNING（避免静默漏做） |
| `teamLimit` 离谱地大（>4） | —— | **夹到 4 并 WARNING 留痕**（不静默少占） |

### 8.3 「最多 4 个坑位」是怎么落地的

不是写死 4，而是 **`allowance = max(teamLimit − already_mine, 0)`，再夹一道
`MAX_TEAM_PITS_PER_DAY = 4`**：

- 主判据用服务端下发的 `teamLimit`（随公会等级/职称变化，比写死准）；
- 游戏内公会表「挖礦次數」这一列 **LV1→1、LV2~3→2、LV4~6→3、LV7~10→4**，
  最大值就是 **4**；10.3 抓包实测 `teamLimit = 4`，与之吻合 ——
  所以 `teamLimit` 本身就等于你说的那个 4；
- 夹一道 4 是"服务端万一给出离谱大数时不要发疯"，且**被夹住时打 WARNING**，
  绝不静默少占。

### 8.4 测试

`tests/test_tasks_alliance_mining.py`：**33 项全绿**。
（全量回归当时受**另一个 agent 并发改同一仓库**干扰 —— 见 §8.5。）

### 8.5 ⚠️ 遗留与注意事项

1. **并发写仓库**：本次实施期间有**另一个 agent 在同时改同一个工作区**
   （`webapi/app.py` 等被其改动，`test_webapi_app.py` 反复被 SIGTERM ——
   疑似 `tools/safe_run.py --kill-all` 之类会杀进程的用例在并发跑）。
   ⇒ **合并前请在无并发时重跑一次全量 `pytest`。**
2. **改动 0（诊断）尚未做**：`teamLimit` / `already_mine` / `allowance` 已经进了
   `TaskResult.data` 与汇总文案，但**还没有"首次业务失败转储 21044 原始字节"**。
   `-8` 的语义仍未证实；若真机再出现，按 §四 改动 0 补上。
3. **改动 F 未做**（按你的决定）：跨运行的"角色已放置"不知道，靠"连续 2 次即停"兜底。
4. **个人矿未动**：`-2` 仍是 Q-044（角色条件未建模）。注意它的运行视图里**仍有**
   `errorCode=-2（未知错误码 -2）` 字样 —— 本次按你的"个人矿本次不做"没有改；
   要一并去掉说一声。
