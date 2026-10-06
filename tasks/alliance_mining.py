"""工会挖矿（新版 NewPit 协议家族：个人矿 + 团体矿，两个任务共用本模块）。

【为什么个人 / 团体是两个任务】
两者在协议上同源（21039 一次拉回 ``personalList`` + ``teamList``），但
**团体矿的正常运转依赖队友**（槽位按 ``memberPid`` 分给成员、按
``needRoleInfoID`` 指定角色）—— 混在一个任务里，"队友没到位"会把个人矿
的成功也污染成失败行。拆开后各自一条 ✔/✘，互不拖累。

【个人矿流程（``alliance_mining_personal``）】
1. **拉状态**：21039（``teachingID=-1``，10.3 抓包实测）→ ``personalList``；
2. **收矿**：``0 < endTime <= now 且 isReceived == 0`` 的矿点逐个发
   21049（收奖励是"开新矿"的前置闭环，所以属于挖矿任务的一部分）；
   ★ ``endTime`` 是**毫秒**（10.3 抓包），旧版拿秒比较导致永远收不了矿；
3. **开矿**：空闲矿点（``endTime == -1``）合成**一次** 21047 一键开矿。
   ★ 10.3 抓包（``captures/工会签到+个人挖矿+团队挖矿占坑位全流程.saz``）
   实证：``roleSidList`` **不能发空** —— 真实客户端每坑填
   ``roleMaxCondition``（``AlliancePersonalPitData`` 模板表）个角色，
   各坑角色互不重叠。

   ★★ **每坑的角色必须满足该坑的 7 类条件**（2026-10-06 补完）★★
   140 个模板**全部**要求元素 + 资质，84 个还要职业、64 个要星级、
   50 个要品质、5 个要种族。**只按等级从高到低挑人必然被服务端拒** ——
   这就是 21047 一直回 ``errorCode=-2`` 的根因。
   条件语义（反汇编 ``NewPitPersonalLimit.IsAchieveLimit`` 实证）：
   元素 / 资质 / 职业 / 种族是 **==**，星级 / 等级 / 品质是 **>=**；
   比较对象是**角色形态**（``RoleMainData``）的元素 ID / 资质档 / 职业 ID /
   种族 ID / 品质档 —— 这些值**在文本表里被抹空了**，只存在于
   ``DataRelative`` 那张"表间引用索引"里（见 ``models/game_config``）。
   挑不满条件的坑**整坑不发**，并如实报出缺的是哪条条件。

【团体矿流程（``alliance_mining_team``）】
1. 拉状态（同上，读 ``teamList`` / ``teamLimit``）；
2. **占坑**（★ 2026-10-05 修正空槽判据 + **修正角色匹配的 ID 空间**）：
   团体矿占坑是**二元问题** —— 空槽位（``memberPid == **-1**``，见
   ``models.alliance.NO_MEMBER_PID``）上只要名册里存在满足
   ``needRoleInfoID`` + ``needRoleLv`` 的角色（客户端显示 ＋ 号）就能占，
   具体占哪个角色不重要。

   ⚠️ **匹配时必须做 ID 换算**：名册里 ``RoleClass.roleID`` 是
   **RoleMainID（130xxxxxx 段）**，而槽位的 ``needRoleInfoID`` 是
   **RoleInfoID（133xxxxxx 段）** —— 两个**不同的 ID 空间**。
   过去写的是 ``role.roleID == slot.needRoleInfoID``，**永远为假**，
   于是每个槽位都报"名册中没有可用的匹配"（**根因不是名册没拉到**）。
   现在统一走 ``models.game_config.RoleIdMap.matches()``；反汇编取证与
   换算链见该类的 docstring。
   优先级：**空槽少的坑先占**（越接近满员越可能被队友补满），并列时
   **难度高的先占**（奖励更多）；

   ★★ **只发送"能填进坑"的数据（2026-10-05 晚）** ——
   客户端「点 ＋ 号」的守卫 ``PitRoleIcon.CheckCanJoinRole`` 自己判了三条，
   我们过去**一条都没判**，于是真机 3 次成功后连吃 15 个 ``-8``：
   （a）**每日总次数**：``TeamCount < TeamLimitCount``，
        ``TeamLimitCount`` 就是 21040 的 ``teamLimit``；
   （b）**每坑我最多 1 个**：``GetRoleJoinPitCount_Team(pitSid)
        < GetTeamPitRoleCountLimit()``；
   （c）**角色不能已在别的坑**：``IsRoleJoinPit_Team(roleInfoID)``
        —— 本项目目前只在**单次运行内**去重（``used_role_sids``），跨运行未实现。
   本任务实现了 (a)(b)，并用"连续两次被同一理由拒绝就停手"兜住 (c) 的残余。
3. **不发 21045 开矿包**：坑位满了游戏**自动开始挖矿**（用户实测 +
   10.3 抓包里没有 21045，互证）；
4. **能占几个 = 服务端给的额度**：``allowance = max(teamLimit − 我已占, 0)``。
   ⚠️ 这条闸门**被误删过一次**（白天把 ``teamLimit`` 误读成"按坑的限制"），
   删掉后真机立刻连吃 15 个 ``-8`` —— 详见 ``_team_allowance`` 的说明，
   **别再删**。

【"没入会"不算失败】
21039 回 ``errorCode=19`` → ``ok=True`` 并说明，与签到任务同一哲学。

【消息号与字段依据】
``models/alliance.py``（白名单表 + 模型）、``models/role.py``（名册）、
``models/game_config.load_personal_pit_conditions``（每坑人数）；
字段编号见 ``models/proto_fields.py``；取证见 ``notes/dossier/AllianceNewPit*.json``。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import ClassVar, Final

from client.game_client import GameClient
from models.alliance import (
    FORBIDDEN_PACKET_IDS,
    MINING_ALLOWED_PACKET_IDS,
    AllianceNewPitOneKeyStartPacket,
    AllianceNewPitOneKeyStartRes,
    AllianceNewPitPacket,
    AllianceNewPitRes,
    AllianceNewPitRewardPacket,
    AllianceNewPitRewardRes,
    AllianceNewPitSetRolePacket,
    AllianceNewPitSetRoleRes,
    AlliancePersonalPitClass,
    AllianceTeamPitClass,
    AllianceTeamPitDetailClass,
    ERR_NOT_IN_ALLIANCE,
    is_not_in_alliance,
    PitSetRoleType,
    error_code_name,
)
from models.game_config import (
    PersonalPitCondition,
    PitConditionTerm,
    RoleAttributes,
    RoleIdMap,
    TeamPitCondition,
    load_personal_pit_conditions,
    load_role_attributes,
    load_role_id_map,
    load_team_pit_conditions,
)
from models.role import (
    ALLOWED_PACKET_IDS as ROLE_ROSTER_ALLOWED_PACKET_IDS,
)
from models.role import GetAllRolePacket, GetAllRoleRes, RoleClass
from tasks.base import BaseTask, QuantityScope, TaskResult, register_task, reward_phrase
from tasks.packet_guard import guarded_send

_LOGGER: Final[logging.Logger] = logging.getLogger("tasks.alliance_mining")

#: 连续被**同一个错误码**拒绝多少次，就停止本轮继续占坑。
#:
#: 【为什么是 2（★ 2026-10-05 真机）】
#: 那天 3 次占坑成功后，连吃了 **15 个** ``errorCode=-8`` —— 后 14 发全是白发的。
#: 第一次失败可能是偶发（队友刚好先占了那个槽位），**连续两次同因**就足以说明
#: "再发同样的请求不会成功"。阈值取 2 而不是 1，是为了不被单次竞争误伤。
#:
#: ⚠️ 这里**不猜错误码的含义**：只把它当成"服务端连续用同一个理由拒绝我"这个
#: 可观测事实。``-8`` 至今没在任何工会枚举里找到，不许给它编名字。
CONSECUTIVE_FAILURE_STOP: Final[int] = 2

#: 每日团队矿占坑数的**防御性上限**。
#:
#: 【为什么是 4】游戏内公会说明原文（``LanguageSettings_zhcn`` 第 10227 行）：
#: 「公會LV1…挖礦次數 1 / LV2 2 / LV3 2 / LV4 3 / LV5 3 / LV6 3 / LV7 4 /
#: LV8 4 / LV9 4 / LV10 4」—— **这一列的最大值就是 4**，即公会练到顶也只给 4 次。
#: 10.3 抓包实测 ``teamLimit = 4``，与之吻合。
#:
#: 【它只是"兜底"，不是主判据】主判据是服务端下发的 ``teamLimit``
#: （见 :meth:`AllianceTeamMiningTask._team_allowance`）—— 它随公会等级/职称变化，
#: 比写死 4 准。这里再夹一道 4，是为了"服务端万一给出一个离谱的大数时不要发疯"。
#:
#: ⚠️ 真被这道上限夹住时会**打 WARNING 留痕**，绝不静默少占 —— 若哪天游戏把
#: 上限提到 5，日志会立刻告诉你，而不是安静地漏做一次。
MAX_TEAM_PITS_PER_DAY: Final[int] = 4

#: 汇总里最多列几条明细（其余折叠成"另 N 个见开发者日志"）。
#:
#: 【为什么要折叠】2026-10-05 真机那次的运行视图有 33 条 unfillable + 15 条失败，
#: 共约 60 行，人根本读不了。聚合后一眼能看出"卡在哪一类"，细节仍留在 DEBUG。
_MAX_LISTED_DETAILS: Final[int] = 3

#: "占不了坑"的原因分类（键 → 中文短标签）。
#:
#: 标签文字与 :meth:`AllianceMiningTeamTask._unfillable_reason` 的正文保持一致，
#: 这样聚合后的汇总与单条明细读起来是同一套说法。
UNFILLABLE_REASON_LABELS: Final[dict[str, str]] = {
    "missing": "名册里没有这个角色",
    "low_level": "等级不足",
    "used": "可用角色已被本轮其它坑位占用",
}


class _AllianceMiningBase(BaseTask):
    """个人 / 团体挖矿的公共骨架：拉状态、收矿、发名册。

    **为什么不注册成任务**：它只是共享代码的宿主，"挖矿"这个动作在两个
    子类里各有一套计划逻辑（个人 = 一键开矿，团体 = 进槽 + 等队友），
    强行抽成一个任务只会让结果行语义含糊。
    """

    name = ""  # 子类各自定义

    requires_auth = True

    #: 挖矿本身零消耗（开矿不花钱；"刷新矿点" 21041 之类消耗类报文在禁发清单里）
    spends_diamond = False

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.sent_packet_ids: list[int] = []
        #: 团体矿点模板表缓存（``{teamPitID: 条件}``；只用于排序，见
        #: :meth:`_team_pit_conditions`）。``None`` = 还没读过。个人矿任务不会
        #: 碰它 —— 惰性读取，不产生多余 IO。
        self._team_conditions: dict[int, TeamPitCondition] | None = None
        #: 角色属性表（元素 / 资质 / 职业 / 种族 / 品质）。``None`` = 还没读过。
        #: ★ 2026-10-06：个人矿按条件挑人的依据 —— 数据来自 ``DataRelative``
        #: （文本导出把 ``RoleMainData.element`` / ``roleQualification`` 等
        #: 引用列抹空了，那些值只在这张关系索引表里）。
        self._role_attribute_table: dict[int, RoleAttributes] | None = None
        #: 角色 ID 换算表（``RoleMainID`` → ``RoleInfoID``）。``None`` = 还没读过。
        #: ★ 2026-10-05：团体矿挑角色**必须**先换算 —— ``RoleClass.roleID`` 是
        #: RoleMainID(130xxxxxx)，而矿点的 ``needRoleInfoID`` 是 RoleInfoID(133xxxxxx)，
        #: 直接 ``==`` 永远为假。详见 :class:`models.game_config.RoleIdMap`。
        self._role_id_map: RoleIdMap | None = None

    # ------------------------------------------------------------------
    # 发包原语
    # ------------------------------------------------------------------
    def _fetch_pits(self, game: GameClient) -> AllianceNewPitRes:
        """拉取挖矿状态总表（21039 → 21040，只读）。"""
        view = guarded_send(
            game,
            AllianceNewPitPacket(),
            AllianceNewPitRes,
            allowed=MINING_ALLOWED_PACKET_IDS,
            forbidden=FORBIDDEN_PACKET_IDS,
            record=self.sent_packet_ids,
            logger=self.logger,
        )
        return view.decode_sub_packet(AllianceNewPitRes)

    def _fetch_roster(self, game: GameClient) -> GetAllRoleRes:
        """拉取角色名册（5011 → 5012，只读；团体矿进槽挑角色用）。"""
        view = guarded_send(
            game,
            GetAllRolePacket(),
            GetAllRoleRes,
            allowed=ROLE_ROSTER_ALLOWED_PACKET_IDS,
            forbidden=FORBIDDEN_PACKET_IDS,
            record=self.sent_packet_ids,
            logger=self.logger,
        )
        return view.decode_sub_packet(GetAllRoleRes)

    def _harvest_one(self, game: GameClient, pit_sid: int) -> tuple[bool, str, AllianceNewPitRewardRes]:
        """收一个已完成矿点的奖励（21049）。

        .. warning::
           **不重试**：奖励领取是一次性的，超时后服务端可能已发奖。
        """
        view = guarded_send(
            game,
            AllianceNewPitRewardPacket(pitSid=pit_sid),
            AllianceNewPitRewardRes,
            allowed=MINING_ALLOWED_PACKET_IDS,
            forbidden=FORBIDDEN_PACKET_IDS,
            record=self.sent_packet_ids,
            logger=self.logger,
        )
        res = view.decode_sub_packet(AllianceNewPitRewardRes)
        if not res.is_success():
            return False, f"errorCode={res.errorCode}（{error_code_name(res.errorCode)}）", res
        # 21050 gotList 的数量口径尚未实证 → 保守口径只报件数
        # （见 models/reward.py 的 QuantityScope.UNKNOWN）。
        return True, reward_phrase(res.gotList, scope=QuantityScope.UNKNOWN), res

    def _not_in_alliance_result(self, exc: AllianceNewPitRes | None = None) -> TaskResult:
        """"未加入工会"的标准结果（``ok=True``：无事可做不是失败）。"""
        message = "未加入工会，跳过挖矿"
        self.logger.info("%s", message)
        return TaskResult(task=self.name, ok=True, message=message, data={"skipped": True})

    # ------------------------------------------------------------------
    # 通用工具
    # ------------------------------------------------------------------
    @staticmethod
    def _now() -> int:
        """当前 Unix **毫秒**时间戳（矿点状态判断的时钟）。

        【★ 2026-10-03 单位修订】抓包实证矿点 ``endTime`` 是毫秒（13 位），
        旧版这里返回秒，``needs_harvest``/``is_mining`` 因此永远判不对 ——
        "收矿从未触发"的直接原因。
        """
        return int(time.time() * 1000)

    @staticmethod
    def _load_pit_conditions() -> dict[int, PersonalPitCondition] | None:
        """读个人矿点模板条件表（``AlliancePersonalPitData``）。

        :return: ``{personalPitID: 条件}``；读不到时返回 ``None``。

        【为什么这里吞掉异常、返回 None】
        每坑指派几个角色必须以模板表 ``roleMaxCondition`` 为准（10.3 抓包
        实证与客户端逐一相等）；拿不到表就**不猜人数** —— 宁可不开矿并
        如实说明，也不发一个"我方自己发明的请求形状"。
        """
        try:
            return load_personal_pit_conditions()
        except (OSError, ValueError) as exc:  # TableError 继承自 ValueError
            _LOGGER.warning("读取个人矿点模板条件失败（本次不开矿）：%s", exc)
            return None

    def _team_pit_conditions(self) -> dict[int, TeamPitCondition]:
        """读团体矿点模板表（``AllianceTeamPitData``）的**难度**，只用于排序。

        【为什么这里读不到就返回空表，而个人矿那边是 None】
        个人矿的模板表决定**请求形状**（每坑填几个角色）—— 读不到就不该发包，
        所以那边返回 ``None`` 让调用方停下来。团体矿占坑的请求里**不含**
        模板表的数据（21043 只带 ``pitSid`` / ``roleSidList`` / ``teamPitIndex``），
        ``difficulty`` 纯粹影响**排队顺序**：读不到时按难度 0 排（退化为
        "空槽少优先"），占坑本身照常进行 —— 没必要为了排序把功能停掉。

        【为什么缓存】一次运行里 ``_occupy_priority`` 会被调用 O(n log n) 次，
        每次都重读磁盘 —— 缓存到实例上，失败结果也缓存（不反复告警）。
        """
        if self._team_conditions is None:
            try:
                self._team_conditions = load_team_pit_conditions()
            except (OSError, ValueError) as exc:
                _LOGGER.warning(
                    "读取团体矿点模板表失败（本次退化为只按空槽数排序）：%s", exc
                )
                self._team_conditions = {}
        return self._team_conditions

    def _role_ids(self) -> RoleIdMap | None:
        """读角色 ID 换算表（``RoleMainData`` + ``RoleInfoData``）。

        :return: 换算表；读不到时返回 ``None``。

        【为什么读不到返回 ``None``，而不是像难度表那样退化成空表】

        难度表读不到只是"排序退化"，占坑照常。但换算表读不到 ⇒ **无法判断
        名册里有没有满足 ``needRoleInfoID`` 的角色**。此时若按空表处理，
        每个槽位都会被判成"名册中没有可用的匹配" —— 那是**一句假话**，
        会把"素材表缺失"误导成"我确实没有这个角色"，正是本项目最忌讳的
        那种"错误信息指向错误方向"。所以返回 ``None``，由调用方明确报
        "读不到映射表"，而不是编一个看起来合理的结论。

        【为什么缓存】一次运行里要对每个空槽做一次匹配，重读两张表
        （6653 + 587 行）不值得；失败结果也缓存（不反复告警）。
        """
        if self._role_id_map is None:
            try:
                self._role_id_map = load_role_id_map()
            except (OSError, ValueError) as exc:
                _LOGGER.warning("读取角色 ID 换算表失败：%s", exc)
        return self._role_id_map


def _no_startable_reason(notes: list[str]) -> str:
    """把"一个坑都开不成"的原因压成一句可读的话。

    :param notes: ``_build_one_key_elements`` 产出的分配告警（每个坑一条）。
    :return: 结果行用的说明。

    【为什么要把第一条摊开写】"没有可成行的矿点（见分配告警）"这种写法
    逼用户自己往上翻 —— 而缺元素（去抽卡）、缺资质（去升级）、凑不齐人数
    （少开几个坑）的处理办法完全不同。指向明确是项目「错误信息要指向正确
    方向」纪律的硬要求；但也不能把 7 个坑的告警全塞进结果行，所以只摊第一条。
    """
    if not notes:
        return "没有可成行的矿点，未发送 21047"
    if len(notes) == 1:
        return f"没有可成行的矿点：{notes[0]}"
    return f"没有可成行的矿点（{len(notes)} 个坑都不行，例：{notes[0]}）"


@register_task
class AllianceMiningPersonalTask(_AllianceMiningBase):
    """工会个人挖矿：收已完成矿 + 一键开空闲矿。"""

    name = "alliance_mining_personal"

    opt_in_hint: ClassVar[str] = "已加入工会才会执行；开矿不消耗资源，每日有次数上限（服务端控制）"

    # ------------------------------------------------------------------
    # 开矿（个人 = 一键）
    # ------------------------------------------------------------------
    def _role_attributes(self) -> dict[int, RoleAttributes] | None:
        """读角色属性表（元素 / 资质 / 职业 / 种族 / 品质）。

        :return: ``{roleMainID: RoleAttributes}``；读不到返回 ``None``。

        【为什么读不到要返回 ``None`` 而不是空表】与 ``_role_ids`` 同一条理由：
        空表会让**每一个坑**都被判成"名册里没有满足条件的角色" —— 那是假话，
        会把"素材表缺失"误导成"我确实没有合适的角色"。返回 ``None`` 由调用方
        明确报"读不到属性表"。
        """
        if self._role_attribute_table is None:
            try:
                self._role_attribute_table = load_role_attributes()
            except (OSError, ValueError) as exc:
                _LOGGER.warning("读取角色属性表失败：%s", exc)
        return self._role_attribute_table

    def _pick_roles_for_pit(
        self,
        condition: PersonalPitCondition,
        pool: list[RoleClass],
        used: set[int],
        attributes: Mapping[int, RoleAttributes] | None,
    ) -> tuple[list[RoleClass], str]:
        """按矿点的条件从名册里挑人。

        :param condition: 矿点模板（含 7 类条件）。
        :param pool: 候选角色，**已按 (等级, 星级) 降序**排好。
        :param used: 本轮已被别的坑占用的角色实例 sid。
        :return: ``(挑到的角色, 失败原因)``；成功时原因为空串。

        【算法 —— 与客户端 ``NewPit.PersonalCanStart`` 同构】
        1. 对每条条件：先数**已挑中**的角色里有几个满足它；不够 ``need`` 个就
           继续从池子里挑（挑最靠前的，即等级/星级最高的）；
        2. 全部条件都满足后，再补足到 ``roleMaxCondition`` 个；
        3. **任何一条条件凑不满就整坑放弃**（返回失败原因），绝不提交残阵
           —— 实测残阵会被服务端硬拒，且会连带拖垮同批的其它坑。

        【为什么要按条件挑而不是按等级挑】140 个模板**全部**要求元素 + 资质
        （84 个还要职业、64 个要星级、50 个要品质、5 个要种族）。只按等级挑，
        被服务端拒是必然的 —— 这就是 21047 一直回 ``-2`` 的根因。
        """
        chosen: list[RoleClass] = []
        picked: set[int] = set()

        def matches(role: RoleClass, term: PitConditionTerm) -> bool:
            return term.matches(
                attributes=attributes.get(role.roleID) if attributes else None,
                level=role.roleLV,
                star=role.roleStar,
            )

        for term in condition.terms:
            have = sum(1 for role in chosen if matches(role, term))
            while have < term.need:
                pick = next(
                    (
                        role
                        for role in pool
                        if role.roleSid not in used
                        and role.roleSid not in picked
                        and matches(role, term)
                    ),
                    None,
                )
                if pick is None:
                    return chosen, (
                        f"条件「{term.label}」需要 {term.need} 个角色，"
                        f"名册里只凑到 {have} 个"
                    )
                chosen.append(pick)
                picked.add(pick.roleSid)
                have += 1

        for role in pool:
            if len(chosen) >= condition.role_max:
                break
            if role.roleSid in used or role.roleSid in picked:
                continue
            chosen.append(role)
            picked.add(role.roleSid)

        if len(chosen) < condition.role_max:
            return chosen, (
                f"需要 {condition.role_max} 个角色，名册只凑到 {len(chosen)} 个"
            )
        return chosen, ""

    def _build_one_key_elements(
        self,
        idle_pits: list[AlliancePersonalPitClass],
        roster: GetAllRoleRes,
        conditions: dict[int, PersonalPitCondition],
    ) -> tuple[list[AlliancePersonalPitClass], list[str]]:
        """给空闲矿点填 ``roleSidList``，构造 21047 的请求元素。

        【规则（★ 10.3 抓包实证 + ★ 2026-10-06 条件建模）】
        - ``roleSidList`` **不能为空**：真实客户端每坑填的人数与模板表
          ``roleMaxCondition`` 逐一相等（抓包 8 坑 = 2/2/4/5/6/6/6/6 个）；
        - 各坑角色**互不重叠**（抓包 8 坑 37 个 sid 无一重复）——
          一个角色同一时间只能在一个坑里干活；
        - **每坑的角色必须满足该坑的 7 类条件**（元素/资质/职业/星级/等级/
          品质/种族），比较语义见 :class:`models.game_config.PitConditionTerm`。
          挑不满就**整坑放弃**，不提交必被拒的编队。

        :return: ``(可直接发包的请求元素, 分配过程中的说明/告警列表)``。
            元素是**新建**的（只带 sid/id/roleSidList，endTime/isReceived 保持
            默认 0），与抓包里客户端的请求形状一致 —— 绝不把 21040 解码出的
            ``endTime=-1`` 原样带回请求里。
        """
        attributes = self._role_attributes()
        if attributes is None:
            return [], [
                "读不到角色属性表（DataRelative / RoleMainData / RoleInfoData），"
                "无法判断角色是否满足矿点条件，未开矿"
            ]

        elements: list[AlliancePersonalPitClass] = []
        notes: list[str] = []
        # 优先用等级高、星级高的角色（与客户端"挑最强的"取向一致）
        pool = sorted(
            roster.roleList,
            key=lambda role: (role.roleLV, role.roleStar),
            reverse=True,
        )
        used: set[int] = set()
        for pit in idle_pits:
            condition = conditions.get(pit.id)
            if condition is None or condition.role_max <= 0:
                notes.append(
                    f"矿点 {pit.sid}（模板 {pit.id}）在配置表里没有可用的"
                    "roleMaxCondition，已跳过"
                )
                continue
            chosen, reason = self._pick_roles_for_pit(
                condition, pool, used, attributes
            )
            if reason:
                # 报缺要带上"这个坑要什么条件" —— 否则用户只知道"凑不齐"，
                # 不知道该去练哪个角色（项目「错误信息要指向正确方向」纪律）。
                where = f"矿点 {pit.sid}"
                if condition.title:
                    where += f"（{condition.title}）"
                notes.append(
                    f"{where} 条件：{condition.describe_terms()} —— "
                    f"{reason}，已跳过（不提交残阵）"
                )
                continue
            used.update(role.roleSid for role in chosen)
            elements.append(
                AlliancePersonalPitClass(
                    sid=pit.sid,
                    id=pit.id,
                    roleSidList=[role.roleSid for role in chosen],
                )
            )
        return elements, notes

    def _one_key_start(
        self, game: GameClient, elements: list[AlliancePersonalPitClass]
    ) -> tuple[bool, str, AllianceNewPitOneKeyStartRes]:
        """对给定的请求元素发一次一键开矿（21047）。

        :param elements: :meth:`_build_one_key_elements` 的产物
            （``roleSidList`` 已按模板表人数填好）。

        .. warning::
           **不重试**：开矿消耗今日次数，超时后服务端可能已经开了。

        【★ include_defaults=True 必须（Q-050，2026-10-04 定案）】
        矿点元素的 ``endTime=0 / isReceived=0`` 是服务端字节校验的一部分：
        省略它们（每元素少 4 B 的 ``20 00 28 00``）会被网关以
        22 字节 ``{"response_code":"OK"}`` 硬拒 —— 与 11009 少
        ``useSweepTicket=0``、39011 少 ``actionType=0`` 完全同源
        （glory_summit_capture.md 第九节情形①）。
        """
        view = guarded_send(
            game,
            AllianceNewPitOneKeyStartPacket.for_pits(elements),
            AllianceNewPitOneKeyStartRes,
            allowed=MINING_ALLOWED_PACKET_IDS,
            forbidden=FORBIDDEN_PACKET_IDS,
            record=self.sent_packet_ids,
            logger=self.logger,
            include_defaults=True,
        )
        res = view.decode_sub_packet(AllianceNewPitOneKeyStartRes)
        if not res.is_success():
            return False, f"errorCode={res.errorCode}（{error_code_name(res.errorCode)}）", res
        return True, f"已提交 {len(elements)} 个矿点", res

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def execute(self) -> TaskResult:
        with self.open_game_client() as game:
            if self.dry_run:
                return self._dry_run(game)

            now = self._now()
            state = self._fetch_pits(game)
            if not state.is_success():
                if is_not_in_alliance(state.errorCode):
                    return self._not_in_alliance_result()
                message = (
                    f"拉取挖矿状态失败：errorCode={state.errorCode}"
                    f"（{error_code_name(state.errorCode)}）"
                )
                self.logger.warning("%s", message)
                return TaskResult(task=self.name, ok=False, message=message)

            # ② 收矿（闭环前置：不收的矿会占着坑）
            harvested: list[str] = []
            harvest_failed: list[str] = []
            for pit in state.personalList:
                if not pit.needs_harvest(now):
                    continue
                ok, text, _res = self._harvest_one(game, pit.sid)
                if ok:
                    harvested.append(text)
                    # 裸 pit.sid 是**开发者日志**内容（运行视图只报"收了几个矿"）
                    self.logger.debug("✔ 已收矿 矿点 %s：%s", pit.sid, text)
                else:
                    harvest_failed.append(text)
                    self.logger.warning("✘ 收矿失败：%s", text)

            # ③ 开矿（空闲矿点合成一次一键请求；★ 先拉名册给每坑填角色）
            idle = [pit for pit in state.personalList if pit.is_idle()]
            started: list[str] = []
            start_error = ""
            allocation_notes: list[str] = []
            if idle:
                roster = self._fetch_roster(game)
                conditions = self._load_pit_conditions()
                if conditions is None:
                    start_error = (
                        "读不到 AlliancePersonalPitData 模板表（无法确定每坑角色数），未开矿"
                    )
                elif not roster.roleList:
                    start_error = "名册（5012）为空，无法给矿点指派角色，未开矿"
                else:
                    elements, allocation_notes = self._build_one_key_elements(
                        idle, roster, conditions
                    )
                    if elements:
                        ok, text, res = self._one_key_start(game, elements)
                        if ok:
                            started = [str(pit.sid) for pit in elements]
                            self.logger.info("✔ 一键开矿 %s", text)
                        else:
                            start_error = text
                            self.logger.warning("✘ 一键开矿失败：%s", text)
                    else:
                        # ★ 2026-10-06：把**第一条**具体原因直接写进结果行。
                        #   旧文案是"见分配告警"，用户得往上翻才知道缺的是哪条条件
                        #   —— 而"缺元素/缺资质/凑不齐人数"的处理办法完全不同
                        #   （去抽卡 / 去升级 / 少开几个坑），指向必须明确。
                        start_error = _no_startable_reason(allocation_notes)
                for note in allocation_notes:
                    self.logger.warning("⚠ 角色分配：%s", note)

        mining_now = sum(1 for pit in state.personalList if pit.is_mining(now))
        parts: list[str] = []
        if harvested:
            parts.append(f"收矿 {len(harvested)} 个")
        if started:
            parts.append(f"开矿 {len(started)} 个")
        if mining_now:
            parts.append(f"挖掘中 {mining_now} 个")
        if not parts:
            # ★ 2026-10-05 文案修正：`parts` 只统计"收矿/开矿/挖掘中"三种
            #   **动作**，压根不反映 `idle` 是否为空。过去一律输出
            #   "没有可收的矿，也没有空闲矿点"，于是在"**有**空闲矿点、但开矿
            #   被服务端拒绝"时会谎报"没有空闲矿点" —— 直接把排查方向带偏。
            #   文案必须与事实一致：这是项目「运行视图纪律」的硬要求。
            if idle:
                parts.append(f"有 {len(idle)} 个空闲矿点，但这次一个都没开成")
            else:
                parts.append("没有可收的矿，也没有空闲矿点")
        message = "个人矿：" + "，".join(parts)
        if start_error:
            message += f"；开矿失败：{start_error}"

        ok = not harvest_failed and not start_error
        if idle and not started and not start_error:
            # 理论不可达，防御：有闲矿却既没开也没报错
            ok = False
        return TaskResult(
            task=self.name,
            ok=ok,
            message=message,
            data={
                # 只留计数：逐条奖励文本已由 message / DEBUG 日志承载，
                # data 不再重复一份（2026-10-04「data 一并收敛」）
                "harvested_count": len(harvested),
                "harvest_failed_count": len(harvest_failed),
                "started": tuple(started),
                "start_error": start_error,
                "allocation_notes": tuple(allocation_notes),
                "mining": mining_now,
                "personal_limit": state.personalLimit,
            },
        )

    def _dry_run(self, game: GameClient) -> TaskResult:
        """演练：只组装"拉状态"这一包（开矿内容要等 21040 响应才能确定）。"""
        request = game.build(AllianceNewPitPacket())
        print(request.describe())
        print()
        print(
            "一键开矿请求要等 21040 响应才能确定矿点清单"
            "（将收已完成矿 + 对空闲矿发一次 21047）；"
            "每坑的角色会**按该坑的元素/资质/职业/星级/等级/品质/种族条件**挑"
            "（挑不满的坑不发）；未发送任何请求。"
        )
        print("—— 以上为演练输出，未发送任何请求 ——")
        return TaskResult(
            task=self.name,
            ok=True,
            message=f"演练完成：21039 请求体 {len(request.body)} 字节，未发送",
            data={"body_size": len(request.body)},
        )


@register_task
class AllianceMiningTeamTask(_AllianceMiningBase):
    """工会团体挖矿：占坑位（二元判断 + 优先级排序）。

    【★ 2026-10-05 根因修正（此前"没有可占的坑位"的原因）】
    空槽位的哨兵值是 ``memberPid == **-1**``，**不是 0**。旧判据
    ``not slot.memberPid``（等价 ``== 0``）因为 ``-1`` 是 truthy 而恒为假 →
    空槽列表恒空 → 任务什么都没做、只报「没有可占的坑位」。
    取证与闭环佐证见 ``models.alliance.NO_MEMBER_PID``。

    【占坑是二元问题】
    空槽位上，只要名册里存在满足 ``needRoleInfoID`` + ``needRoleLv`` 的角色
    （客户端显示 ＋ 号），就能占；具体占哪个角色不重要。

    - **不发 21045 开矿包**：坑位满了游戏**自动开始挖矿**
      （用户实测；10.3 抓包也确实没有 21045）；
    - **能占几个 = 服务端给的额度**（★ 2026-10-05 晚**恢复**）：
      ``allowance = max(teamLimit − 我已占槽位数, 0)``。
      ⚠️ 白天那一版曾把 ``teamLimit`` 误读成"按坑的限制"而**删掉这条闸门**，
      真机随即 3 次成功后连吃 15 个 ``-8``。**误读已撤回，闸门已恢复** ——
      完整证据链见 :meth:`_team_allowance`，**别再删**。
    - **每个坑我最多占 1 个槽位**（★ 2026-10-05 晚新增）：
      游戏内文案「每個團隊挖礦公會成員最多僅能上陣1個角色」，
      客户端判据 ``GetRoleJoinPitCount_Team(pitSid) < GetTeamPitRoleCountLimit()``。
      所以同一个坑里我占第 2 个槽位**必然被拒** —— 那一发不该发。
    - **连续两次被同一理由拒绝就停手**：见 :data:`CONSECUTIVE_FAILURE_STOP`。
      真机那次 15 个 ``-8`` 里后 14 发全是白发的；这里**不猜错误码的含义**，
      只把它当"服务端在用同一个理由拒绝我"这个可观测事实。
    - **优先级**（同一次运行里多个可占的坑怎么排队）：
      ① 剩余（空）槽位**少**的坑优先 —— 越接近满员越可能被队友补满、
      自动开矿，成功几率最高；
      ② 并列时，**难度**（``AllianceTeamPitData.difficulty``）**高**的优先
      —— 奖励更多。10.3 抓包实测槽位数严格等于模板表 ``roleMaxCondition``
      且只由难度决定，故这与旧的"总坑位数多优先"数值等价，改成读难度是
      把语义写在代码里。
    - **角色不复用**（游戏规则等价实现）：一个角色只能占一个坑 ——
      游戏里占完一个坑后，同角色可占的其他坑会灰掉；脚本在本次运行内
      用 ``used_role_sids`` 去重，已占坑的角色服务端也会拒。
    """

    name = "alliance_mining_team"

    opt_in_hint: ClassVar[str] = (
        "已加入工会才会执行；只要名册里有满足槽位要求的角色就能占坑，"
        "次数受「每日团队矿上限」约束（用满即跳过），坑位满员后自动开矿（无需开矿包）"
    )

    # ------------------------------------------------------------------
    # 进槽 / 开矿
    # ------------------------------------------------------------------
    def _join_slot(
        self,
        game: GameClient,
        pit_sid: int,
        slot_index: int,
        role_sid: int,
    ) -> tuple[bool, str, int]:
        """把角色放进团体矿的指定槽位（21043 ``type=Team_In``）。

        :return: ``(是否成功, 人读文案, 错误码)``。成功时错误码为 ``0``。
            错误码单独返回是为了让调用方能判"**连续同一个错误码**"
            （见 :data:`CONSECUTIVE_FAILURE_STOP`），而**不必**把它写进运行视图
            —— 用户看到的日志里不该出现 ``errorCode=-8`` 这种读不懂的东西。

        .. warning::
           **不重试**：槽位操作有服务端状态，超时后可能已经生效。
        """
        # ★ include_defaults=True（Q-050）：teamPitIndex=0（0 号槽位）时
        # 该字段会被省略 → 服务端字节校验拒收（与 21047 同源）。
        view = guarded_send(
            game,
            AllianceNewPitSetRolePacket(
                type=PitSetRoleType.TEAM_IN,
                pitSid=pit_sid,
                roleSidList=[role_sid],
                teamPitIndex=slot_index,
            ),
            AllianceNewPitSetRoleRes,
            allowed=MINING_ALLOWED_PACKET_IDS,
            forbidden=FORBIDDEN_PACKET_IDS,
            record=self.sent_packet_ids,
            logger=self.logger,
            include_defaults=True,
        )
        res = view.decode_sub_packet(AllianceNewPitSetRoleRes)
        if not res.is_success():
            # 错误码**只**出现在开发者日志与结构化 data 里，不进运行视图。
            self.logger.debug(
                "占坑被拒 矿点 %s 槽位 %s：errorCode=%s（%s）",
                pit_sid,
                slot_index,
                res.errorCode,
                error_code_name(res.errorCode),
            )
            return False, f"矿点 {pit_sid} 槽位 {slot_index} 被服务端拒绝", res.errorCode
        return True, f"角色 {role_sid} 已进入矿点 {pit_sid} 的 {slot_index} 号槽位", 0

    @staticmethod
    def _empty_slots(pit: AllianceTeamPitClass) -> list[int]:
        """坑位里**还没被任何人占**的槽位下标（客户端显示 ＋ 号的那些）。

        .. note::
           ★ **2026-10-05 根因修正**：判据从 ``not slot.memberPid``（等价
           ``== 0``）改为 :meth:`AllianceTeamPitDetailClass.is_empty`
           （``memberPid <= 0``）。实测空槽哨兵是 **-1** 而非 0，旧的 truthy
           判据因此恒为假 ——"团体矿永远显示没有可占的坑位"就是这么来的。
           取证见 ``models.alliance.NO_MEMBER_PID``。
        """
        return pit.empty_slot_indices()

    @staticmethod
    def _unfillable_reason(
        pit_sid: int,
        slot_index: int,
        slot: AllianceTeamPitDetailClass,
        owned: list[RoleClass],
        role_ids: RoleIdMap,
    ) -> tuple[str, str]:
        """解释"这个槽位为什么占不了"——**把三种原因分开说**。

        :return: ``(分类键, 明细文案)``。分类键取自
            :data:`UNFILLABLE_REASON_LABELS`，供汇总聚合用。

        【为什么值得单独写一个方法（2026-10-05）】
        旧实现只会输出「名册中没有可用的匹配」，而这句话其实覆盖了三种
        完全不同的情况：① 压根没这个角色；② 有但等级不够；③ 有也够级、
        但本轮已经被别的坑位用掉了。三种情况的**处理办法完全不同**
        （①去抽卡、②去升级、③把坑位分开跑），笼统一句话等于没给信息 ——
        正是项目「错误信息要指向正确方向」这条纪律要避免的。
        """
        need_name = role_ids.name_of_info(slot.needRoleInfoID) or (
            f"角色 {slot.needRoleInfoID}"
        )
        where = f"矿点 {pit_sid} 槽位 {slot_index}"
        need = f"需要「{need_name}」≥{slot.needRoleLv} 级"
        if not owned:
            return "missing", f"{where}（{need}：{UNFILLABLE_REASON_LABELS['missing']}）"
        best_level = max(role.roleLV for role in owned)
        if best_level < slot.needRoleLv:
            return (
                "low_level",
                f"{where}（{need}：名册里有 {len(owned)} 个，"
                f"最高只有 {best_level} 级）",
            )
        return (
            "used",
            f"{where}（{need}：{UNFILLABLE_REASON_LABELS['used']}）",
        )

    @staticmethod
    def _aggregate(items: list[tuple[str, str]]) -> str:
        """把 ``[(分类键, 明细), …]`` 折成"分类计数 + 少量样例"的一行。

        :return: 形如 ``名册里没有这个角色 ×18、等级不足 ×14``；
            后面最多再跟 :data:`_MAX_LISTED_DETAILS` 条明细，其余折叠。

        【为什么要折叠】见 :data:`_MAX_LISTED_DETAILS` —— 真机那次 33 条明细
        把运行视图刷成了 60 行，等于没有信息。聚合后保留"卡在哪一类"与
        "几个具体例子"，全量明细进 DEBUG。
        """
        counts: dict[str, int] = {}
        for key, _detail in items:
            counts[key] = counts.get(key, 0) + 1
        parts = [
            f"{UNFILLABLE_REASON_LABELS.get(key, key)} ×{count}"
            for key, count in sorted(counts.items(), key=lambda kv: -kv[1])
        ]
        text = "、".join(parts)
        shown = items[:_MAX_LISTED_DETAILS]
        if shown:
            text += "；例：" + "；".join(detail for _key, detail in shown)
        if len(items) > len(shown):
            text += f"（另 {len(items) - len(shown)} 个见开发者日志）"
        return text

    # ------------------------------------------------------------------
    # 占坑配额（★ 2026-10-05 晚：客户端的三条限制，这里判前两条）
    # ------------------------------------------------------------------
    def _my_slots(self, pit: AllianceTeamPitClass, my_pid: int) -> list[int]:
        """这个坑里**已经被我占**的槽位下标。

        .. note::
           字段口径已核实：抓包里 ``slot.memberPid == slot.member(IconClass).playerID``，
           都是**玩家 ID**。``my_pid <= 0``（未登录 / 哨兵）时一律返回空 ——
           否则会把空槽的 ``-1`` 哨兵算成"我占的"。
        """
        if my_pid <= 0:
            return []
        return [
            index
            for index, slot in enumerate(pit.detaliClassList)
            if slot.memberPid == my_pid
        ]

    def _team_allowance(self, team_limit: int, already_mine: int) -> tuple[int | None, str]:
        """今日还能占几个坑（``None`` = 不设闸门）。

        :param team_limit: 21040 的 ``teamLimit``（= 客户端 ``m_teamLimitCount``）。
        :param already_mine: 我在 ``teamList`` 里已占的槽位数。
        :return: ``(额度, 备注)``；额度为 ``None`` 表示**读不到上限，不做本地判断**。

        【★★ 2026-10-05 晚：这条闸门是被"误删"后恢复的，别再删一次】

        上一轮（同日白天）把 ``teamLimit`` 判成"按坑的限制、不能当配额用"，
        据此删掉了 ``teamLimit − already_mine`` 这个闸门 —— **那是误读**：

        - ``AllianceNewPitRes`` 只有 5 个字段，``teamLimit`` 就是**一个 int**；
        - 被当成"按坑配额表"的 ``m_lzt_newPitLimit_Team`` 其实是**坑的槽位列表**
          （``NewPitTeamLimit`` 这个类只包了一个 ``AllianceTeamPitDetailClass``），
          **跟配额毫无关系**；
        - 真正的配额是 ``m_teamLimitCount``(0x94) vs ``m_teamCount``(0xA4)，
          ``PitRoleIcon.IsTeamCountMax()`` 比的正是这两个；
          ``UpdatePitTeamCountText()`` 用 ``TeamRemainCount`` + ``TeamLimitCount``
          渲染「剩余/上限」。

        删掉闸门的直接后果：真机 3 次成功后**连吃 15 个 -8**（全是白发）。

        【为什么 ``teamLimit <= 0`` 时**不**当成 0】
        那意味着服务端没下发这个值（老版本 / 异常），此时退化为"服务端裁决"
        （= 上一轮的行为）并告警。宁可多发几次，也不要因为读不到配额就**静默漏做**。
        """
        if team_limit <= 0:
            note = (
                f"21040 没给出团队矿次数上限（teamLimit={team_limit}），"
                "本次不做本地配额判断，由服务端裁决"
            )
            _LOGGER.warning("%s", note)
            return None, note
        allowance = max(team_limit - already_mine, 0)
        if team_limit > MAX_TEAM_PITS_PER_DAY:
            # 服务端给出的上限比游戏内已知的最大值还大 —— 要么游戏改版了，
            # 要么字段读错了。**夹住并留痕**，绝不静默按大数发一堆请求。
            note = (
                f"21040 给的每日上限 {team_limit} 超过已知最大值 "
                f"{MAX_TEAM_PITS_PER_DAY}，本次按 {MAX_TEAM_PITS_PER_DAY} 处理"
                "（若游戏确实提高了上限，请更新 MAX_TEAM_PITS_PER_DAY）"
            )
            _LOGGER.warning("%s", note)
            allowance = min(allowance, MAX_TEAM_PITS_PER_DAY)
            return allowance, note
        return allowance, ""

    def _occupy_priority(self, pit: AllianceTeamPitClass) -> tuple[int, int]:
        """占坑优先级排序键（越小越先）。

        ① 剩余（空）槽位**少**的先 —— 越接近满员，越可能被队友补满自动开矿；
        ② 并列时**难度高**的先 —— 奖励更多。

        【★ 2026-10-05 次键由"槽位数"改为"难度"】
        旧实现用 ``-len(detaliClassList)``。实测（10.3 抓包 18/18 吻合）槽位数
        **严格等于**模板表 ``AllianceTeamPitData.roleMaxCondition``，而它只由
        ``difficulty`` 决定（难度 1→2 槽、2→3、3→4、4→5、5→6）—— 所以两个键
        在数值上等价。改成直接读 ``difficulty`` 是为了**把语义写在代码里**：
        "同空槽数下先占奖励多的"，而不是"先占列表长的"。

        :param pit: 团体矿点。
        :return: ``(空槽数, -难度)``。难度读不到模板表时按 0 处理（排在最后，
            不阻断占坑 —— 服务端是最终裁判）。
        """
        difficulty = 0
        condition = self._team_pit_conditions().get(pit.id)
        if condition is not None:
            difficulty = condition.difficulty
        return (len(self._empty_slots(pit)), -difficulty)

    def _my_player_id(self) -> int:
        """自己的玩家 ID。

        用途：① 数"我在各坑里已占几个"（:meth:`_my_slots`）——
        **这是每日配额闸门的输入**（2026-10-05 晚起不再只是展示值）；
        ② 报错时能说清是谁。

        .. note::
           字段口径已核实：抓包里 ``slot.memberPid == slot.member(IconClass).playerID``，
           都是玩家 ID。返回 0 表示拿不到（未登录），调用方必须按"数不出我占的"
           处理，**不能**拿 0 去比对 ``memberPid``。
        """
        game_state = self.session.game_state
        if game_state is None:
            return 0
        return int(game_state.player_id)

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def execute(self) -> TaskResult:
        with self.open_game_client() as game:
            if self.dry_run:
                return self._dry_run(game)

            now = self._now()
            state = self._fetch_pits(game)
            if not state.is_success():
                if is_not_in_alliance(state.errorCode):
                    return self._not_in_alliance_result()
                message = (
                    f"拉取挖矿状态失败：errorCode={state.errorCode}"
                    f"（{error_code_name(state.errorCode)}）"
                )
                self.logger.warning("%s", message)
                return TaskResult(task=self.name, ok=False, message=message)

            # ① 统计"我已经占了哪些坑的哪些槽位"
            #
            #    ★ 2026-10-05 晚：``already_mine`` 从"仅观察值"升格为**配额闸门的输入**
            #    （客户端 ``IsTeamCountMax()`` 比的就是 TeamCount vs TeamLimitCount）。
            #    判据用 ``my_pid > 0``：为 0（未登录）或 -1 时不能比对 ``memberPid``
            #    —— 否则会把空槽的哨兵值算成"我占的"。
            my_pid = self._my_player_id()
            mine_by_pit = {pit.sid: self._my_slots(pit, my_pid) for pit in state.teamList}
            already_mine = sum(len(slots) for slots in mine_by_pit.values())

            # ② 收集"没在挖、还有空槽、**且我还没占过**"的坑位，按优先级排队
            #
            #    ★ 最后一条是 2026-10-05 晚新增：游戏规则是「每個團隊挖礦公會成員
            #    **最多僅能上陣1個**角色」（客户端 ``GetRoleJoinPitCount_Team(pitSid)
            #    < GetTeamPitRoleCountLimit()``）。同一个坑里我占第二个槽位**必然被拒**，
            #    那一发请求根本不该发出去。
            #
            #    名册只在确实要挑角色时才拉 —— 5011 是只读，但没必要时多一发是一发。
            candidates = [
                pit
                for pit in state.teamList
                if not pit.is_mining(now)
                and self._empty_slots(pit)
                and not mine_by_pit[pit.sid]
            ]
            candidates.sort(key=self._occupy_priority)

            # ③ ★★ 每日次数闸门 ★★
            #
            #    【这条闸门被误删过一次，别再删】详见 :meth:`_team_allowance`。
            #    额度为 0 ⇒ **一个占坑包都不发**，直接跳过（不是失败）。
            allowance, gate_note = self._team_allowance(state.teamLimit, already_mine)

            if allowance == 0:
                message = (
                    f"今日团队矿次数已用尽（上限 {state.teamLimit}，"
                    f"我已占 {already_mine} 个），跳过占坑"
                )
                self.logger.info("%s", message)
                return TaskResult(
                    task=self.name,
                    ok=True,
                    message=message,
                    data={
                        "joined": (),
                        "join_failed": (),
                        "unfillable": (),
                        "mining": sum(1 for pit in state.teamList if pit.is_mining(now)),
                        "team_limit": state.teamLimit,
                        "already_mine": already_mine,
                        "allowance": 0,
                        "skipped": True,
                    },
                )

            remaining = allowance  # ``None`` = 不设闸门（服务端裁决）
            roster = self._fetch_roster(game) if candidates else None
            role_ids = self._role_ids() if candidates else None
            used_role_sids: set[int] = set()

            joined: list[str] = []
            join_failed: list[tuple[int, int, int]] = []  # (矿点, 槽位, 错误码)
            unfillable: list[tuple[str, str]] = []  # (分类, 明细)
            blocked = ""
            stop_reason = ""
            last_code = 0
            same_code_run = 0

            if candidates and role_ids is None:
                # 换算表读不到 ⇒ 无法判断名册能否满足要求。**明确报出来**，
                # 绝不按"名册里没有"处理 —— 那是一句假话，会把"素材表缺失"
                # 误导成"我确实没这个角色"（详见 _role_ids 的说明）。
                blocked = (
                    "读不到角色 ID 换算表（RoleMainData / RoleInfoData），"
                    "无法判断名册是否满足矿点要求，未占坑"
                )
                self.logger.warning("%s", blocked)
            elif candidates:
                assert roster is not None and role_ids is not None
                for pit in candidates:
                    if remaining is not None and remaining <= 0:
                        break
                    for slot_index in self._empty_slots(pit):
                        if remaining is not None and remaining <= 0:
                            break
                        slot = pit.detaliClassList[slot_index]
                        # ★★ 2026-10-05 核心修复 ★★
                        # 必须先把 ``role.roleID``（**RoleMainID**，130xxxxxx）
                        # 换算成 RoleInfoID（133xxxxxx），才能跟
                        # ``slot.needRoleInfoID``（RoleInfoID）比 —— 两者是
                        # **不同的 ID 空间**，过去直接 ``==`` 永远为假，
                        # 于是每个槽位都报"名册中没有可用的匹配"。
                        # 反汇编依据见 models/game_config.RoleIdMap。
                        owned = [
                            role
                            for role in roster.roleList
                            if role_ids.matches(role.roleID, slot.needRoleInfoID)
                        ]
                        candidates_roles = [
                            role
                            for role in owned
                            if role.roleLV >= slot.needRoleLv
                            and role.roleSid not in used_role_sids
                        ]
                        if not candidates_roles:
                            unfillable.append(
                                self._unfillable_reason(
                                    pit.sid, slot_index, slot, owned, role_ids
                                )
                            )
                            continue
                        role_sid = candidates_roles[0].roleSid
                        ok, text, code = self._join_slot(
                            game, pit.sid, slot_index, role_sid
                        )
                        if ok:
                            used_role_sids.add(role_sid)
                            joined.append(text)
                            if remaining is not None:
                                remaining -= 1
                            last_code = 0
                            same_code_run = 0
                            self.logger.info("✔ %s", text)
                            # ★ 每坑只占 1 个（客户端规则）—— 占成就换下一个坑。
                            break
                        join_failed.append((pit.sid, slot_index, code))
                        self.logger.debug("✘ 占坑失败 矿点 %s 槽位 %s", pit.sid, slot_index)
                        # 连续同一个错误码 ⇒ 再发同样的请求不会成功，停手。
                        # **不猜错误码的含义**，只把它当"同一个理由"用。
                        same_code_run = same_code_run + 1 if code == last_code else 1
                        last_code = code
                        if same_code_run >= CONSECUTIVE_FAILURE_STOP:
                            stop_reason = (
                                f"连续 {same_code_run} 次被服务端以同一理由拒绝，"
                                "已停止继续占坑（错误码见开发者日志）"
                            )
                            break
                    if stop_reason:
                        break

        mining_now = sum(1 for pit in state.teamList if pit.is_mining(now))
        parts: list[str] = []
        if mining_now:
            parts.append(f"挖掘中 {mining_now} 个")
        if joined:
            parts.append(f"占坑 {len(joined)} 次")
        if not parts:
            # ★ 2026-10-05：有候选坑位却没占上时，必须说清**为什么**。
            #   过去一律输出"没有可占的坑位"，把"匹配不上"说成了"没有坑位"，
            #   与项目「错误信息要指向正确方向」的纪律相悖。
            if blocked:
                parts.append(blocked)
            elif candidates:
                parts.append(f"有 {len(candidates)} 个坑位可占，但名册挑不出可用角色")
            else:
                parts.append("没有可占的坑位")
        message = "团体矿：" + "，".join(parts)
        if gate_note:
            message += f"；{gate_note}"
        if unfillable:
            message += f"；无法占坑 {len(unfillable)} 个（{self._aggregate(unfillable)}）"
        if join_failed:
            # ⚠️ 这里**只报次数**，不带错误码 —— 用户读不懂 `-8`，
            #    而它对我们排查有用，所以留在 DEBUG 与 data 里。
            message += f"；占坑被拒 {len(join_failed)} 次"
        if stop_reason:
            message += f"；{stop_reason}"

        ok = not join_failed
        return TaskResult(
            task=self.name,
            ok=ok,
            message=message,
            data={
                "joined": tuple(joined),
                "join_failed": tuple(
                    f"矿点 {pit_sid} 槽位 {index}" for pit_sid, index, _code in join_failed
                ),
                #: 结构化错误码（**只在这里与 DEBUG 日志出现**，不进运行视图）
                "join_error_codes": tuple(code for _pit, _index, code in join_failed),
                "unfillable": tuple(detail for _key, detail in unfillable),
                #: ``teamLimit`` = 每日团队矿次数上限（客户端 ``m_teamLimitCount``）
                "team_limit": state.teamLimit,
                "already_mine": already_mine,
                #: 本次实际可占几个（``None`` = 读不到上限、没设闸门）
                "allowance": allowance,
                "stop_reason": stop_reason,
            },
        )

    def _dry_run(self, game: GameClient) -> TaskResult:
        """演练：只组装"拉状态"与"拉名册"两包（占坑要等响应才能确定）。"""
        request = game.build(AllianceNewPitPacket())
        print(request.describe())
        roster_request = game.build(GetAllRolePacket())
        print(roster_request.describe())
        print()
        print(
            "占坑（21043）要等 21040 / 5012 响应才能确定"
            "（将按'空槽少的坑优先、并列时难度高的优先'排队；"
            "只对**名册有匹配角色**的空槽发，且**每个坑最多占 1 个**、"
            "总数不超过 21040 给出的「每日上限 − 我已占」；"
            "坑位满员后游戏自动开矿，本任务不发 21045）；未发送任何请求。"
        )
        print("—— 以上为演练输出，未发送任何请求 ——")
        return TaskResult(
            task=self.name,
            ok=True,
            message=f"演练完成：21039 请求体 {len(request.body)} 字节，未发送",
            data={"body_size": len(request.body)},
        )
