# 立项：战斗引擎 + token 签名（代号 M2-BE）

> 状态：**未启动**（本期只交付荣耀之巅，见 `notes/glory_summit_capture.md` §6）。
> 本文件是启动时的输入，不是已完成的工作。每条结论都附证据与复现命令，
> 目的是启动时**不必重新解一遍抓包**。

## 0. 立项理由

竞技场 / 天命对决 / 失控炼成阵 的战斗结果**必须由客户端本地算出并上报**：
`26019` / `26039` / `22109` 都带 `battleResult`（内含逐施法存证 `battleServerVerify`）
与 32 位 hex 的 `token`；握手 `jsonInfo.PVPVerify=1`、客户端全局 `isPVPVerify`
表明服务端会据此复核。没有这套引擎，这三个板块只能停在"只读次数"。

## 1. 验收标准（唯一硬指标）

真机打一场竞技场，`26019` + `97025` 被服务端接受（`26020.errorCode=0`），
本地结算的胜负/积分与 `26004.winIntegral/loseIntegral`、`26020.myResults` 对得上。

## 2. 已确认事实（字节级，勿重复侦察）

| # | 结论 | 出处 |
|---|---|---|
| F1 | 竞技场进战 `26003{actionType=0, playerID, teamID}` → `26004{errorCode, loseIntegral, winIntegral, items, fightToken, fightGlobalEffectClass, opponentAssemble, mineAssemble}` | `captures/竞技场抓包.saz` raw/020 |
| F2 | 结算 `26019{result, type, playerID, fightingNumber, fightToken, battleResult(7237B), token(32hex)}` → `26020{errorCode, myResults}` | 同上 raw/243 |
| F3 | `BattleResultClass` 8 字段：teamId / fightingPower / damageContent / damage / yimoLv / myTeamRecord / enemyTeamRecord / **battleServerVerify** | dump.cs:431949 |
| F4 | `battleServerVerify` = base64(gzip(JSON))，解压 **139,199B**：`{"BattleType":1,"SectionId":0,"Proofs":[{"isDmgRes","isDmgRdc","ShareCount","LastValue","NeedVerifyDmgFormat","Cast":{"order","from","to","effId","eSkillType"},"LstAttachEffs":[…]}]}` | 解 raw/243 |
| F5 | `token` **不是朴素哈希**：`md5(battleResult)/md5(damageContent)/md5(battleServerVerify)/md5(整包)/md5(去token)/md5(拼接)` 六组候选**全不中** | 离线穷举 |
| F6 | `97025 PVPVerifyPacket` 的 `message/fightRecord/fightData` **都是 JSON**；开战前会先发一条**空战报**（`damage 0`、`Proofs` 空、`winOrLose=-1`）并被接受 | raw/232、raw/244 |
| F7 | 客户端全局 `isPVPVerify` + 类 `BattleServerVerifyData`、`NumcVerifyProofs`、`BattleResultModule` | dump.cs:741602 / 482024 / 466429 |
| F8 | 天命对决同构：`26038{opponentData, battleClass: List<RoleBattleOrDetailClass>}` + `26039` ≡ `26019` | dump.cs:413369 |
| F9 | 天命对决战报 `WuDouReplayRes.replayResult` = JSON（`elapsed/step/winOrFail/reportDetail/recordAssembly`），`recordAssembly` 解压 **1,157,636B** | `天命对决抓包_胜利.saz` raw/03 |
| F10 | **失控炼成阵是例外**：`22109` 上报 `dmgHpRate=10000, score=53247`，存证解压仅 `{"BattleType":7,"SectionId":661000003,"Proofs":[]}` → 服务端 `errorCode=0`、发奖、扣次数 | `失控炼成阵.saz` raw/26 |

**由 F10 得出的关键判断**：失控炼成阵**不需要战斗引擎**，
只要拿到 `token` 的签名公式（并确认 `dmgHpRate/score` 的取值来源）即可自动化。

## 3. 未知项（每条：答案来源 + 阻塞对象 + 复现）

| # | 未知项 | 答案只能来自 | 阻塞 |
|---|---|---|---|
| U1 | **`token` 签名公式**（最关键） | 反汇编 `GameAssembly.dll`（dump.cs 无方法体） | 失控炼成阵 ✅ 直接卡这里；竞技场也在 |
| U2 | `Proofs` 最小合法集与完整 schema | 多份真实存证 + `BattleServerVerifyData` | 竞技场/天命对决 |
| U3 | 服务端**是否真的复算**（还是只存不验） | 反汇编 + 协议面证据（**禁做真机试探**） | 决定 M2 是"工程量大"还是"不该做" |
| U4 | 随机数来源与种子 | 反汇编战斗随机类 | 结算一致性 |
| U5 | 技能/数值表与伤害公式 | 配置表 `SkillData`/`SkillEffectData`/`EnemyLineupData`… + 反汇编 | 竞技场/天命对决 |

复现（找 U1）：

```bash
python tools/il2cpp_symtab.py                      # 定位 RVA
python tools/disasm_il2cpp.py --grep token         # 反汇编找签名函数
```

## 4. 阶段与 go/no-go 门（每阶段都有"失败即停"）

| 阶段 | 时间盒 | 交付物 | 退出条件 |
|---|---|---|---|
| **G0 定性** | 0.5~1 天 | `notes/battle_engine_G0.md`（U1/U2/U3 结论） | **U1 拿不到 → 立即 no-go** |
| **G1 单场最小复现** | 2~4 天 | `tools/battle_sim.py`（最小）+ 一场真机成功日志 | 连续 2 次失败且定位不到 → 暂停重估 |
| **G2 引擎完整化** | 按模式增量 | 逐模式验收（**不许一次做完**） | 每模式独立判 |
| **G3 接入任务层** | 1 天 | 按 §5 契约接到 `tasks/`（开关/上限/禁买防线不变） | — |

> 优先级建议：**先做 U1（token 单点逆向）**，因为它是"小投入、决定失控炼成阵能否落地"
> 的那一步；竞技场的 Proofs 引擎（U2/U5）是大投入，等 U1 与 U3 结论出来再评估。

## 5. 接口契约（任务层将来不用改）

```python
# tools/battle_sim.py
@dataclass(frozen=True)
class BattleInput:
    mine_assemble: bytes; opponent_assemble: bytes
    fight_token: int; battle_type: int
    yimo_part: int | None = None

@dataclass(frozen=True)
class BattleOutcome:
    win_or_lose: int
    battle_result_bytes: bytes   # → 直接填 26019.battleResult
    token: str                   # → 直接填 26019.token
    dmg_hp_rate: int; score: int # 练成阵用
    replay_json: str             # 97025.fightRecord/fightData 用
```

## 6. 风险与**禁做清单**

- 风险：账号风控/封禁（天命对决是真人 PvP，风险最高）、随机数不可复现、
  服务端复算不一致、配置表跨版本漂移、工作量爆炸（周级）。
- ⛔ **禁做**：① 伪造/篡改/重放战报去试探服务端校验；② G1 通过前消耗真实次数；
  ③ 把 139KB 存证 / 1.1MB 回放 / 95MB 归档整段读进上下文。
- 🛡 每次真机实验前后跑一次只读任务（`python main.py run top_pvp_battle`）记录状态对账。
