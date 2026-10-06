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

【⚠️ 5012 尚未实测确认】
本模块的响应从未在抓包中出现过（5012 不在 ``notes/gateway_traffic.md`` 清单里，
``captures/`` 下 19 份 SAZ + 3 份 HAR 也 0 次命中）—— 客户端登录后不拉它，
而现有的工会抓包是"站在界面里"开始录的，所以拍不到那一发。
字段**形状**来自客户端侧（``GameAssembly.dll`` 定义 +
``tools/extract_proto_fields.py`` 按 ``[ProtoMember]`` 声明顺序生成），
**字段编号本身未经响应字节验证**。首次真机返回时应用 ``describe_message()``
对照一次，有出入就按实测改这里与 ``protocol_samples`` 的夹具。
（对比之下，1004 那条路的结构由 ``LoginRes`` 的完整字段表给出，可信度更高。）

【★ 这不是"没用的模块"，别当成死代码删掉（2026-10-05 记）】
工会**团体**挖矿占坑（21043）的 ``roleSidList`` 必须填一个**角色实例 sid**，
而 21040 只告诉你要什么（``needRoleInfoID`` + ``needRoleLv``），
**从不告诉你手里有哪个 sid 可用**。实测 raw/84 的 21043 请求体逐字段解码：
``{1: type=1, 2: pitSid=205294527, 3: roleSidList=[155242087], 4: teamPitIndex=2}``
—— 这个 ``155242087`` 只能来自名册（5012 或 1004）。个人矿（21047）同理。
"""

from __future__ import annotations

from typing import Final

from pydantic import Field

from models.game_packet import ProtoMessage

#: 本模块允许发出的报文（类名 → 消息号）；guarded_send 白名单校验
ALLOWED_PACKET_IDS: Final[dict[str, int]] = {
    "GetAllRolePacket": 5011,
}

#: 明确禁发的报文（会改角色状态/花资源，本项目碰不到也不该碰）
FORBIDDEN_PACKET_IDS: Final[dict[str, int]] = {
    "UpRoleExpPacket": 5003,
    "TransferRolePacket": 5009,
    "OverfulfilRolePacket": 5005,
    "RoleTrainingPacket": 9003,
    "SetRoleSkinPacket": 5031,
}

PACKET_PURPOSE: Final[dict[int, str]] = {
    5011: "拉取角色名册（只读：挖矿按条件挑角色用）",
    5012: "角色名册响应",
}


class GetAllRolePacket(ProtoMessage):
    """拉取全部角色（消息号 5011，空报文，只读）。"""

    proto_class_name = "GetAllRolePacket"


class RoleClass(ProtoMessage):
    """一个角色实例（``GetAllRoleRes.roleList`` 与 ``LoginRes.roleInitClass`` 的元素）。

    .. note::
       ``skillsLV`` / ``favorite`` 两个嵌套结构在当前需求（挖矿挑角色只看
       sid / roleID / 等级 / 星级）里用不到，按项目惯例**保留原始字节**。

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
    #: 技能等级（未建模，保留原始字节）
    skillsLV: bytes = b""
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
