"""好友报文：一键领取 / 一键全送（10009/10010）与好友列表（10001/10002）。

【一键全送 = actionType 4 / 一键领取 = actionType 3】
抓包实证（2026-10-16，``captures/好友礼物 一键赠送 一键领取.saz``）：
真实客户端"一键全送"发 ``{actionType=4, playerID=-1}``、"一键领取"发
``{actionType=3, playerID=-1}``，响应都是 10010。
两个动作落在**同一个报文**上，但分属两个任务：

- **全送**由 ``tasks/daily_task_auto.py`` 使用（完成日常任务 249000051
  「好友贈禮 3 次」）；
- **领取**由 ``tasks/friend_gift_claim.py`` 使用（2026-10-18 新增的独立自动任务）。

两者的礼物都由**服务端**决定（协议里没有道具参数）；领取会把当时所有待领的
赠礼一次结清（``raw/13`` 实测 ``friendList`` 25 人、``itemList`` 1 组）。

【好友列表包】
10001 请求 ``{actionType=0}``（0 值也显式发，抓包子包 2 字节）；
10002 响应含 ``friendClass``（与 ``models/bbq.py`` 的 ``FriendClass`` 同型，
直接复用 —— "同一个结构建两份模型"正是字段错位的经典来源）、邀请码与屏蔽列表。
"""

from __future__ import annotations

from typing import Final

from pydantic import Field

from models.bbq import FriendClass
from models.daily import GetObjClass
from models.game_packet import ProtoMessage
from models.packet_ids import packet_id

#: 本模块允许发出的报文（类名）
ALLOWED_PACKET_NAMES: Final[tuple[str, ...]] = (
    "FriendOperationPacket",
    "GetFriendListPacket",
)

#: 明确禁发（暂无）
FORBIDDEN_PACKET_NAMES: Final[tuple[str, ...]] = ()

#: ``{类名: 消息号}``
ALLOWED_PACKET_IDS: Final[dict[str, int]] = {
    name: packet_id(name) for name in ALLOWED_PACKET_NAMES
}

#: ``{类名: 消息号}``
FORBIDDEN_PACKET_IDS: Final[dict[str, int]] = {
    name: packet_id(name) for name in FORBIDDEN_PACKET_NAMES
}

#: ``{消息号: 说明}``
PACKET_PURPOSE: Final[dict[int, str]] = {
    10001: "拉取好友列表（只读）",
    10002: "好友列表响应",
    10009: "好友操作（3 = 一键领取 / 4 = 一键全送）",
    10010: "好友操作响应",
}

#: ``FriendNetWorkModule.eFuntionType`` 取值（dump.cs 542833）
OPERATION_GAIN_GIFT: Final[int] = 1  # 领取单个好友的赠送
OPERATION_SEND_GIFT: Final[int] = 2  # 给单个好友送礼
OPERATION_GAIN_ALL_GIFT: Final[int] = 3  # 一键领取
OPERATION_SEND_ALL_GIFT: Final[int] = 4  # 一键全送（本任务用）

#: "一键"操作把 ``playerID`` 填 ``-1``（抓包实测）。
ALL_GIFT_TARGET: Final[int] = -1


class FriendOperationPacket(ProtoMessage):
    """好友操作（消息号 10009）。

    .. warning::
       一键全送"没有成本"（不动钻石），但它**有副作用**（真的会给好友发礼物）
       ——超时后不重试。
    """

    proto_class_name = "FriendOperationPacket"

    #: 操作类型（见 ``OPERATION_*``）
    actionType: int = 0
    #: 目标好友 ID（一键操作为 :data:`ALL_GIFT_TARGET`）
    playerID: int = 0


class FriendOperationRes(ProtoMessage):
    """好友操作响应（消息号 10010）。"""

    proto_class_name = "FriendOperationRes"

    #: 业务错误码（0 = 成功）
    errorCode: int = 0
    #: 被处理的好友 ID 列表（一键领取实测有值；一键全送实测为空）
    friendList: list[int] = Field(default_factory=list)
    #: 道具明细（数量为持有量口径）
    itemList: list[GetObjClass] = Field(default_factory=list)


class GetFriendListPacket(ProtoMessage):
    """拉取好友列表（消息号 10001）。

    .. note::
       抓包实测请求体是 ``{actionType=0}``（2 字节，0 值照发）
       ⇒ 组包需 ``include_defaults=True``。
    """

    proto_class_name = "GetFriendListPacket"

    #: 拉取类型（抓包实测发 0）
    actionType: int = 0


class GetFriendListRes(ProtoMessage):
    """好友列表响应（消息号 10002）。"""

    proto_class_name = "GetFriendListRes"

    #: 好友列表（元素类型与宴席好友列表共用 ``FriendClass``）
    friendClass: list[FriendClass] = Field(default_factory=list)
    #: 邀请码
    inviteCode: str = ""
    #: 屏蔽码列表
    blockCode: list[str] = Field(default_factory=list)

    def sendable_count(self) -> int:
        """今天还能收礼的好友数（``send_state == 0``，dump 常量口径）。

        .. note::
           ``send_state`` 的语义来自客户端枚举（``CanSend=0 / HaveSent=1``），
           仅用于**报告**展示；发包不依赖它 —— 一键全送由服务端自己筛人。
        """
        return sum(1 for friend in self.friendClass if friend.send_state == 0)
