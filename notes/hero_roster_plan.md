# 英雄（角色）数据获取 + 工会挖矿名册匹配修复方案

> 2026-10-05 制定（先计划）→ **同日已按 §三 落地（见 §八 实施记录）**。
> 触发问题：① 前端要新增「角色查看」；② 工会团体挖矿永远报「名册中没有可用的匹配」。
>
> 本文所有结论都标了证据来源；凡是推断的都显式写了「待真机确认」。

---

## 零、结论速览

| # | 结论 | 证据强度 |
|---|---|---|
| 1 | `英雄查看.saz` 里**一条游戏网关接口都没有** —— 打开英雄界面**不发请求** | 实测（全文件 4 个会话，无 `game-ct-labs.ecchi.xxx:1893`） |
| 2 | 英雄数据 = **1004 `LoginRes.roleInitClass`（字段 5）**，与背包 `itemInitClass`(6)、装备 `equipInitClass`(4) **同一个包** | dump.cs 类定义 + 生成表字段号，已离线验证 |
| 3 | **工会挖矿报错的根因不是"没拉到名册"** —— 名册**拉到了** | 21047 失败请求转储里有 31 个真实且互不重复的角色 sid |
| 4 | 真正的根因：**匹配键用错了 ID 空间**。`RoleClass.roleID` 是 **RoleMainID(130xxxxxx)**，`needRoleInfoID` 是 **RoleInfoID(133xxxxxx)**，两者**永不相等** | 反汇编 `AddServerRole` 实证 + 配置表 ID 区间实证 |
| 5 | 映射关系已跑通：`RoleMainID →(qualityUpRoleSubID 链回溯)→ 基础 MainID →(RoleInfoData.initialRoleMainID)→ RoleInfoID`；对日志里 7 个空槽需求 **7/7 命中** | 离线脚本验证 |

**一句话**：前端要的英雄数据在 1004 里（登录就白拿）；挖矿的锅不在名册，在 ID 换算。

---

## 一、侦察过程（三条线索，逐条交代）

### 1.1 线索一：`captures/英雄查看.saz` —— 没有任何游戏接口

解包后只有 **4 个会话**：

| # | 内容 | 大小 |
|---|---|---|
| raw/1 | `GET pfdd.88kongque.com/.../notosanscjksc-black sdf.…`（CDN 字体） | 响应 30,162,305 B |
| raw/2 | `GET .../assets_resources_…mainui_role_hero_plane_particle system rtcamera.…`（**英雄界面 UI 预制体**） | 响应 268,813 B |
| raw/3 | `CONNECT a.hyenadata.net:443`（埋点隧道） | — |
| raw/4 | `POST a.hyenadata.net/api/trace/request`（埋点 `liveness`） | 响应 2 B |

抓包时间窗：`12:15:40 ~ 12:17:36`（1 分钟内）。

**关键点**：同一台机器、同一个 Fiddler 的工会抓包（`工会签到+…全流程.saz`，93 个会话）里，
游戏网关是 `POST https://game-ct-labs.ecchi.xxx:1893/`，一共 8 条 —— 说明 Fiddler **是能拍到网关的**。
而英雄抓包里 0 条。

**结论**：打开英雄界面时**客户端没有向服务端要英雄数据**。数据只能是之前某个包给的。

> ⚠️ 待真机确认：有可能"打开界面那一下"恰好在抓包窗口之外。但从 raw/2 正好是
> **英雄界面预制体**的下载来看，抓包窗口是覆盖了这次界面打开的。

### 1.2 线索二：回查登录抓包 —— 1004 响应体被 Fiddler 省略了，但能确定两件事

用项目自带 `tools/extract_har.py --list` 跑 `login.har` + `cherrytale-fiddler-1.har`，
再手工解码 `1003-login-zone.har`，得到登录后的完整包序：

| 顺序 | 请求字节 | 响应字节 | 响应包号 | 包名 |
|---:|---:|---:|---:|---|
| 1 | 88 | 650 | 99016 | VersionControlServerRes（握手） |
| 2 | 330 | 8872 | 1002 | AccountClientInfoLoginRes |
| 3 | 178 | **85162（被省略）** | **1004** | **LoginRes** |
| 4 | 192 | 24646 | 1010 | LoginOtherDataPacketRes |
| 5+ | — | — | 31004 / 33056 / 32006 / 33058 / 11026 / 99024 / 21026 / 27004 / 11012 / 14012 | 各种初始化 |

**两件事**：

1. **1004 的响应体是 85,162 字节，但 Fiddler 存 HAR 时把大二进制体省略了**
   （`content.text` 为 `null`，只剩 `content.size`）。所以现有抓包**无法**做 1004 的字节级验证。
2. **客户端登录后没有发 5011**（`GetAllRolePacket`）。全量扫 19 份 SAZ + 3 份 HAR，
   5011/5012 **零命中**（与 `notes/open_questions.md` Q-057 一致）。

把这两点合起来：**客户端在登录后不需要再拉名册**，因为 1004 已经给了。这与 1.1
（打开英雄界面不发请求）**互相印证**。

### 1.3 线索三：逆向索引 —— 谁承载 `List<RoleClass>`

`Cherrytale IL2CPP/dump.cs` 里只有 4 个类持有 `List<RoleClass>`：

| 类 | 字段 | 包号 | 说明 |
|---|---|---|---|
| **`LoginRes`** | **`roleInitClass`** | **1004** | **进区登录响应** |
| `GetAllRoleRes` | `roleList` | 5012 | 专用名册包（客户端进界面才用） |
| `GetObjClass` | `roleList` | — | 非本需求 |
| `WorldBossRankClass` | `roleClass` | — | 非本需求 |

`LoginRes` 的完整字段表（生成表 `models/proto_fields.py`，编号 = 声明顺序，已离线验证）：

```
1 errorCode          2 playerInitClass     3 myIconInfo
4 equipInitClass     5 roleInitClass  ★    6 itemInitClass   ← 背包
7 teamInitClass      8 astrolable          9 eventDeadlineID
10 sysActClass      11 goddessInitClass    ...
```

> 这正好解释了你说的「**既然背包数据都能找到**」：背包（`itemInitClass`）和英雄
> （`roleInitClass`）在**同一个 1004 响应**里，只是 `models/server.py` 的 `LoginRes`
> 当年只声明了 1/2/3 三个字段，后面的**全被解码器当"未知字段"跳过了**
> （`game_packet.py` 的 decode 对未声明编号只记 debug 日志）。

**而且**：`models/server.py` 只声明 3 个字段不会报错，是因为 `LoginRes` **只解码、不编码** ——
`encode()` 里的"字段必须齐全"校验碰不到它。所以这个遗漏一直没被发现。

### 1.4 关键反汇编：`RoleClass.roleID` 到底是哪个 ID

`AllianceTeamPitDetailClass.needRoleInfoID` 名字里就写着 RoleInfoID，
而 `models/role.py` 的注释把 `RoleClass.roleID` 写成"角色模板 ID"，并断言
`role.roleID == slot.needRoleInfoID`。这两者是不是一个东西？反汇编判定：

`RoleMainModule.AddServerRole(RoleClass iRoleClz, bool)`（Offset `0x19C2040`）：

```asm
mov r14d, dword ptr [rsi + 0x10]   ; RoleClass.roleSid  (dump 偏移 0x10 ✓)
call 0x1819cd5f0                   ; = IsOwnedRolebySID(roleSid)
mov ebp,  dword ptr [rsi + 0x14]   ; RoleClass.roleID   (dump 偏移 0x14 ✓)
mov r8,   qword ptr [rdi + 0x60]   ; this.m_hashSet_RoleMainID
mov edx,  ebp
call 0x1819d0380                   ; = isExistInHashSet(roleID, m_hashSet_RoleMainID)
```

`RoleMainModule` 的字段表（dump.cs 506919 起）：

```
0x60 m_hashSet_RoleMainID      ← roleID 查的是这个
0x68 m_hashSet_RoleSID
0x70 m_hashSet_RoleInfoID
```

**⇒ `RoleClass.roleID` 是 `RoleMainID`，不是 `RoleInfoID`。**

配置表 ID 区间实证（读 `Cherrytale Asset/TextAsset/`）：

| 表 | 主键 | 区间 | 行数 |
|---|---|---|---|
| `RoleInfoData` | `roleID` | **133000001 ~ 133991800** | 587 |
| `RoleMainData` | `roleMainID` | **130000001 ~ 130954009** | 6653 |

日志里 7 个空槽的 `needRoleInfoID ∈ {133003000, 133007400, 133008400, 133011300,
133013400, 133015900, 133016200}` —— 全在 **133xxxxxx**，而名册里的 `roleID` 在
**130xxxxxx**。**`==` 永远为假 ⇒ 每一个槽位都判"名册中没有可用的匹配"**，与日志完全一致。

### 1.5 名册其实是拉到了 —— 21047 转储实证

`tmp/failed-request_21047_20261004-134645.hex`（274 B）解出 7 个矿点元素：

| 元素 | 矿点 sid | 模板 id | roleSidList |
|---:|---:|---:|---|
| 0 | 216697069 | 935000114 | 2 个 |
| 1 | 216697073 | 935000117 | 3 个 |
| 2 | 216697071 | 935000118 | 3 个 |
| 3 | 216697068 | 935000126 | 5 个 |
| 4 | 216697070 | 935000131 | 6 个 |
| 5 | 216697072 | 935000132 | 6 个 |
| 6 | 216697067 | 935000136 | 6 个 |

共 **31 个角色实例 sid，互不重复**（如 148773605 / 148993685 / 158309294 …），
且每坑人数**恰好等于** `AlliancePersonalPitData.roleMaxCondition`。

**这些 sid 只有一个来源：5012 名册。** 若名册为空，`_build_one_key_elements` 会走
`notes.append("…名册只凑到 0 个 —— 已跳过")` 分支、**根本不会发 21047**。
既然 21047 发出去了，名册必然非空。

> 顺带修正一处**日志文案错误**：个人矿的汇总文案是
> `parts` 为空时输出「个人矿：**没有可收的矿，也没有空闲矿点**」，但 `parts`
> 只统计"收/开/挖掘中"三种动作，**不反映 `idle` 是否为空**。所以
> 「也没有空闲矿点」这句在"有闲矿但开矿被拒"时是**假的**，会把人往"没有矿点"上带。
> 与项目既有「运行视图纪律」不符，建议一并修。

---

## 二、要做的三件事

### A. 英雄数据接入（新功能 · 前端「角色查看」用）

**数据源：1004 `LoginRes.roleInitClass`（字段 5）。**

理由：
- **零额外请求**：登录本来就会收到 1004；
- **结构可信**：`roleInitClass` 的字段号由生成表给出，`RoleClass` 9 个字段全部有 dump.cs 声明；
- **客户端行为一致**：登录后不发 5011、打开英雄界面也不发请求（1.1 / 1.2）。

**改动点**：

1. `models/server.py` 的 `LoginRes` 补声明：
   - **必需**：`roleInitClass: list[RoleClass]`（字段 5）
   - **建议一并**：`equipInitClass`(4)、`itemInitClass`(6)、`teamInitClass`(7)
     —— 同一次改动、同一种风险，且背包/装备将来也要用。
   - ⚠️ `RoleClass` 从 `models/role.py` 导入；已离线验证**无循环依赖**
     （`models/role.py` 只依赖 `models.game_packet`，`models/server.py` 目前不导入 role）。
2. `tasks/login.py`：把 `login_res.roleInitClass` 收进 `TaskResult.data`
   （`role_count` + 精简后的角色列表），并**落到会话文件**，供跨进程复用。
3. 新增 `tasks/roles.py`（只读任务，名字 `roles`）：
   - 首选**发 5011** 拿实时名册（客户端语义一致）；
   - 5011 拿不到/为空时，**回退到登录时存下的 `roleInitClass` 快照**；
   - 输出：`roleSid / roleID / RoleInfoID / 名称 / 等级 / 星级 / 品质 / 元素`。

> **为什么不只依赖登录快照**：工具跨进程复用 `.session.json` 时**不会重新登录**，
> 那时没有 1004。所以"实时 5011 + 快照兜底"两条腿都要。

### B. 工会挖矿匹配修复（真 bug）

**根因**：`tasks/alliance_mining.py` 里 `role.roleID == slot.needRoleInfoID`（:615），
两个不同 ID 空间直接比。个人矿（:252 按等级排序）同样没有按矿点条件筛角色，
大概率就是 `errorCode=-2` 的来源（**待真机确认**）。

**修法**：新增一张 `RoleMainID → RoleInfoID` 映射，匹配前先换算。

映射算法（**已离线验证，对日志 7 个需求 7/7 命中**）：

```
1. 建反向索引 parent[qualityUpRoleSubID] = roleMainID      （读 RoleMainData）
2. 建 init2info[initialRoleMainID] = [roleID, ...]          （读 RoleInfoData）
3. base(mainID) = 沿 parent 回溯到链首
4. roleInfoID(mainID) = init2info[base(mainID)]
```

验证样例：

| 角色 roleMainID | base | RoleInfoID |
|---|---|---|
| 130003000 | 130003000 | 133003000 |
| 130003002（品质升级形态） | 130003000 | 133003000 |
| 130007400 | 130007400 | 133007400 |

**落地位置**：`models/game_config.py` 新增 `load_role_info_mapping()`（与既有
`load_personal_pit_conditions()` / `load_team_pit_conditions()` 并列）。

**顺带要一起改的**：

- `models/role.py` 的 docstring：`roleID` 的说明从"角色模板 ID（13xxxxxx 段）"
  改成 **"角色 RoleMainID（130xxxxxx 段）；要跟矿点的 needRoleInfoID 比对，
  必须先经 `models/game_config.load_role_info_mapping()` 换算"**。
  —— 这条注释是**本次 bug 的源头**，必须改，否则下次还会踩。
- `AllianceTeamPitDetailClass` 的 `needRoleInfoID` 注释同步说明是 RoleInfoID。
- 个人矿：`_build_one_key_elements` 增加**按矿点条件筛角色**（`AlliancePersonalPitData`
  的 `elementCondition` / `qualificationCondition` / `lvCondition` / `starCondition` /
  `qualityCondition` / `raceCondition` / `jobCondition`）。**先只做 element + lv**，
  其余留观察 —— 这一步是**独立子任务**，建议与 A/B 分开做，别混在一起验证。
- `tasks/alliance_mining.py` 个人矿汇总文案（见 1.5 的文案错误）。

### C. 前端「角色与资源」面板（`web/`）

现状：`web/index.html` 的 `#panel-info` 标题就是**「角色与资源」**，
`renderInfo()`（`web/assets/app.js:296`）已渲染三组卡片：
核心资产 / 日常与竞技 / 进度与探索。

**建议**：在 `renderInfo()` 里**新增第 4 组「我的角色」**，数据来自
`GET /api/player`（`webapi/app.py:512 player_info`）的聚合结果。

- 后端：`player_info()` 的 `read_tasks` 里加一条 `runner.TaskRequest(name="roles")`；
- 返回体加 `"roles": {...}`（数量、按品质/等级排序的列表）；
- 前端：新增 `statCard("角色数量", …)` + 一个角色网格（名字/等级/星级/品质/元素）。
- 角色**中文名**从 `RoleInfoData.name` 取（繁体），与项目「道具一律翻中文名」的纪律一致。

---

## 三、分步 TODO（建议顺序）

### 阶段 0：一次性诊断（不改生产代码，**必须先做**）

目的：把"推断"变成"实测"，避免白改。

- [ ] 0.1 真机跑一次登录，把 **1004 响应原始字节**存到 `tmp/`（HexView 或加一次性 dump），
      用 `describe_message()` 确认字段 5 真的是 `roleInitClass` 且**非空**。
- [ ] 0.2 真机跑一次 5011，dump 5012 原始字节，确认 `roleList` 非空、
      **并记录 `roleID` 的实际取值范围**（预期 130xxxxxx）。
- [ ] 0.3 交叉验证：名册里的 `roleID` 经映射后，能否命中 21040 里的 `needRoleInfoID`。

> 0.2 的 roleID 区间是**整个方案的地基**。若实测发现 roleID 已经是 133xxxxxx，
> 那么 1.4 的结论就要重判（那意味着 5012 的 `roleID` 与 `LoginRes.roleInitClass`
> 的 `roleID` 语义不同）—— 这一步**不要跳**。

### 阶段 1：英雄数据（A）

- [ ] 1.1 `models/server.py`：`LoginRes` 补 4 个字段（5 必需，4/6/7 建议）
- [ ] 1.2 `tasks/login.py`：`roleInitClass` 进 `TaskResult.data` + 会话文件
- [ ] 1.3 `tasks/roles.py`：新只读任务（5011 实时 + 快照兜底）
- [ ] 1.4 `models/session_state.py`：加 `roles` 快照字段（可选）

### 阶段 2：挖矿匹配（B）

- [ ] 2.1 `models/game_config.py`：`load_role_info_mapping()`
- [ ] 2.2 `tasks/alliance_mining.py`：团队矿匹配改用 `roleInfoID(role.roleID)`
- [ ] 2.3 `models/role.py` / `models/alliance.py`：修正注释（**防复发**）
- [ ] 2.4 `tasks/alliance_mining.py`：个人矿汇总文案修正
- [ ] 2.5（独立）个人矿按 element/lv 筛角色

### 阶段 3：前端（C）

- [ ] 3.1 `webapi/app.py`：`player_info()` 加 `roles` 任务与返回体
- [ ] 3.2 `web/assets/app.js`：`renderInfo()` 新增「我的角色」组
- [ ] 3.3 `web/index.html` / CSS：角色网格样式（若需要）

### 阶段 4：同步与回归

- [ ] 4.1 `py -3.14 -u tools/sync_android_assets.py`（改 `webapi/`、`models/`、`web/` 后**必须**）
- [ ] 4.2 全量 `pytest`

---

## 四、验证方式

**离线（不需要真机）**

1. **映射表单测**：`tests/test_game_config.py` 新增
   `load_role_info_mapping()` 用例 —— 断言
   `roleInfoID(130003000) == 133003000`、`roleInfoID(130003002) == 133003000`（品质升级形态）、
   以及日志里那 7 个 `needRoleInfoID` **全部可被某个 roleMainID 命中**。
2. **离线重放**：拿 21040 的真实响应（`protocol_samples/alliance_samples.py` 的 raw/23）
   + 一份**真实 5012 载荷**（阶段 0.2 抓到的），断言 `AllianceMiningTeamTask`
   **识别出可占坑位并成功匹配角色**。这是本次修复最直接的判据。
3. **钉住旧行为**：现有 `tests/test_tasks_alliance_mining.py` 里的 `roster_payload`
   是**编造的**（roleID=133000001，恰好在 RoleInfoID 段），所以测试**永远是绿的** ——
   这正是 bug 逃逸的原因。必须新增"roleID 必须是 130xxxxxx 段"的钉子。
4. **文案回归**：个人矿在 `idle` 非空但开矿失败时，汇总文案**不得**出现"没有空闲矿点"。

**真机**

5. 跑 `python main.py run alliance_mining_team`：预期不再出现"名册中没有可用的匹配"，
   而是发出 21043 并回 `errorCode ∈ {0,1}`。
6. 跑 `python main.py run alliance_mining_personal`：观察 21048 的 errorCode
   （`-2` 是否消失；若仍为 `-2`，按 2.5 继续补 element 条件）。

---

## 五、需要你拍板的点

1. **`LoginRes` 补几个字段**：只补 `roleInitClass`，还是把 4/6/7
   （`equipInitClass` / `itemInitClass` / `teamInitClass`）一次补齐？
   （我建议一次补齐：同源、同风险、背包/装备迟早要用。）
2. **5011 还发不发**：`tasks/roles.py` 是"5011 实时 + 快照兜底"，
   还是**干脆只用登录快照**（更省一次请求，但跨进程复用会话时数据可能陈旧）？
3. **个人矿的条件筛选（2.5）是否本次一起做**：它是独立子任务，
   一起做会让"团队矿修好了没有"这个判据变模糊。
4. **是否接受把 `notes/recon_findings.md` §21.19 里那句
   "无需新增『登录时角色清单』抓取"改掉** —— 该结论基于"内存里的角色数据来自
   登录初始化 + 5012"，方向对，但**没有指明 `roleInitClass` 这个字段**，
   容易被下次误读成"登录包里没有角色数据"。

---

## 六、风险与未验证项

| 项 | 状态 | 说明 |
|---|---|---|
| 1004 的 85 KB 响应里 `roleInitClass` 是否非空 | **未验证** | 抓包被 Fiddler 省略；阶段 0.1 解决 |
| 名册 `roleID` 的实际取值区间 | **未验证** | 阶段 0.2 解决。**这是地基** |
| `GetRoleInfoID_ByRoleMainID` 的完整实现 | 已反汇编主路径 | 读到 `RoleMainData.info`(0x20) → `RoleInfoData.roleID`(0x10)；但 `RoleMainData.info` 在**文本表里全为 NULL**，故本项目用 `initialRoleMainID + qualityUpRoleSubID` 链重建，等价性**已离线验证但未真机验证** |
| 个人矿 `errorCode=-2` 的确切原因 | **未验证** | 与名册 ID 空间错位同源的可能性最大（角色选错），也可能是 element 条件不满足 |
| 5011 是否真的可用 | **未验证** | 客户端登录后不发它；全量抓包 0 命中。阶段 0.2 一并确认 |
| 一个 RoleInfoID 对应多个 RoleMainID | 已确认 | 例：`initialRoleMainID=130000100` 对应 133000005 / 133000014 / 133000100。匹配时**任一命中即可**，需在实现里明确 |
| `RoleClass` 的 `roleID` 字段号（=2）是否被字节验证过 | **未验证** | 现有抓包无 5012；阶段 0.2 用 `describe_message()` 对照 |

---

## 七、一句话交接

> 下次要动这块，先读本文件 §零 与 §六。
> **英雄数据在 1004 `LoginRes.roleInitClass`（字段 5）；挖矿的锅在
> `RoleMainID(130xxxxxx) ≠ RoleInfoID(133xxxxxx)`，不在名册拉取。**

---

## 八、实施记录（2026-10-05 同日落地）

**你拍板的四点**：① `LoginRes` 一次补齐；② `roles` 走"5011 实时 + 快照兜底"；
③ 个人矿条件筛选本次不做（团队矿修好再说）；④ 改掉 `recon_findings.md` 的旧结论。

### 8.1 改了什么

| 文件 | 改动 |
|---|---|
| `tools/extract_proto_fields.py` | `FOCUS_CLASSES` 加 `EquipClass` / `TeamClass`（并注明它们是 LoginRes 的初始化清单） |
| `models/proto_fields.py` | **重新生成**（158→160 类、1551→1562 字段；diff 纯新增，已核对） |
| `models/server.py` | 新增 `EquipClass` / `TeamClass`；`LoginRes` 补 4 个字段（4/5/6/7）。**导入 `models.daily.ItemClass` 与 `models.role.RoleClass`**（已确认无循环依赖） |
| `models/game_config.py` | 新增 `TABLE_ROLE_MAIN` / `TABLE_ROLE_INFO` + **`RoleIdMap`** / `load_role_id_map()`；方法 `base_main_id` / `role_info_ids` / `matches` / `name_of` / `name_of_info` / `main_ids_for_info` |
| `models/role.py` | **修正错误注释**（`roleID` 是 RoleMainID，不是模板 ID）—— 这是 bug 的源头；模块头补"角色数据有两个来源" |
| `models/alliance.py` | `needRoleInfoID` 注释标明是 RoleInfoID、比对前必须换算 |
| `models/session_state.py` | 新增 `roles` 快照字段 + `role_snapshot_from()`；`SESSION_JSON_VERSION` 2→3（只影响日志提示） |
| `tasks/login.py` | 1004 的 `roleInitClass` 压成快照存进会话状态；`TaskResult.data["role_count"]` |
| `tasks/roles.py` | **新增**只读任务 `roles`（5011 实时 → 空/报错则退登录快照，结果里带 `source` / `fallback_reason`） |
| `tasks/alliance_mining.py` | 团队矿匹配改走 `RoleIdMap.matches()`；`_unfillable_reason()` **分因报缺**（没这个角色 / 等级不够 / 被本轮占用）；换算表读不到时**明确报错而不是谎称"名册里没有"**；个人矿汇总文案不再谎报"没有空闲矿点" |
| `services/task_registry.py` / `task_spec.py` | 登记 `roles`（hidden，属只读数据类） |
| `webapi/app.py` | `/api/player` 聚合 `roles`，返回体加 `roles{count,source,fallback_reason,items}` |
| `web/assets/app.js` / `input.css` / `styles.css` | 「角色与资源」面板新增第 4 组**「我的角色」**（含"来源：实时名册/登录快照"提示）；样式用 tailwind 重建（已验证重建**零漂移**） |
| `notes/recon_findings.md` | §21.19 那条"无需新增登录时角色清单抓取"**改成修正说明** |

### 8.2 测试

- **新增** `tests/test_game_config_roles.py`（12 例）：钉死"两个 ID 空间不相交"、
  "把 RoleInfoID 当 RoleMainID 传进来必须匹配不上"、品质升级形态同映射、
  抓包日志里 7 个需求全部可达。
- **新增** `tests/test_tasks_roles.py`（9 例）：实时优先 / 空快照兜底 / 报错兜底 /
  两条都空如实报缺 / 0 字节子包（真机"没角色"的形态）也要兜住 / 演练零发包。
- **改写** `tests/test_tasks_alliance_mining.py`：夹具改用**成对**的
  `ROLE_MAIN_*` / `ROLE_INFO_*` 常量 + `patch_role_id_map()`；
  新增 3 条核心回归钉子（换算必需 / 分因报缺 / 缺表不许撒谎）。
  ⚠️ **旧夹具把 RoleInfoID 塞进 `roleID` 位置**，正是 bug 逃逸的原因 —— 已记录在文件头。
- **补** `tests/test_server.py`：`LoginRes` 字段 4/5/6/7 编号 + `roleInitClass`
  解出 `RoleClass` + 未建模字段安全忽略。
- **改** `tests/test_webapi_app.py`：只读任务清单加 `roles`，并断言
  `roles.source` / `items` 透传。
- 全量 `pytest`：**0 失败**（10 项跳过，均需真机凭据）。
  （唯一一次失败是 `test_tools_safe_run.py` 的后台进程注册表竞态，单独复跑全绿，与本次无关。）
- `main.py selftest`：**环境就绪**（生成表同步检查通过）。

### 8.3 同步

`tools/sync_android_assets.py` 已跑：12 项同步完成；`RoleMainData` / `RoleInfoData`
**已随打包配置表进入 APK**（表扫描器从新增的 `TABLE_ROLE_*` 常量自动识别），
36 张表 / 9.4 MB。

### 8.4 仍未验证（**下一步该做的**）

1. **真机跑一次 `python main.py run login`**，看日志里
   "已记下角色名册快照：N 个角色" —— 确认 1004 的 `roleInitClass` 真的非空。
   若为空，那条 WARNING 就是线索（不是静默失败）。
2. **真机跑 `python main.py run roles`**，看 `source` 是 `live` 还是 `snapshot`，
   并**记下名册里 `roleID` 的实际取值区间** —— 这是全案地基。
3. **真机跑 `python main.py run alliance_mining_team`**：预期不再出现
   "名册中没有可用的匹配"，而是发出 21043 并回 `errorCode ∈ {0,1}`。
4. 个人矿的 `errorCode=-2`（§五 第 3 点，本次**故意不做**）：团队矿确认修好后再动。
