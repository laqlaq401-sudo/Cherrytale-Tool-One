# 每日任务自动执行（`daily_task_auto`）—— 方案与实施计划

> 状态：**方案定稿 · 已实现 · 真机验收通过 · 目标自动顺延（B）已落地**（2026-10-16 晚）
> 6 个动作（含契约拼图，用户已拍板纳入）；单测 26 例
> 关联：`tasks/daily_box.py`（领奖逻辑现成）、`models/daily.py`（6001 报文）、`notes/open_questions.md` Q-023

---

## 一、背景与事实基线

### 1.1 线上「日常任务」= 18 条（不是配置表全部）
- `Cherrytale Asset/TextAsset/DailyMissionData` 共 **47 条任务行**（29 条 `status=0` + 18 条 `status=-1`）；
  **服务端只下发 18 条**（dmID **249000040~249000057**，即 `status=-1` 组），`status=0` 的 29 条为旧版、线上不下发。
  证据：`captures/每日免费一次抽卡.saz` raw/04·23·31（6002 响应的 dmInitList）。
- 每条完成 **+1 活跃点**；活跃宝箱 5 档需 **1/3/5/7/9 点**（`DailyMissionRewardData` 939000001~5）；
  周常同结构（`WeeklyMissionData` 16 条 / `WeeklyMissionRewardData` 251000000~4）。

### 1.2 18 条 × 归属（终版）
| 归属 | 条数 | 明细 |
|---|---|---|
| **本任务执行** | **6** | 英雄等级提升1次、英雄技能升级1次、装备强化1次、英雄赠礼1次、好友赠礼3次、**契约拼图抽取1次** |
| 现有独立任务负责（本任务**不碰**） | 7 | 登录 / 免费召唤 / 市集购买 / 宴席 / 素材关卡×5 / 通关×5 / 消耗体力80 |
| 排除 | 2 | 竞技场、天命对决（PvP） |
| 做不了（暂缓） | 3 | 失控炼成阵（缺 token 签名）、契约调教（协议未证）、辉煌航迹「进行」（未证） |

> **两个原始目标如何落地**：「把所有可做的做完」＝本任务补 6 个缺口动作 + 现成 7 条由独立任务照跑；
> 「宝箱全领」＝两侧点数合并后由**一键领奖**结算（本任务结尾会领并报告缺口）。

### 1.3 领奖链路（现成，直接复用）
- `DMReward(misID=-1)` 一键结算：`mistypeID` 0=日常任务 / 14=周常任务 / 6=日常宝箱 / 15=周常宝箱；
  无待领回 `-1`（正常）；**传具体宝箱 ID 会被拒 `-2`（勿传）**。逻辑在 `tasks/daily_box.py`。
- ⚠️ `misID=-1` 源自反汇编、**尚未真机验证**（Q-023 未解）→ 验证计划第 1 项。

---

## 二、已定决策（grilling 逐条确认，不再改动）

| # | 决策 |
|---|---|
| D1 | **单个主动任务**（不拆子任务）；手动「执行」；**不进「一键执行」**；不置灰、可反复跑（每次只做剩余） |
| D2 | 只做 **6 个新动作**；现成 7 条**不执行、不管理**（避免与独立任务重复执行/抢资源） |
| D3 | 英雄升级＝**等级最低 SSR**、只 +1 级；并列任取（取名单先遇，可复现） |
| D4 | 技能升级＝同一 SSR 上「可升、下一级花费最低」的技能 +1 级 |
| D5 | 装备强化＝**强化等级最低**的装备 ×1（并列取花费最省） |
| D6 | 英雄赠礼＝同一 SSR + 背包**最便宜礼物** ×1（礼物基本通用；被拒自动换下一个） |
| D7 | 好友赠礼＝游戏「**一键全送**」（SendAllGift=4, playerID=-1；送礼无成本） |
| D8 | 资源不足/数据缺失 → **跳过该项并报告**，其余照做 |
| D9 | 统一领奖（四连）；报告含「活跃点 X/9、缺口任务清单」 |
| D10 | 验证先行 → 回归 → 双端手测；**文档过目后才动代码** |
| D11 | **契约拼图抽取纳入 v1**：读 11029/11030 → 挑 `sectionState==0`（NewOpen）区段、步阶最小者优先 → `11009{sectionID, times=1, useSweepTicket=0}`（include_defaults、有副作用不重试）；消耗＝CP（200000111）按步阶 `energyCost` 5~20 |

---

## 三、抓包核对结论（2026-10-16，6/6 完成）

证据：`captures/英雄升级.saz`、`captures/每日任务 英雄升级 技能升级 装备升级.saz`、
`captures/好友礼物 一键赠送 一键领取.saz`、`captures/契约 拼图抽取.saz`
（嵌套核对脚本 `tmp/deep_tree.py`，未入库）

| 动作 | 请求（子包字段） | 响应 | 备注 |
|---|---|---|---|
| **英雄升级 5003** | `#1 roleSid` + `#2 经验书列表`（每项 `itemSid/itemID/数量`） | 5004：`errorCode + 更新后 RoleClass + costList(消耗后余额)` | 升级/升技后客户端补发 97007 战力上报——**可忽略** |
| **技能升级 4001** | `#1 roleSid`、`#2 skillPlace=2`、`#3 targetLV=2`（单元素、**非 packed**） | 4002：`errorCode + itemClass(余额)` | skillPlace/targetLV 语义见 §七-2（受控实验钉死） |
| **装备强化 3003** | `#1 equipSid`、`#2 count=1` | 3004：`errorCode + 2×ItemClass`（魔尘 200200049 / 金币 余额） | 消耗＝魔尘＋金币 |
| **英雄赠礼 5015** | `#1 roleSid`、`#2 itemId=0`、`#3 useTime=0`、`#4 [{itemSid:-1, itemID:礼物, 数量:1}]` | 5016：`errorCode + getList(含更新后 RoleClass) + addExp(好感+10)` | **0 值字段显式发送**（include_defaults=True 口径） |
| **好友赠礼 10009** | `#1 actionType=4（SendAllGift）`、`#2 playerID=-1` | 10010：`errorCode …` | 「一键领取」＝actionType=3（本次也抓到） |
| **契约拼图 11009/11010** | `#1 secretSectionID`、`#2 times`、`#3 useSweepTicket=0` | 11010：`errorCode + rewardList(拼图碎片) + costList(CP 余额)` | 详见 §八 |
| 好友列表 10001/10002 | 空查询 `#1=0` | 好友 50 人；`FriendClass` 与 `models/bbq.py` 一致（send_state #8 / get_state #9） | ≥3 人，满足「赠礼 3 次」 |

**经验口径（实证钉死）**：升级所需经验 = `2 × expSum[roleLV] − roleExp`（`RoleExpData` 的 expSum；`roleExp` 取 RoleClass#5）。
两样本自洽互证：① LV44 样本 roleExp 恰为 `2×expSum[43]`；② LV32 样本喂 88140 经验后（喂前推算 828980 = `2×expSum[30]`+100，即 LV31 进行中）
恰好跨过 `2×expSum[31]`=917060 → LV32、余 60。
**选书策略**：贪心（大书→小书、尾差补最小书），**控制溢出**（避免一次升多级）。

---

## 四、技术方案

### 4.1 任务形态
- 注册名 `daily_task_auto`（暂定）；title「每日任务自动执行」；group=manual；**无参数**；单「执行」按钮；前端预计零改动。
- 执行流程（`tasks/daily_task_auto.py`）：
  1. 拉 6001 状态（复用 `daily_box` 抽出的模块级公共函数）
  2. 先一键领掉已达标项（任务奖励 + 宝箱，顺手）
  3. 逐个检查 **6 个动作**的完成态 → **未完成才做**（动作间 `cancellation.sleep_interruptible`；循环点 `cancellation.raise_if_cancelled`）
  4. 再一键领奖（新完成奖励 + 新达标宝箱）
  5. 复查；无新进展即止
  6. **报告**：本次补做明细 / 领到明细 / 活跃点与缺口
- 副作用纪律：超时**不重试**；业务失败跳过并记录；dry-run 打印全部请求体（项目惯例）。

### 4.2 目标选择（策略实现）
- **候选序 + 自动顺延（B，2026-10-16 新增）**：SSR 全量按 `(roleLV, roleSid)` 升序排成候选序列；
  「英雄升级 / 技能升级 / 英雄赠礼」从首位（= D3 的最低等级 SSR）开始顺序取用，
  本地可判定不可行的候选**静默跳过**，最多**真发 3 次**（`MAX_FALLBACK_ATTEMPTS`）——保证 1 级 SSR 垫底的账号也不留缺口。
- **SSR 判定**：`roleID → RoleMainData → RoleQualificationData`，`qualificationPrint ∈ {23,24,25}` ⇒ SSR（含 Pick UP/Wish）；
  需在 `models/game_config.py` 新增换算（底层表加载已有）。
- **名册来源**：登录快照（1004）优先（含 roleLV/roleExp/skillsLV）；不足时发 5011（从未真机验证过）。
- **技能当前等级**：解析 `RoleClass.skillsLV`（= `SkillClass{activeLV,loopLV,passive1LV,passive2LV,awakeningLV,autoSkill}`，dump 已取）。
- **最便宜技能**：先按**品质门槛**过滤（`SkillOpenStateData` 按英雄 `skillOpenID` 取行，`quality ≥ 门槛` 的技能位才可升），再逐技能查 `SkillUpgradeCostData` 的「下一级花费」取最小。
- **装备**：快照装备列表取 `min(equipLV)`（并列取低阶/品质最省）。
- **礼物**：背包扫 `useType=12`（200600001~005 / 091~093），取价值最低、数量≥1。
- **经验书**：背包 `200000003~010`（ExpBooksData 值 30~380000），按 §三 策略选；itemSid 从背包拿（1004 itemInitClass / 2007 GetAllItem）。
- **契约拼图**：见 §八（读 11030 → 挑 `sectionState==0`、步阶最小的区段 → 11009 `times=1`）。

### 4.3 领奖与报告（口径）
- 领：`DMReward(-1, 0/14/6/15)`；`0`=领到、`-1`=无待领（正常）、其它=失败记录。
- 报告示例：
  `本次补做：英雄升级 ✔（最低 SSR +1 级）/ 技能升级 ✔ / 装备强化 ✔ / 英雄赠礼 ✔ / 好友赠礼 ✔（50 位好友）/ 契约拼图 ✔；
  一键领奖：任务奖励×N、宝箱×M；当前活跃点 X/9——缺口任务：通关×5、素材×5（请跑一键执行/对应任务）`

---

## 五、改动清单（草案）

| 文件 | 改动 |
|---|---|
| `tools/extract_proto_fields.py` | FOCUS_CLASSES 增补：4001/4002、5003/5004、3003/3004、5015/5016、10009/10010、10001/10002、11029/11030（11009/11010 已收录）→ **重生成** `models/proto_fields.py` |
| `models/role.py` | 5003 移出 `FORBIDDEN_PACKET_IDS`；新增 `UpRoleExpPacket/Res`；`skillsLV` 建模（SkillClass） |
| `models/equip.py`（新） | `StrengthenEquipPacket/Res`（复用 `EquipClass`） |
| `models/present.py`（新） | `UsePresentPacket/Res` |
| `models/friend.py`（新） | `FriendOperationPacket/Res`、`GetFriendListPacket/Res`（`FriendClass` 复用 bbq） |
| `models/secret.py`（新） | `GetSecretSectionPacket/Res`（11029/11030）；11009/11010 复用 `models/activity_stage.py` |
| `models/game_config.py` | SSR 换算；`ExpBooksData` / `SkillUpgradeCostData` / `RoleExpData` / `SecretSectionData` 加载 |
| `tasks/daily_task_auto.py`（新） | 主任务（§4.1 流程） |
| `services/task_registry.py` | `load_builtin_tasks()` 加一行 import |
| `services/task_spec.py` | 登记 `TaskSpec`（manual、无参数） |
| `services/runner.py` | 视需要：`task_kwargs_for` 加翻译分支（无参任务通常无需） |
| `tests/test_tasks_daily_task_auto.py`（新） | 编解码 / 目标选择 / 循环 / 降级 |
| 前端 `web/` | **预计零改动**（普通卡片自动渲染）；⚠️ 若动 `web/` 须重建 tailwind + `sync_android_assets.py` |

---

## 六、验证计划（顺序执行）

0. ✅ **会话恢复**（2026-10-16 21:5x）：用户以「#98 肉桂樹魔（空桉）」新会话（`Cheerytoolneeds/.session.json`）供验证；工程以 `CHERRYTALE_SESSION_FILE` 指向它、**零拷贝**接入。
1. ✅ **misID=-1 领奖真机复验（已闭）**：四连领奖请求全部被受理（无待领时回 `-1`）；受控实验（免费晚宴席 → 完成「天天吃大餐」）后 `daily_box` 实测 **errorCode=0 真领到**（奖励入账、活跃点 9→10）。**Q-023 闭环**。
2. **受控实验**（每项动作前后各拉名册/6001 对照）：
   a. ✅ **技能升级 `skillPlace/targetLV` 语义钉死（已闭，2026-10-16 真机探针 ×6）**：
      `skillPlace` = `skillsLV` 字段的 **1 起序号**（1=active / 2=loop / 3=被动1 / 4=被动2 / 5=觉醒；1/2/3 已实测，4/5 外推）；
      `targetLV` = **目标绝对等级**；组包 9B 字节与服务端完全兼容（errorCode=0）。
      **新发现（解锁门槛）**：技能位按英雄**品质**解锁——`SkillOpenStateData`（行 1）＝
      `{active≥0, loop≥1, awakening≥2, passive1≥4, passive2≥6, passive3≥7}`（"≥"含等号，已在 passive1=4 边界实测）；
      未达 → `errorCode=-4`（实证：quality=0 英雄升 loop 被拒、同英雄升 active 通过）。
      记录：`tmp/probe_skill_up.py` / `tmp/diff_roster.py` / `tmp/roster_*.json`。
      ⚠️ 对测试账号的微小改动：奥茲 loop 2→3（另旧抓包 1→2）、克麗奧佩脫拉 active 1→2、織縈 loop 1→2、麻理繪 loop 1→2 与被动1 1→2；
   b. 5003 / 4001 / 3003 / 5015 各真发一次 → 6001 对应任务 completedTimes +1（随实现阶段的真机验收执行）；
   c. 契约拼图真发 1 次（随实现执行）。
3. ✅ **名册真机复核（已闭）**：5011→5012 **实测可用**（154 个角色，来源=实时名册）——该链路首次真机验证。
4. dry-run 全流程 → 真机全流程 → 回归（全量 pytest，cherrytale 环境）+ 双端手测。
5. ✅ **实现完成（2026-10-16 晚）**：`tasks/daily_task_auto.py` + 模型层（`models/{equip,present,friend,secret}.py`、
   `models/role.py` 扩展、`game_config` 五个新加载器）+ `extract_proto_fields` 补 17 类重生成 + 注册；
   新增单测 22 例（后随 B 方案增至 **26 例**）；全量 pytest **1840 passed / 16 skipped / 0 failed**（连跑两遍稳定）；CLI 冒烟
   （`main.py tasks` / `daily_task_auto --dry-run`）通过。**真机验收（2b/2c + 全流程）待用户配合执行。**
   ⚠️ 前端零改动（普通卡片自动渲染）；未动 `web/`，无需 tailwind 重建 / Android 资源同步。
6. ✅ **真机验收（2026-10-16 晚，#98 空桉）**：正式任务真机跑通（编排 / 已做跳过 / 领奖 / 缺口报告全部正确，白名单记录干净）；
   六个动作由**生产步骤方法**逐一真发全部被服务端接受：3003+3005、5015（好感 +50）、10001+10009、11029+11009、
   **5003（首次真发：宿儺 11→12 级）**、4001（宿儺主动技 1→2）。至此全部报文族真机验过。
   ⚠️ **发现一个目标策略边缘**：当"最低等级 SSR"是 **1 级新英雄**时——
   ① 升级被本任务"不越级"保护跳过（背包最小书 140 > 单级需求 20，会一次升 3 级）；
   ② 技能升级被服务端回 `-3`（新英雄技能尚未开放）。
   → 已选 **B（自动顺延）**，见 §六-7。
7. ✅ **目标自动顺延（B）已落地（2026-10-16 晚，用户拍板）**：
   - **候选序**：全部 SSR 按 `(roleLV, roleSid)` **升序**（`_pick_target_candidates`），首位即"等级最低的 SSR"（与 D3 完全一致，无并列随机性）。
   - **三个"英雄向"动作**（英雄升级 / 技能升级 / 英雄赠礼）改为**顺序取用候选**：
     先跳过**本地算得出不可行**的候选（满级、背包无书可凑、技能全满、`roleLV<2` 等 —— 不计次、不发包），
     再向服务端**最多真发 `MAX_FALLBACK_ATTEMPTS = 3` 次**；成功即止。
   - **应答口径**：成功且非首位候选 → 措辞追加「；已自动顺延」；全部候选都失败 → 追加「；已尝试 N 个候选」
     （赠礼为「；已尝试 N 次**赠送**」）并**如实跳过**（D8 不变）。
   - **不适用**：装备强化 / 好友赠礼 / 契约拼图（目标唯一或全量，无顺延概念）。D6 里"礼物被拒自动换下一个"是**换礼物**，与换英雄是两回事，两者可叠加。
   - 单测：`tests/test_tasks_daily_task_auto.py::TestFallback`（静态跳过不计次 / `-3`→顺延 / 赠礼偏好 / 上限封顶），全套 **26 passed**；
     全量 pytest **1844 passed / 16 skipped / 0 failed**（rc=0，96.6s；= B 之前 1840 + 顺延 4 例）。
   - ⚠️ 措辞细节：赠礼全败的尾巴是「已尝试 N 次**赠送**」（该处计的是赠送次数 —— 同一候选可能换过多个礼物），
     与升级/升技的「已尝试 N 个**候选**」不同口径。

---

## 七、风险与未决

1. ~~**Q-023 未解**：`misID=-1` 无真机记录；`-2` 语义未定 → 验证 1 钉死。~~ → ✅ **已闭（2026-10-16 真机）**：`misID=-1` 被受理（`-1`=无待领）且**成功路径实测**（errorCode=0 + 奖励入账）；传具体宝箱 ID 仍属已知 `-2` 场景。
2. ~~**技能升级 `skillPlace/targetLV` 语义存疑**~~ → ✅ **已闭（2026-10-16 真机）**：`skillPlace`＝1 起序号、`targetLV`＝绝对目标等级；**技能位解锁门槛＝英雄品质**（见 §六-2a）。外推项：place 4/5 待实现验收时点一次复核即可。
3. 英雄赠礼「礼物↔角色」映射列全为空 → 被拒换礼物兜底。
4. exp ×2 倍率来源未考（公式已实证）；若遇偏差在验证 2b 复核。
5. 「扫荡算不算通关/素材」**不影响本任务**（现成 7 条不碰），仅影响缺口报告措辞。
6. `models/session_state.py` 的角色快照只有 4 字段（roleSid/ID/LV/Star）、且无装备快照 → 实现时需**扩展快照**或直读登录会话完整解析（含 roleExp/skillsLV/装备表）。
7. **契约拼图待核**：为何 raw/28 成功抽取了未出现在 11030 清单里的 909179104 段（"可抽"判据是否不只认 `sectionState==0`）；
   「任意一次 11009 是否都计 270101」无 6001 前后对照；CP 消耗公式的十连侧（20×10）缺前值佐证。

---

## 八、契约拼图抽取（已定：纳入 v1）

- 判定：日常任务 249000048「窺探契約：抽取《契約》拼圖 1 次」（actionID 270101，+1 活跃点）。
- 机制（抓包实证）：`11029`（空包只读）→ `11030`（区段清单：`#1 sectionID / #2 sectionState / #3 limitItemCount / #4 puzzleFragBonus`）
  → `11009 SweepSectionPacket{sectionID, times, useSweepTicket=0}`（**必须 include_defaults**；有副作用不重试）→ `11010`。
- 消耗：CP（`200000111`）= 该步阶的 `energyCost`（Step1~4 = 5/10/15/20）；`SecretSectionData` 其余列多为空/未用。
- 实现策略（建议）：挑 `sectionState == 0（NewOpen）` 的区段、**步阶最小者优先**（示例：909179101，Step1 仅 5 CP）；固定 `times=1`，不自动十连。
- 待核清单（实现/验证阶段收敛）：见 §七-7。

---

## 九、明确不做

- 不执行现成 7 条；不做 PvP（竞技场/天命对决）；不做炼成阵 / 契约调教 / 航迹（协议未证）。
- 不改现有任务与「一键执行」；不碰钻石 / 体力 / 扫荡券。
- 不做二次确认弹窗（沿用既有原则）；不自动清空任何状态。
