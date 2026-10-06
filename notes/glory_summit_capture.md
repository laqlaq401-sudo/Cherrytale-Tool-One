# 荣耀之巅（TopPvp）抓包档案

> 本文件记录**实测字节级结论**与复现命令，目的是：下次不必重新解一遍抓包。
> 对应实现：`models/top_pvp.py` + `tasks/top_pvp.py`（任务名 `top_pvp_battle`）。

## 一、这份档案回答的核心问题：「谁算战斗？」

| 板块 | 结算方 | 实测证据 |
|---|---|---|
| **荣耀之巅** | **服务器** | `39007` 请求体 **0 字节** → `39008` 回 46KB 战报（`replayResult`） |
| 竞技场 | 客户端 | `26004` 下发**双方阵容** + `26019` 由客户端上报结果（7623B 战报 + 139KB 存证 + token） |
| 天命对决 | 客户端 | `26038` 下发**双方角色明细** + `26039` 字段与 `26019` 逐字相同 |
| 失控炼成阵 | 客户端 | `22107`→`22108` 下发我方阵容；`22109` 由客户端上报 `dmgHpRate/score`（存证可为空） |

**结论**：四个板块里只有荣耀之巅**不需要任何战斗引擎**，可以直接用协议自动化。
其余三个都需要在 Python 侧复现"本地结算"（工作量与风险见 `notes/battle_engine_project.md`）。

## 二、荣耀之巅实测（`captures/荣耀之巅.saz`，2026-09-21）

该包只含 2 个网关 POST：

| 会话 | 请求 | 响应 |
|---|---|---|
| `raw/01` | `39011 TopTeamEditInfoPacket` | `39012`（616B） |
| `raw/02` | **`39007 TopPvpChallengePacket`（请求体 = 0 字节）** | **`39008`（46,792B 文件 / 子包 46,032B）** |

`39008` 的字段（顺序 = dump.cs 声明顺序）：

```
#1 errorCode        = 0
#2 gotList          = 15B   （GetObjClass）
#3 rewardList       = 81B   （repeated GetObjClass）
#4 versusInfo_Me    = 214B  （TopPvpVersusInfoClass：point/pointChange/…）
#5 versusinfo_Enemy = 149B
#6 replayResult     = 46,032B  ← 战报 JSON（**服务器算完整场**）
```

战报 JSON 的键（只列有用部分）：

```json
{"elapsed":1, "step":418, "winOrFail":true, "isNpc":false, "error":false,
 "replay_file_name":"4499072827CAEDAB_DAC105AA7EFB8A79",
 "reportDetail":"{\"T_Player\":{...}}",
 "recordAssembly":"H4sIA…"}     ← base64(gzip)，解压后是完整回放装配数据
```

> ⚠️ 战报单份 46KB。实现里只取 `winOrFail / elapsed / step / isNpc / error /
> replay_file_name` 这几个键（见 `TopPvpChallengeRes.battle_summary`），
> 绝不整段打进日志。

## 三、四个板块的通道差异（为什么天命对决抓不到）

- `captures/竞技场抓包.saz`：`26003/26004`、`26019/26020`、`97025` **都在 HTTP 网关**上；
- `captures/失控炼成阵.saz`：`22107/22108`、`22109/22110`、`97025` **都在 HTTP 网关**上；
- `captures/荣耀之巅.saz`：`39007/39008` 在 HTTP 网关上；
- `captures/天命对决抓包_胜利.saz` / `_失败.saz`：**只有"战后"流量**
  （`26031/26033/26053/26051` 与战报回放），进战与结算的 `26037/26038/26039/26040`
  **完全不在包内** —— 但战报时间戳落在抓包窗口内、次数也确实被扣（3→2），
  说明这些包走了 **Fiddler（HTTP 代理）看不到的通道**（很可能是握手下发的
  `serverPort=1888` 长连接 / 跨服战服；dump.cs 里存在 `NetWorkUdpModule`、`CommonTcpClient`）。

**因此天命对决/竞技场是否必须走那条通道，目前不臆断**；已知的是它们的**协议语义**
（谁算结果）不受通道影响。

## 四、尚未实测、必须诚实标注的两点

1. **`39001 → 39002` 这份包里没有**：`GetTopPvpHomeRes` 的 16 个字段目前只来自
   dump.cs（字段顺序可信，已被 `--check` 与单测钉住），**实际取值未见过**。
   第一次真机跑 `python main.py run top_pvp_battle`（只读）时会把它们打印出来。
2. **"剩余可挑战次数"没有已知字段**：39002 只有 `buyChallengeCount`（已购买次数）。
   本实现**不推算**次数，靠"用户显式场数 + `config.TOP_PVP_MAX_BATTLES` 硬上限 +
   服务器 `errorCode=-1`" 三重约束。若日后要精确读次数，候选是挑战券
   `RPS_Const.TopPvp_Ticket = 200000141` 的数量（需另找背包接口）。

## 五、复现命令（最小 token 消耗）

```bash
# 列某份 saz 里的网关 POST（只打印消息号与体积，不把大文件读进上下文）
PYTHONIOENCODING=utf-8 python -c "import glob,os,zipfile;from models.envelope import parse_envelope as P;from models.packet_ids import packet_name as N;z=zipfile.ZipFile([f for f in glob.glob('captures/*.saz') if '荣耀' in f][0]);[print(n,N(P(z.read(n).split(b'\r\n\r\n',1)[1]).packet_id),len(z.read(n))) for n in z.namelist() if n.endswith('_c.txt') and b'game-ct-labs' in z.read(n) and len(z.read(n))<800]"

# 解某个包（把 raw/02 换成目标会话号）
PYTHONIOENCODING=utf-8 python -c "import glob,zipfile;from models.envelope import parse_envelope as P;from models.protobuf_wire import fields_by_number as F;z=zipfile.ZipFile([f for f in glob.glob('captures/*.saz') if '荣耀' in f][0]);g=F(P(z.read('raw/02_s.txt').split(b'\r\n\r\n',1)[1]).sub_packet_bytes);print([(n,[(f.wire_type,(f.value if f.wire_type==0 else len(f.as_bytes()))) for f in g[n]][:6]) for n in sorted(g)])"
```

## 六、实现与验证

```bash
python -m pytest tests/test_models_top_pvp.py tests/test_tasks_top_pvp.py -q

# 只读（默认，零消耗）：打印段位/排名、已购次数、跳过战斗演出开关
python main.py run top_pvp_battle

# 演练（不发包）：打印将要发送的字节与安全边界
python main.py run top_pvp_battle --dry-run --battles 2

# 真打 2 场（消耗 2 次次数；服务器结算，输赢都继续）
python main.py run top_pvp_battle --battles 2
```

**安全边界（写死在代码与测试里）**

- 出站消息号白名单：`{39001, 39007, 39019}`；**禁发**：`39005`（买次数）等（见
  `models/top_pvp.py` 的 `FORBIDDEN_PACKET_IDS`）；
- 收到 `errorCode=-1`（次数不足）立即收工，**绝不进入购买分支**；
- 无论胜负都不重试（POST 有副作用）；
- 只读模式连"跳过战斗演出"开关都不会去改写。

---

## 七、真机实测（2026-09-21 19:2x，账号 <redacted> / S113 伊莉莎白一世）

### 7.1 实测结果

| 请求 | 结果 |
|---|---|
| `39001` 主页 | ✅ 正常：第4340名、段位 721000001、积分 1000、`isSkipBattle=True`（本就开启） |
| `39013` 赛季信息 | ✅ 正常（32,793 字节，说明模式未维护、赛季可读） |
| `39019` 开关 | ✅ 正常（104 字节） |
| **`39007` 挑战** | ❌ 22 字节 `{"response_code":"OK"}` + `content-type: text/html; charset=UTF-8` + `Connection: close` |
| **`39011` 队伍信息** | ❌ 同上（单发也一样） |

- 该 JSON **不是游戏协议**：`response_code` 在客户端字符串池里 0 命中；游戏服正常响应一律
  `content-type: application/octet-stream`。对照实验还证明：把抓包里的 39007 原样重放
  （token 已失效）时，服务端回的是**正规 protobuf** `99004 ServerExceptionRes`
  （文本 `RootPacket md5 is weird`）—— 说明"前端"对**签名正确**的请求才会转发/改答。
- 无效尝试（全部同样被拦）：带/不带真实 `settingMd5`、带活动指纹、带/不带 `playerId`、
  `--battles 1` 连续 4 次、单发 `39011`。
- 与频率/顺序**无关**：同一进程连发 3 次 `39001` 全部正常。
- 账号状态未变化（排名 4340 / 积分 1000 不变）→ 这些请求**没有产生战斗、没有消耗次数**。

### 7.2 由此定位并修复的三处真实缺陷（已进代码与测试）

1. **`timeStampToken` 没接**（`RootPacket` 字段 10）：服务端在**进区登录 1004 响应**的元信息里
   下发，真实客户端后续每个业务请求都回传（抓包实测：所有业务请求都带同一个值）。
   实现：`GameEnvelope.update_from` 捕获 → `GameSessionState` 持久化（会话文件 version 2）
   → `apply_to` 回填。⚠️ **必须重新登录一次**才会写入（旧会话文件会给一条 WARNING）。
2. **缺 `39011`**：实测抓包里真实客户端的顺序是 **39011 → 39007**。
   实现：`TopTeamEditInfoPacket.for_section(config.TOP_PVP_SECTION_ID)`（默认真机实测值
   `726010017`，可用 `CHERRYTALE_TOP_PVP_SECTION_ID` 覆盖）。
3. **失败响应无留痕**：`GameClient.send_raw` 解析失败时把**原始字节 + 响应头**落到 `tmp/`
   —— 正是靠它才认出那个 22 字节 JSON 与 `text/html`。

### 7.3 仍未定性 + 下一步（决定性实验）

`39007`/`39011` 被前端拦下的原因**还没定论**：可能是 (a) 我们的字节与"当前线上客户端"
仍差一处（例如 `settingMd5` 至今是 `-1`——我们的响应里从不下发它），或 (b) 服务端对该账号
做了额外门控。**判定方法**：让真实客户端**现在**打一场荣耀之巅，Fiddler 从"点挑战之前"
开始抓，然后把它那条 `39007` 请求体与我们的逐字节对比。

```bash
python main.py run top_pvp_battle                # 只读：段位/排名/开关（零消耗）
python main.py run top_pvp_battle --battles 1    # 真打 1 场（当前会被前端拦下）
python tools/extract_saz_posts.py --match 荣耀     # 列某份 saz 里的网关 POST
```

> 另：项目根目录那份 `.auth_token` 已过期（门户对它回 `4000 驗證失敗`），且它优先级高于
> 账号密码，导致"用密码登录"根本走不到。已改名为 `.auth_token.expired-20260921`（保留备份）。

---

## 八、第二次抓包对比（`captures/荣耀之巅_抓包2.saz`，账号 10000006）—— 定位到"账号状态"

### 8.1 真实客户端的战斗流程（全在 `:1893` HTTP 上，无其他调用）

| 会话 | 请求 | 响应 |
|---|---|---|
| raw/03 | `39001`（180B，含真 `settingMd5`/`timeStampToken`/活动指纹） | `39002` 21,443 B |
| raw/10 | `39011`（188B，子包 `08 a1 91 98 da 02 10 00` = sectionID **726010017** + actionType **0**） | `39012` 438 B |
| raw/16 | `39007`（180B，空子包） | **`39008` 73,264 B（真的打了一场）** |
| raw/27 | `39001` | `39002` 21,455 B |

→ 与我们实现的顺序/子包字节**完全一致**（`filter_section(726010017)` 对得上）。

### 8.2 `settingMd5` 不是那道门（已排除）

- 值形如 `2.2.0-el-h-win|<32 hex>`，**会随时间变化**（14:2x 为 `af484fe5…`，19:5x 为 `77bf1876…`）；
- 它**不在任何响应**里（响应元字段只有 `packetID` / `PlayerBasicDataClass` /
  `SpecialActivityRefreshClass`）；也不是 `LoginRes.loginSetting`（那只是庆典活动信息）；
- 客户端本地 `TextAsset/config` 只有 `2.2.0;0fb4a766c09`（另一套版本号）；
- `tools/probe_setting_md5.py` 试了 7 种"本地配置表哈希"配方，**全部未命中**；
- **用抓包里的真值实测**（零消耗的 `39011`）→ **仍被拦**。

### 8.3 真正的差异：账号状态（两侧 `39002` 对比）

| 字段 | 对方 10000002 | 我们测试号 10000001 |
|---|---|---|
| `beforeClassID`（段位） | 721000009 | 721000001（最低段） |
| `classRankClassList` / 总榜条数 | 3 / 3 | 17 / 101 |
| **`battleFlags`（巅峰战旗）** | **3 个（52/52/24B）** | **0 个** |
| `bonusRoleID` | 3 个 | 1 个 |
| `rewardCount` | 2750 | 2026 |
| 防守队伍（旧抓包 `39012`） | 已配置 3 队 | 未知（`39011` 进不去） |
| errorCode / refreshTime / buff / isSkipBattle / proveRewardID | 一致 | 一致 |

**结论（待下次验证确认）**：对方是"已在荣耀之巅打过、且配好防守队伍与战旗"的账号；
我们用的是从未参战的新号。服务端对**战斗侧**两个包（`39011`/`39007`）回非协议软 ack
（22B `{"response_code":"OK"}`、`text/html`），而**大厅侧**（`39001/39013/39019`）一律正常 ——
与"战斗侧要求账号已完成模式初始化"这一假设吻合。

### 8.4 下一步验证（用户侧操作 → 本工具复跑）

```bash
# ① 在**游戏里**用测试号 10000001 进入荣耀之巅：保存防守阵容、设置战旗、打一场
# ② 游戏内登录会顶掉我们的会话，因此先刷新一次：
CHERRYTALE_SERVER_ID=113 python main.py run login --account <redacted>
# ③ 先只读看状态（零消耗），再真打一场：
python main.py run top_pvp_battle
python main.py run top_pvp_battle --battles 1
```

若 ③ 仍被拦，则说明门更深（会话层/账号层），下一步考虑：
- 用 `39009 SetTopPvpDefTeamPacket` 主动配置防守队伍（需队伍数据，届时加白名单 + 单测）；
- 或对比"游戏内首战后"的 `39002`，确认 `battleFlags`/段位是否变化。

---

## 九、✅ 根因找到并修复（2026-09-21 20:07 真机打通）

### 9.1 根因：**请求必须显式带上"值为 0 的字段"**（字节形状不对就被服务端当机器人挡下）

真实客户端的请求会把默认值字段也发出来，我们按 `include_defaults=False` 组包时把它们省掉了：

| 位置 | 真实客户端（抓包） | 我们（修复前） |
|---|---|---|
| `39011` 子包 | `08 a1 91 98 da 02 **10 00**`（8B，**含 `actionType=0`**） | `08 a1 91 98 da 02`（7B，**缺** `actionType`） |
| 字段 16 活动指纹 | `0A 20 <32B activeMission> **12 00**`（**含空的 `puzzleRoleGroup`**） | `0A 20 <32B activeMission>`（缺空字段） |

现象：这两个"0 值字段"缺一个，服务端就回 **22 字节 `{"response_code":"OK"}` + `content-type: text/html`**
（非 protobuf、`response_code` 在客户端字符串池里 0 命中）——**读类请求不受影响**，
所以表现为"读得到状态、打不了架"。

### 9.2 四组对照实验（每次只改一个变量，各发一次 `39011`，零消耗）

| 变体 | 条件 | 体长 | 结果 |
|---|---|---|---|
| A | 真指纹 + `include_defaults=True` + 不发 playerId | 188B | ✔ 成功 |
| B | **指纹 = `-1`** + `include_defaults=True` + 不发 playerId | 143B | ✔ 成功 ← **`settingMd5` 无关** |
| C | 真指纹 + `include_defaults=False`（两个 0 值字段都没有） | 184B | ✘ 被拦 |
| D | 真指纹 + `include_defaults=True` + 带 playerId | 193B | ✔ 成功 ← **playerId 无关** |

> 顺带排除：`vCode` 算法**正确**（用抓包的 `packetID/token/settingMd5/timeStamp` 复算，
> 得到 `2AA91E92E418F417DB94487A295F7BF0`，与抓包**逐字符一致**）；
> `settingMd5` 既不在任何响应里、也不是 `LoginRes.loginSetting`，
> `tools/probe_setting_md5.py` 的 7 种本地配置表哈希配方也都没命中 —— 但**它不需要**。

### 9.3 落地的修复

| 文件 | 改动 |
|---|---|
| `models/envelope.py` | 字段 16 的活动指纹**始终**带空的 `puzzleRoleGroup`（`encode(include_defaults=True)`）；更新了"只回传 activeMission"的旧注释（旧教训指的是发**非空**的 puzzleRoleGroup） |
| `tasks/packet_guard.py`（公共守卫） | 白名单 + 禁发清单 + 消息号核对 + `include_defaults` 开关 + 非 protobuf 响应的可读提示 |
| `tasks/top_pvp.py` | 出站请求走公共守卫并**显式** `include_defaults=True`（实测必需：`39011` 会发 `actionType=0`） |

### 9.3.1 ⚠️ 一条被后续实测**修正**的细节：`include_defaults` 不是"一律开启"

做完三个只读任务时发现真实客户端的规则更细：

| 包 | 字段 | 客户端实际发的字节 | 说明 |
|---|---|---|---|
| `39011` | `actionType`（int，**无** `DefaultValue`） | **发** `10 00` | 值为 0 也要发 |
| `26033` | `openTest`（string，**带** `[DefaultValueAttribute]`） | **不发**（子包为空） | 等于默认值就省略 |

也就是说客户端遵循"字段带 ``[DefaultValue]`` 才省略"，而不是"所有 0 值都省略/都发"。
因此：

- **荣耀之巅**（`39011`/`39007`）：`include_defaults=True`（实测必需，否则被 22 字节 ack 挡下）；
- **三个只读板块**（`26001`/`26031`/`26033`/`22101`）：`include_defaults=False`（与抓包一致），
  由 `tasks/packet_guard.guarded_send(..., include_defaults=...)` 按包决定。

> 这一条也解释了为什么"给所有包都开 include_defaults"是错的：会给 `26033`
> 发出客户端不发的字节（`0a 00`），而服务端对这种"多出来的字段"历来不宽容
> （历史上就出现过 `ABNORMAL_PACKET`）。

### 9.3.2 三个只读板块（2026-09-21 20:18 真机验证，**零消耗**）

| 任务 | 命令 | 实读结果 |
|---|---|---|
| 竞技场 | `python main.py run arena_status` | 可挑战次数 **5**、已购买 0、已刷新 0、对手 **16** 个 |
| 天命对决 | `python main.py run wudou_status` | 剩余挑战次数 **5**、NPC 0、名次 **606**、对手 5 个、结算时间 2026-09-21 23:00:00、分组 18 |
| 失控炼成阵 | `python main.py run yimo_status` | 剩余次数 **5**、积分 0、排名 0、**boss 数据=有**、奖励条目 3 个 |

三者都**只发白名单内的只读包**（竞技场 26001 / 天命对决 26031+26033 / 失控炼成阵 22101），
进战、结算、买次数、领奖的包**在模型层就不存在**，另有源码级回归测试兜底。

### 9.4 真机打通记录（20:07）

```
队伍信息：可选角色 20 个、防守队伍 已配置（3 队）
第 1/1 场：errorCode=0，本场=胜，积分变化=+90，step=85，elapsed=0.489
         奖励=道具 200000141×3（挑战券）、200000143×2835、200000050×37175960、200000146×122680
✔ 完成 1/1 场（胜 1、负 0；积分变化合计 +90）
```

**结论：荣耀之巅已可用纯协议自动化**（服务器结算 + 跳过战斗演出默认开启 + 绝不购买次数）。

> 2026-09-25 更新：跳过演出不再是可选项 —— 「保留战斗演出」开关与
> `--keep-battle-animation` 都已删除（行为固定；逃生舱仅剩环境变量
> `CHERRYTALE_TOP_PVP_SKIP_BATTLE=0`），每场间隔默认由 2 秒改为 **15 秒**。
