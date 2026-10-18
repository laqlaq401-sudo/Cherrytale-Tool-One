"""英雄赠礼报文（消息号 5015/5016）。

【抓包实证（2026-10-16）——0 值字段必须显式发送】
真实客户端发 5015 时把 **0 值字段也照发**：``itemId=0``、``useTime=0``
都在字节里（子包 29 字节）；真正的礼物放在 ``itemClass`` 列表里
（每项 ``{itemSid:-1, itemID:礼物ID, itemAmount:1}``，``itemSid`` 实测为 ``-1``）。
⇒ 组包必须 ``include_defaults=True``，否则字节形状与服务端预期不符
（同类教训见 ``tasks/material_stage.py`` 里 11009 少发 0 值字段的事故记录）。
"""

from __future__ import annotations

from typing import Final

from pydantic import Field

from models.daily import GetObjClass, ItemClass
from models.game_packet import ProtoMessage
from models.packet_ids import packet_id

#: 本模块允许发出的报文（类名）
ALLOWED_PACKET_NAMES: Final[tuple[str, ...]] = ("UsePresentPacket",)

#: 明确禁发（本模块暂无——礼物以外的使用/合成/出售路径都不在此处组装）
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
    5015: "给一位英雄赠送礼物（消耗礼物道具 ×1）",
    5016: "赠礼响应",
}

#: 可赠送的礼物道具（``ItemData.useType == 12`` 的两段：200600001~005 / 091~093）。
#: 顺序＝"低阶优先"的偏好顺序（礼物是否通用由服务端判定，被拒时任务层会换下一个）。
GIFT_ITEM_IDS: Final[tuple[int, ...]] = (
    200600001,
    200600002,
    200600003,
    200600004,
    200600005,
    200600091,
    200600092,
    200600093,
)


class UsePresentPacket(ProtoMessage):
    """英雄赠礼（消息号 5015）。

    .. warning::
       **有副作用**：会真实消耗一个礼物道具。超时后不重试。
    """

    proto_class_name = "UsePresentPacket"

    #: 目标英雄的角色**实例** sid
    roleSid: int = 0
    #: 道具 ID —— 抓包实测客户端固定发 ``0``（真正送的礼物在 :attr:`itemClass`）
    itemId: int = 0
    #: 使用次数 —— 抓包实测固定发 ``0``
    useTime: int = 0
    #: 要消耗的礼物列表（``itemSid=-1`` 表示"按物品 ID 扣"）
    itemClass: list[ItemClass] = Field(default_factory=list)


class UsePresentRes(ProtoMessage):
    """赠礼响应（消息号 5016）。"""

    proto_class_name = "UsePresentRes"

    #: 业务错误码（客户端枚举：0 成功 / -1 不足 / -2 满级 / -3 数据错误）
    errorCode: int = 0
    #: 获得的东西（含更新后的角色数据；未细建模的部分按项目惯例保留字节）
    getList: list[GetObjClass] = Field(default_factory=list)
    #: 增加的好感度（抓包实测 +10）
    addExp: int = 0
