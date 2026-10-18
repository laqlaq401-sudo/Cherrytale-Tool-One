"""装备报文：强化（3003/3004）与全量清单（3005/3006）。

【为什么装备清单也要单独拉（3005/3006）】
「每日任务自动执行」要挑"强化等级最低的装备"强化一次。登录快照
（``LoginRes.equipInitClass``）虽然带着装备数据，但**会话文件里不存装备**
（``models/session_state.py`` 只快照角色），所以由任务主动拉一份实时的 ——
3005 是空报文只读查询，零消耗。

【抓包实证（2026-10-16）】
- 3003 请求：``{equipSid, count}``（8 字节；``count=1`` 表示强化一次）；
- 3004 响应：``{errorCode, itemClass[]}``，其中数量是**操作后的持有量**
  （炼成阵魔尘 ``200200049`` 与金币的余额），沿用 Q-021 的"余额口径"。
"""

from __future__ import annotations

from typing import Final

from pydantic import Field

from models.daily import ItemClass
from models.game_packet import ProtoMessage
from models.packet_ids import packet_id
from models.server import EquipClass

#: 本模块允许发出的报文（类名）。3003 有真实消耗（魔尘 + 金币），count 由任务层固定为 1。
ALLOWED_PACKET_NAMES: Final[tuple[str, ...]] = (
    "StrengthenEquipPacket",
    "GetAllEquipPacket",
)

#: 明确禁发：其它会改动装备的报文（不进白名单，也不该被组装）。
FORBIDDEN_PACKET_NAMES: Final[tuple[str, ...]] = (
    "ChangeEquipPacket",
    "RankUpEquipPacket",
    "InheritEquipPacket",
)

#: ``{类名: 消息号}``（供 ``tasks/packet_guard.guarded_send`` 使用）
ALLOWED_PACKET_IDS: Final[dict[str, int]] = {
    name: packet_id(name) for name in ALLOWED_PACKET_NAMES
}

#: ``{类名: 消息号}``
FORBIDDEN_PACKET_IDS: Final[dict[str, int]] = {
    name: packet_id(name) for name in FORBIDDEN_PACKET_NAMES
}

#: ``{消息号: 说明}``（DEBUG 日志与演练输出用）
PACKET_PURPOSE: Final[dict[int, str]] = {
    3003: "强化一件装备（**消耗炼成阵魔尘 + 金币**）",
    3004: "强化装备响应",
    3005: "拉取全量装备清单（只读空包）",
    3006: "全量装备清单响应",
}


class StrengthenEquipPacket(ProtoMessage):
    """强化一件装备（消息号 3003）。

    .. warning::
       **有副作用**：会真实消耗炼成阵魔尘与金币。超时后不重试
       （沿用 ``models/daily.py`` 的副作用纪律）。
    """

    proto_class_name = "StrengthenEquipPacket"

    #: 装备**实例** ID（``EquipClass.equipSid``）
    equipSid: int = 0
    #: 强化次数（本任务固定 1）
    count: int = 0


class StrengthenEquipRes(ProtoMessage):
    """强化装备响应（消息号 3004）。"""

    proto_class_name = "StrengthenEquipRes"

    #: 业务错误码（0 = 成功）
    errorCode: int = 0
    #: 消耗后的持有量（魔尘 / 金币；**数量＝余额口径**，不许当"本次消耗"报出去）
    itemClass: list[ItemClass] = Field(default_factory=list)


class GetAllEquipPacket(ProtoMessage):
    """拉取全量装备清单（消息号 3005，**空报文**，只读）。"""

    proto_class_name = "GetAllEquipPacket"


class GetAllEquipRes(ProtoMessage):
    """全量装备清单响应（消息号 3006）。"""

    proto_class_name = "GetAllEquipRes"

    #: 全部装备（含各自的强化等级 ``equipLV``）
    equipList: list[EquipClass] = Field(default_factory=list)
