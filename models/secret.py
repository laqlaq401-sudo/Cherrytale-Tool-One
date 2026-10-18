"""契约（秘密区段）报文：11029/11030 拉清单；**抽取复用 11009**。

【日常任务 249000048「窺探契約：抽取《契約》拼圖 1 次」的实现链路（抓包实证 2026-10-16）】
1. 发 ``11029``（空包，只读）→ ``11030`` 得到全部契约区段及其状态；
2. 挑一个 ``sectionState == 0（NewOpen）`` 的区段，用 **11009 SweepSectionPacket**
   发一次抽取：``{sectionID, times=1, useSweepTicket=0}``（``include_defaults=True``，
   0 值字段照发 —— 与素材扫荡同一字节纪律，见 ``tasks/material_stage.py``）；
3. 在 11010 响应里读 ``errorCode``。消耗的是 CP（``200000111``，按步阶 5/10/15/20）。

【为什么 11009 不在本模块重复建模】
11009/11010 的报文与使用纪律已在 ``models/activity_stage.py`` 完整建模，
这里**直接复用**（下方 re-export）——"同一个结构建两份模型"正是字段错位的经典来源。
"""

from __future__ import annotations

from typing import Final

from pydantic import Field

#: 11009/11010：抽取本体，复用活动关扫荡的同型报文（与素材扫荡共用一组字节纪律）。
from models.activity_stage import SweepSectionPacket, SweepSectionRes
from models.game_packet import ProtoMessage
from models.packet_ids import packet_id

#: 本模块允许发出的报文（类名）
ALLOWED_PACKET_NAMES: Final[tuple[str, ...]] = (
    "GetSecretSectionPacket",  # 11029 只读清单
    "SweepSectionPacket",  # 11009 抽取本体（复用活动关扫荡的模型）
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
    11029: "拉取契约区段清单（只读空包）",
    11030: "契约区段清单响应",
}

#: ``StageModule.eSectionStateType``（dump.cs 530178）取值
SECTION_STATE_LOCK: Final[int] = -1
SECTION_STATE_NEW_OPEN: Final[int] = 0
SECTION_STATE_BATTLE_FAIL: Final[int] = 1
SECTION_STATE_FINISH: Final[int] = 2


class GetSecretSectionPacket(ProtoMessage):
    """拉取契约区段清单（消息号 11029，**空报文**，只读）。"""

    proto_class_name = "GetSecretSectionPacket"


class SecretSectionClass(ProtoMessage):
    """一个契约区段的状态（``GetSecretSectionRes.SecretSectionList`` 的元素）。"""

    proto_class_name = "SecretSectionClass"

    #: 区段 ID（抽取时作为 ``SweepSectionPacket.sectionID``）
    sectionID: int = 0
    #: 状态（见 ``SECTION_STATE_*``；``0`` = 新开/可抽取）
    sectionState: int = 0
    #: 限次道具数量（实测恒 0，未用）
    limitItemCount: int = 0
    #: 拼图碎片加成（实测恒 -2，未用）
    puzzleFragBonus: int = 0


class GetSecretSectionRes(ProtoMessage):
    """契约区段清单响应（消息号 11030）。"""

    proto_class_name = "GetSecretSectionRes"

    #: ⚠️ 字段名按 dump.cs 原文保持**首字母大写**的 ``SecretSectionList``，别顺手改成小写
    SecretSectionList: list[SecretSectionClass] = Field(default_factory=list)

    def new_open_sections(self) -> list[SecretSectionClass]:
        """全部"可抽取"（``sectionState == 0``）的区段，按服务端顺序。"""
        return [
            section
            for section in self.SecretSectionList
            if section.sectionState == SECTION_STATE_NEW_OPEN
        ]
