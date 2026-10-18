"""角色名册报文（消息号 5011/5012）+ 角色数据的两个来源。

【这个模块为什么存在】
工会挖矿（``AllianceNewPit`` 家族）开矿时要给矿点指派角色，而"哪些角色
满足矿点的元素 / 等级 / 星级条件"只有**玩家自己的名册**才知道。
``GetAllRolePacket``（5011，空包）一次性拉回全部角色 —— 只读、零消耗。

【★ 角色数据有两个来源，别只知道 5012（2026-10-05 定案）】

=========================  =========  ==========================================
来源                        包号        说明
=========================  =========  ==========================================
``LoginRes.roleInitClass``  1004       **进区登录就下发**的全量角色快照。
                                      ``models/server.py`` 的 ``LoginRes`` 字段 5。
``GetAllRoleRes.roleList``  5012       5011 的响应，**主动刷新**用。
=========================  =========  ==========================================

客户端实测行为（两份抓包互证）：**登录后不发 5011**，打开英雄界面也
**一条网关请求都没有** —— 说明它平时就用 1004 那份。5011 只在需要刷新时才发。

⇒ 所以"角色数据拿不到"时，先怀疑 1004 那条路（``LoginRes`` 是否声明了
``roleInitClass``），而不是急着怀疑 5012。

【字段依据】
- 来源：``GameAssembly.dll``（``GetAllRoleRes.roleList`` → ``List<RoleClass>``），
  字段编号由 ``tools/extract_proto_fields.py`` 从 dump.cs 生成。
- ``RoleClass`` 只带角色**实例**信息（实例 sid / ``roleID`` / 等级 / 星级）。
  ⚠️ ``roleID`` 是 **RoleMainID（130xxxxxx）**，**不是**模板/图鉴 ID
  （RoleInfoID，133xxxxxx）—— 详见 :class:`RoleClass` 的 ⚠️。
  元素 / 职业 / 资质这些"模板侧"属性不在 wire 里，要拿 ``roleID`` 去查
  ``RoleMainData`` / ``RoleInfoData`` 等配置表（``Cherrytale Asset/TextAsset/``）。

【★ 5012 已真机验证（2026-10-16）】
当天以实机会话跑通 5011→5012（154 个角色的完整名册）：所建模字段
（roleSid / roleID / roleLV / roleStar / roleExp / skillsLV）的**编号与形状
和 dump 推定一致**。后续如遇大版本更新，仍建议用 ``describe_message()`` 抽查一次。

【★ 这不是"没用的模块"，别当成死代码删掉（2026-10-05 记）】
工会**团体**挖矿占坑（21043）的 ``roleSidList`` 必须填一个**角色实例 sid**，
而 21040 只告诉你要什么（``needRoleInfoID`` + ``needRoleLv``），
**从不告诉你手里有哪个 sid 可用**。实测 raw/84 的 21043 请求体逐字段解码：
``{1: type=1, 2: pitSid=205294527, 3: roleSidList=[155242087], 4: teamPitIndex=2}``
—— 这个 ``155242087`` 只能来自名册（5012 或 1004）。个人矿（21047）同理。

【★ 2026-10-16 扩展：技能升级与英雄升级（每日任务自动执行）】
- 新增 :class:`UpgradeSkillPacket`（4001）：真机 6 次受控探针实证
  ``skillPlace`` = 技能位**序号**（1 起，与 :class:`SkillClass` 字段顺序一致）、
  ``targetLV`` = **目标绝对等级**，且 ``targetLV`` 线上为**非 packed**；
- 新增 :class:`UpRoleExpPacket`（5003）：喂经验书升英雄等级 —— 它已从
  本模块的**禁发清单移出**（现挂在「每日任务自动执行」的执行链上，
  由任务层控制"只升一级 + 不越级"，见 ``tasks/daily_task_auto.py``）。
"""

from __future__ import annotations

from typing import ClassVar, Final

from pydantic import Field

from models.daily import GetObjClass, ItemClass
from models.game_packet import ProtoMessage

#: 本模块允许发出的报文（类名 → 消息号）；guarded_send 白名单校验
ALLOWED_PACKET_IDS: Final[dict[str, int]] = {
    "GetAllRolePacket": 5011,
    "UpgradeSkillPacket": 4001,
    "UpRoleExpPacket": 5003,
}

#: 明确禁发的报文（会改角色状态/花资源，本项目碰不到也不该碰）
FORBIDDEN_PACKET_IDS: Final[dict[str, int]] = {
    "TransferRolePacket": 5009,
    "OverfulfilRolePacket": 5005,
    "RoleTrainingPacket": 9003,
    "SetRoleSkinPacket": 5031,
}

PACKET_PURPOSE: Final[dict[int, str]] = {
    5011: "拉取角色名册（只读：挖矿按条件挑角色用）",
    5012: "角色名册响应",
    4001: "升级一个技能位（消耗技能点；skillPlace=序号、targetLV=目标等级）",
    4002: "技能升级响应",
    5003: "喂经验书提升英雄等级（消耗经验书 ×N）",
    5004: "英雄升级响应",
}


class GetAllRolePacket(ProtoMessage):
    """拉取全部角色（消息号 5011，空报文，只读）。"""

    proto_class_name = "GetAllRolePacket"


class SkillClass(ProtoMessage):
    """一个角色的五条技能等级（``RoleClass.skillsLV``）。

    字段名与 dump.cs 完全一致（编号 = 声明顺序）。**真机实证（2026-10-16）**：
    ``skillPlace=1/2/3`` 的升级请求分别命中 ``activeLV`` / ``loopLV`` / ``passive1LV`` ——
    所以"技能位序号"就是本类字段的 1 起顺序，见 :meth:`level_of`。
    """

    proto_class_name = "SkillClass"

    #: 主动技等级
    activeLV: int = 0
    #: 循环技等级
    loopLV: int = 0
    #: 被动技 1 等级
    passive1LV: int = 0
    #: 被动技 2 等级
    passive2LV: int = 0
    #: 觉醒技等级
    awakeningLV: int = 0
    #: 自动技能（未建模，保留原始字节）
    autoSkill: bytes = b""

    #: 「技能位序号（1 起）→ 字段名」—— ``skillPlace`` 的取值依据（真机实证）
    LEVEL_FIELDS: ClassVar[tuple[str, ...]] = (
        "activeLV",
        "loopLV",
        "passive1LV",
        "passive2LV",
        "awakeningLV",
    )

    def level_of(self, place: int) -> int:
        """第 ``place`` 个技能位的当前等级（``place`` 从 1 起；越界返回 0）。"""
        if 1 <= place <= len(self.LEVEL_FIELDS):
            return int(getattr(self, self.LEVEL_FIELDS[place - 1]))
        return 0

    def levels(self) -> tuple[int, ...]:
        """五个技能位的当前等级（顺序即 ``skillPlace`` 的 1..5）。"""
        return tuple(int(getattr(self, name)) for name in self.LEVEL_FIELDS)


class RoleClass(ProtoMessage):
    """一个角色实例（``GetAllRoleRes.roleList`` 与 ``LoginRes.roleInitClass`` 的元素）。

    :ivar roleSid: 角色**实例** ID —— 挖矿 ``roleSidList``、装备 ``roleSid``、
        队伍 ``rolesSid`` 里装的都是它。
    :ivar roleID: 角色 **RoleMainID**（**130xxxxxx 段**）——
        与"角色模板/图鉴 ID"不是一回事，见下面的 ⚠️。

    .. warning::
       ★ **2026-10-05 修正：这条注释过去是错的，并因此造成了一个真 bug。**

       ``roleID`` 是 **RoleMainID（130xxxxxx）**，**不是** RoleInfoID（133xxxxxx）。
       反汇编依据：``RoleMainModule.AddServerRole``（Offset ``0x19C2040``）把
       ``RoleClass+0x14``(roleID) 交给 ``isExistInHashSet(roleID, this+0x60)``，
       而 ``RoleMainModule`` 的字段表里 ``0x60 = m_hashSet_RoleMainID``
       （``0x70`` 才是 ``m_hashSet_RoleInfoID``）。

       **所以任何"拿 roleID 跟 needRoleInfoID 比"的代码都是错的**
       （工会团体矿曾经这么写，于是每个槽位都报"名册中没有可用的匹配"）。
       要比必须先换算::

           from models.game_config import load_role_id_map
           role_ids = load_role_id_map()
           role_ids.matches(role.roleID, slot.needRoleInfoID)   # ✅

       换算链：``roleID`` 沿 ``RoleMainData.qualityUpRoleSubID`` 回溯到基础形态，
       再用 ``RoleInfoData.initialRoleMainID`` 反查 RoleInfoID。
       详见 :class:`models.game_config.RoleIdMap`。

       （附带一个容易踩的坑：``RoleMainData.info`` 这一列**在文本配置表里全是
       NULL**，所以不能直接读它拿 RoleInfoID —— 必须走上面那条链。）
    """

    proto_class_name = "RoleClass"

    #: 角色实例 ID（矿点指派 / 槽位填入都用它）
    roleSid: int = 0
    #: 角色 **RoleMainID**（130xxxxxx 段；**不是** RoleInfoID —— 见类 docstring 的 ⚠️）
    roleID: int = 0
    #: 角色等级
    roleLV: int = 0
    #: 角色星级
    roleStar: int = 0
    #: 角色经验
    roleExp: int = 0
    #: 技能等级（2026-10-16 建模；``skillPlace`` 与它的字段顺序一一对应）
    skillsLV: SkillClass = Field(default_factory=SkillClass)
    #: 收藏信息（未建模，保留原始字节）
    favorite: bytes = b""
    #: 专注状态
    focusState: int = 0
    #: 皮肤 ID
    roleSkinID: int = 0


class GetAllRoleRes(ProtoMessage):
    """角色名册响应（消息号 5012）。"""

    proto_class_name = "GetAllRoleRes"

    #: 全部角色
    roleList: list[RoleClass] = Field(default_factory=list)

    def by_role_id(self, role_id: int) -> list[RoleClass]:
        """取某模板 ID 的全部角色实例（挖矿"指定角色"条件用）。"""
        return [role for role in self.roleList if role.roleID == role_id]


# ---------------------------------------------------------------------------
# 技能升级 / 英雄升级（每日任务自动执行，2026-10-16）
# ---------------------------------------------------------------------------
class UpgradeSkillPacket(ProtoMessage):
    """升级一个技能位（消息号 4001）。

    【真机实证（2026-10-16，6 次受控探针，见 notes/daily_task_auto_plan.md §六）】
    - ``skillPlace`` = 技能位**序号**（1 起，与 ``SkillClass.LEVEL_FIELDS`` 同序：
      1=主动 / 2=循环 / 3=被动1 / 4=被动2 / 5=觉醒）；
    - ``targetLV`` = **目标绝对等级**（不是"升几级"，实测 1→2、2→3 均按目标值结算）；
    - ``targetLV`` 在线上是**非 packed**（单元素时就是一个裸 varint）——
      所以声明 ``unpacked_fields``，组包时逐元素独立带标签。
    """

    proto_class_name = "UpgradeSkillPacket"

    #: 目标英雄的角色实例 sid
    roleSid: int = 0
    #: 技能位序号（1 起，见类文档）
    skillPlace: int = 0
    #: 目标等级列表（单个元素即"升到该级"）
    targetLV: list[int] = Field(default_factory=list)

    #: 抓包实证：targetLV 线上为非 packed（逐元素独立 varint）
    unpacked_fields: ClassVar[frozenset[str]] = frozenset({"targetLV"})


class UpgradeSkillRes(ProtoMessage):
    """技能升级响应（消息号 4002）。

    ``errorCode`` 语义（2026-10-16 实证）：``0`` = 成功；
    ``-4`` = 目标技能位的**品质门槛未满足**（英雄品质低于
    ``SkillOpenStateData`` 里该位的门槛）——其余取值未实测。
    """

    proto_class_name = "UpgradeSkillRes"

    #: 业务错误码（0 = 成功）
    errorCode: int = 0
    #: 操作后的持有量（技能点/金币余额；**余额口径**，别当"本次消耗"报出去）
    itemClass: list[ItemClass] = Field(default_factory=list)


class UpRoleExpPacket(ProtoMessage):
    """喂经验书提升英雄等级（消息号 5003）。

    .. warning::
       **有副作用**：会真实消耗经验书。任务层负责算好"只升一级、不越级"的书单
       （每本书都要带背包实例 ID ``itemSid``）；超时后不重试。
    """

    proto_class_name = "UpRoleExpPacket"

    #: 目标英雄的角色实例 sid
    roleSid: int = 0
    #: 要消耗的经验书（``itemSid`` 是背包实例 ID，必须从 2007 全量背包里读）
    itemClass: list[ItemClass] = Field(default_factory=list)


class UpRoleExpRes(ProtoMessage):
    """英雄升级响应（消息号 5004）。"""

    proto_class_name = "UpRoleExpRes"

    #: 业务错误码（0 = 成功）
    errorCode: int = 0
    #: 更新后的角色数据（含新等级——任务层用它复核"是否真的 +1 级"）
    roleClass: RoleClass | None = None
    #: 消耗后的持有量（沿用 GetObjClass 结构；数量＝余额口径）
    costList: list[GetObjClass] = Field(default_factory=list)
