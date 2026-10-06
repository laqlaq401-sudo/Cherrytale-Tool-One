# 公会团体挖矿修复方案（只作计划，不改代码）

> 2026-10-05 制定。目标：修好「团体矿永远显示没有可占的坑位」。
> 全部结论来自 `captures/工会签到+个人挖矿+团队挖矿占坑位全流程.saz` 的**实测字节**
> + `Cherrytale IL2CPP/dump.cs` 的客户端源码结构（未反汇编机器码，只读结构声明）。

---

## 一、结论先行：根因是**空槽位判据写错了**

`tasks/alliance_mining.py:474-481` 的空槽判据是：

```python
if not slot.memberPid        # 等价于 memberPid == 0
```

**实测 `memberPid` 的空值是 `-1`，不是 `0`。**

`-1` 是 truthy，所以 `not slot.memberPid` 恒为 `False` → `_empty_slots()` 恒返回空列表
→ `candidates` 恒为空 → 任务永远输出「团体矿：没有可占的坑位」。

这与用户在游戏里看到「明明有 ＋ 号可点」完全一致 —— **不是"路径找错了"，是空值哨兵值判断错了**。

### 实测证据

10.3 抓包 raw/23（21040 响应）里，18 个团体矿点共 68 个槽位，`memberPid` 取值分布：

| memberPid | 槽位数 | 含义 |
|---|---|---|
| 6209807 / 6201999 / 6212217 / 6155231 / 6142760 / 6231519 | 61 | 已被 6 位队友认领 |
| **-1** | **7** | **空槽位（可占）** |

7 个 `-1` 槽位全部落在 `endTime == -1`（未开矿）的 4 个坑里，且每个 `-1` 槽位的
`member` 字段长度恰好是 **306 B**，而所有已占槽位是 **153 / 155 / 158 / 161 / 165 B**。

额外佐证：`member` 字节解出的 `IconClass` 里，空槽位的 `playerID = -1`（字段 1）、
其它各字段也是 `-1` 哨兵 —— **整条记录是"默认头像模板"，playerID 同样用 -1 表示"没人"**。

### 抓包链路自证

`-1` 判据能完整解释抓包里的占坑流程：

| 会话 | 请求（21043） | 该槽位在 raw/23 里的状态 |
|---|---|---|
| raw/84 | `type=1, pitSid=205294527, teamPitIndex=2` | raw/23 中 `[2] memberPid=-1`（空）→ 占 |
| raw/86 | `type=1, pitSid=205294519, teamPitIndex=1` | raw/23 中 `[1] memberPid=-1`（空）→ 占 |

**两次占坑都精准落在 `memberPid == -1` 的槽位上** —— 这就是"点 ＋ 号"对应的报文。
而在 raw/84 的响应里，这两个槽位的 `memberPid` 变成了 `10000006`（= 自己），闭环。

---

## 二、顺带被验证的另一件事：**不需要额外抓角色信息**

用户判断正确。21040（`AllianceNewPitRes.teamList[]`）单次响应就已包含：

- `needRoleInfoID`（槽位要求的角色模板 ID）、`needRoleLv`（等级门槛）；
- 每个槽位**当前是否空着**（`memberPid == -1`）。

也就是说「点开挖矿界面就能知道能占什么坑」这件事，**在 21040 一个响应里就是完整信息**。
5012 名册只用来回答"我手上哪个角色能满足 `needRoleInfoID` + `needRoleLv`"，
**不需要为占坑再单独抓一份角色数据**。

> 注：测试里 `roster_payload` 是造出来的假名册（项目里所有 SAZ 都没抓过 5012，
> 全量扫过 19 份 `captures/*.saz`，5011/5012 零出现）。这不影响修复 —— 5012 是只读拉取、
> 字段已由 dump.cs 确认，属"从未真机验证"的历史欠账（`models/role.py` docstring 已自认），
> 建议本次真机跑一次时顺带核一眼。

---

## 三、次要问题：坑位排序键也用错了数据源

### 3.1 `teamLimit` 不是"可占坑数上限"

`tasks/alliance_mining.py:528-537` 把它当配额：

```python
allowance = max(state.teamLimit - already_mine, 0)
```

实测 `teamLimit = 4`，而抓包这次占了 **2 个**坑，且任务里 `already_mine == 0`
（占坑前没人占）。按现值，`allowance = 4` —— **恰好没触发 bug**，但语义是错的。

**dump.cs 佐证（`AllianceNetWorkModule.NewPit`，TypeDefIndex 11877）**：客户端把
`teamLimit` 建模为**每个坑独立的**「该坑还能进几个人」限制：

```csharp
private List<AllianceNetWorkModule.NewPitTeamLimit> m_lzt_newPitLimit_Team;
public bool IsJoinPit_Team(NewPit iNewPit) { }
public int  GetRoleJoinPitCount_Team(int iPitSid) { }
```

是 `List<...>`（按坑），不是 `int`（全局）。所以 21040 的 `teamLimit` 需要重新定位语义
（很可能是"今日可参与团体矿的次数"或冗余字段）。

### 3.2 `_occupy_priority` 的第二个键方向是错的

`tasks/alliance_mining.py:483-490`：

```python
return (len(self._empty_slots(pit)), -len(pit.detaliClassList))
```

按抓包实测，**槽位数严格等于模板表 `roleMaxCondition`，而它只由难度决定**：

| 实际坑名（raw/23） | 模板 ID | 难度 | 槽位数 | 表 `roleMaxCondition` |
|---|---|---|---|---|
| 牛刀小試 | 936000001 / 936000004 | 1 | 2 | 2 |
| 初露鋒芒 | 936000011 / 936000020 | 2 | 3 | 3 |
| 畫龍點睛 | 936000027 / 936000029 | 3 | 4 | 4 |
| 爐火純青 | 936000032 / 936000039 | 4 | 5 | 5 |
| 縱橫捭闔 | 936000043 / 936000047 / 936000048 | 5 | 6 | 6 |

18/18 全部吻合。所以「总坑位多 = 奖励多」需要一个前提：**同难度内的多个坑才是同奖励、
可比的**。跨难度按槽位数排（骰 6 槽的縱橫捭闔 > 2 槽的牛刀小試）是**把高难度坑排在前面** ——
奖励确实更多，但没有考虑自己名册是否够得着高难度的等级门槛（实测空槽 `needRoleLv`
最高到 **50**）。

**结论：排序不是本次故障原因（故障在 `_empty_slots`），但既然要动这块，建议一并正名。**

---

## 四、附带发现（不改也不算错，记录备查）

1. **空槽位的 `needRoleInfoID` / `needRoleLv` 是"真要求"，不是 0**

   7 个空槽的实测要求：`needRoleLv ∈ {38, 43, 44, 46, 47, 48, 50}`，
   `needRoleInfoID ∈ {133003000, 133007400, 133008400, 133011300, 133013400, 133015900, 133016200}`。

   这与截图一致（灰坑上写着「37」「41」「SSR」，绿框 ＋ 号坑写着「89」）。
   **客户端 `PitRoleIcon.CheckCanJoinRole()` 就是拿这个 `needRoleInfoID` 去名册里找角色** ——
   现实现（`role.roleID == slot.needRoleInfoID and role.roleLV >= slot.needRoleLv`）
   在思路上是对的。**唯一要留意**：`roleSid` 身份正确性从未真机验证，
   若真机出现"有匹配角色却被拒"，第一嫌疑就在 5012 解码（见 `models/role.py` 的 ⚠️ 说明）。

2. **`AllianceNewPitSetRoleRes` 没有 `teamLimit` 字段**
   （只有 `errorCode` / `personalList` / `teamList`）—— 所以占坑过程中无法刷新配额，
   只能每轮重新拉 21040。

3. **抓包里两次 21043 之间没有重新拉 21040**，而两次占坑分别在不同坑
   （`sid=205294527` 与 `sid=205294519`）—— 说明客户端是"进界面前拉一次，
   之后靠 21044 响应里的 `teamList` 增量维护"。本项目每次占坑都从 21040 重新算，
   更保守、无副作用，可保持。

---

## 五、修复计划

### 改动 1（核心 · 必须）：修正空槽判据

**文件**：`models/alliance.py`

- `AllianceTeamPitDetailClass` 新增判据方法，把「空」的语义收口到一处：
  ```python
  #: 槽位无人认领时的 memberPid 哨兵值（★ 2026-10-05 抓包实证：-1，不是 0）
  NO_MEMBER_PID: Final[int] = -1

  def is_empty(self) -> bool:
      """该槽位是否无人认领（可占）。"""
      return self.memberPid <= 0
  ```
- 更新 `memberPid` 字段的 docstring（现在写的是「0 = 待认领，语义待实测」）。
- `slots_for_member()` 保持 `== player_id`（自己一定是正数；`player_id` 为 0 时
  不该误判空槽为自己的，故不要改写成 `<=`）。
- 更新 `AllianceNewPitRes.teamLimit` 的语义注释。

**文件**：`tasks/alliance_mining.py`

- `_empty_slots()` 改为调用 `slot.is_empty()`。
- `already_mine` 统计（:531-536）改为 `slot.memberPid > 0`（语义等价，可读性更好）。
- 模块 docstring 里多处「`memberPid == 0`」的表述全部改为 `-1`。

> 用 `<= 0` 而不是 `== -1` 是有意的：哨兵值万一在不同服务端版本是 `0`，
> 两种都能覆盖；而 `> 0` 判定"已占"在两种约定下都成立。

### 改动 2（建议 · 一并做）：占坑配额改用可行判定

- 把 `allowance = teamLimit - already_mine` 的**硬闸门**降级为**观察值**：
  `teamLimit` 仍然是"今日次数"，但一旦发满次数服务端会回错误码（`34 Re_Request_MiningPit`
  或类似），脚本按错误码原样上报即可，不再本地猜一个可能算错的配额。
- 保留一个**防御性上限**（例如 `allowance = max(state.teamLimit, 1)` 或直接不设限），
  避免一次运行把 18 个坑全占 —— 具体形式待你拍板。

### 改动 3（建议 · 一并做）：排序键正名

- 主键保持「空槽少优先」（抓包与用户描述的"越接近满员越可能自动开矿"互证，方向正确）。
- 次键改为**难度（模板表 `difficulty`）从高到低**，而不是槽位数 —— 语义更准，
  等价于"同空槽数下优先奖励多的"，且可读。
- 需要在 `models/game_config.py` 增加 `load_team_pit_conditions()`（读
  `AllianceTeamPitData` 的 `teamPitID` / `title` / `difficulty` / `roleMaxCondition`），
  与既有 `load_personal_pit_conditions()` 并列。
- 若你不想引入新表读取，次键也可以直接改为 `-len(detaliClassList)` 保持不变
  （因为槽位数 = roleMaxCondition = f(difficulty)，等价）—— 但要在注释里讲明这层等价关系。

### 改动 4（测试）：加字节级回归钉子

**文件**：`protocol_samples/alliance_samples.py`（**由工具生成，禁止手改**）

用 `tools/export_saz_samples.py` 追加 raw/23、raw/84、raw/86 三个会话：

```bash
PYTHONIOENCODING=utf-8 python tools/export_saz_samples.py \
  --match '工会签到' --sessions raw/23,raw/84,raw/86 --prefix ALLY \
  --out protocol_samples/alliance_samples.py
```

> ⚠️ 注意：该命令会**覆盖** `alliance_samples.py`，必须把 `--sessions` 写成
> **raw/19,raw/21,raw/30,raw/23,raw/84,raw/86 全量**，否则会丢掉现有的签到/个人矿夹具。
> 建议先 `--out /tmp/x.py` 跑一次 diff 确认。

**文件**：`tests/test_capture_1003_regression.py` 新增 `TestAllianceTeamMiningCapture`：

- `test_team_pit_empty_slot_member_pid_is_minus_one`：
  解 raw/23 的 `ALLY23_RESPONSE_HEX`，断言
  `{(p.sid, i) for p in ... for i,s in ... if s.memberPid == -1}` ==
  `{(205294527,1),(205294527,2),(205294522,1),(205294522,3),(205294516,0),(205294516,3),(205294519,1)}`；
  并断言**没有任何槽位** `memberPid == 0`。
- `test_team_join_request_matches_client_bytes`：
  `AllianceNewPitSetRolePacket(type=1, pitSid=205294527, roleSidList=[155242087], teamPitIndex=2).encode()
   == sub(ALLY84_REQUEST_HEX)`（含 `include_defaults=True` 形态）；
  raw/86 同理（`roleSidList=[223860075]`, `teamPitIndex=1`）。
- `test_join_target_slot_was_empty_in_state`：
  交叉验证 —— raw/84 请求里的 `(pitSid, teamPitIndex)` 在 raw/23 状态里 `memberPid == -1`。
  这是**从抓包反推"客户端认为什么算空"**的最强钉子。
- `test_team_pit_slot_count_equals_role_max_condition`：
  18 个坑的槽位数与 `AllianceTeamPitData.roleMaxCondition` 逐一相等（顺带钉死模板表读取）。

**文件**：`tests/test_tasks_alliance_mining.py`

- 现有 `slot_bytes(..., member_pid)` 的调用点大量传 `0` 表示空槽 —— 需**全量改为 `-1`**
  （约 10 处，见 :574/:576/:635/:644/:681/:682/:722/:759/:760/:826/:846 附近）。
- `test_fills_empty_slot_with_matching_role` 等用例的 docstring 里「memberPid==0」改 `-1`。
- **建议保留一个 `member_pid=0` 的用例**，断言它也被当作空槽（`<= 0` 的兼容性），
  防止将来把判据收紧成 `== -1` 时无声回退。

**文件**：`tests/test_models_alliance.py`

- 新增 `AllianceTeamPitDetailClass.is_empty()` 的单元测试（-1 / 0 / 正数三态）。

### 改动 5（文档）

- `notes/open_questions.md` 新增条目：`memberPid` 空值 = **-1**（本次实证），
  以及 `teamLimit` 语义待定（客户端建模为按坑的 `List<NewPitTeamLimit>`）。
- 视情况把 `notes/recon_findings.md` 的 §21.11 补充实测表。

### 改动 6（同步 Android 副本）

`android/app/src/main/python/` 是打包脚本**拷贝**的副本，改动 1/3 落地后需重跑同步
（脚本 `tools/sync_android_assets.py`，或按 `一键打包.bat` 的既有流程）。

---

## 六、验证方式

1. **纯本地（不需要真机）**：
   ```bash
   PYTHONPATH="C:\Users\Anqi Liu\Desktop\Cherrytale tool One\.venv\Lib\site-packages" \
     "/c/Users/Anqi Liu/AppData/Local/Python/pythoncore-3.14-64/python.exe" -m pytest -q
   ```
   目标：现有 `1534 passed` 基础上，新增钉子全绿。
2. **离线重放抓包**：用 raw/23 的响应喂给 `AllianceMiningTeamTask`，
   断言它**识别出 4 个可占坑、7 个空槽**（不需要真机就能证明"不再永远显示没有可占的坑位"）。
   这是本次修复最直接的成功判据。
3. **真机**：跑 `alliance_mining_team` 看是否真的发出 21043、返回 `errorCode ∈ {0,1}`，
   顺带核一眼 5012 的 `roleSid`/`roleID` 解码是否正确。

---

## 七、需要你拍板的点

1. **改动 2 的配额处理**：彻底去掉本地 `teamLimit` 闸门（完全听服务端），还是保留一个宽松上限？
2. **改动 3 的排序次键**：引入 `AllianceTeamPitData` 读取拿 `difficulty`，还是维持现状只改注释？
3. **改动 4 的夹具再生成**：是否接受 `alliance_samples.py` 全量重生（需先 diff 确认不丢现有夹具）？
