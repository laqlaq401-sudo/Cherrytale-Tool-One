"""失控炼成阵（Yimo）战斗协议数据模型。

【业务说明】
对应 SystemID 51「失控煉成陣」，协议模块 ``YimoBattleModule``（22101~22114）。
本模块定义战斗进场与伤害结果上报的 Protobuf 协议结构：
- 22107 ``YimoBattleRequest``：申请进入炼成阵战斗（消耗挑战次数）
- 22108 ``YimoBattleRequestRes``：进战响应，下发本次战斗的 ``fightToken`` 与 Boss 等级/血量状态
- 22109 ``YimoBattleResult``：上报战斗得分，携带精准 Token 与存证数据
- 22110 ``YimoBattleResultRes``：结算发奖响应，下发积分奖励与排名

【签名与存证解耦特征】
- 签名算法：``MD5(fightToken + score + "03120908")``
- 逆向与实测证实：失控炼成阵支持空存证结算（Proofs 为空列表），
  存证数据完全不参与网关签名的计算。
"""

from __future__ import annotations

import logging
from typing import ClassVar, Final

from pydantic import Field

from models import proto_fields
from models.daily import GetObjClass
from models.game_packet import ProtoMessage
from models.main_stage import BattleResultClass
from models.packet_ids import packet_id

_LOGGER: Final[logging.Logger] = logging.getLogger("models.battle_yimo")

#: 动态注册字段表（避免修改已固化的 proto_fields.py）
_YIMO_PROTO_FIELDS: Final[dict[str, tuple[str, ...]]] = {
    "YimoBattleRequest": ("part", "bossLv"),
    "YimoBattleRequestRes": (
        "errorCode",
        "fightToken",
        "hpRate",
        "topScore",
        "fightGlobalEffectClass",
        "mineAssemble",
    ),
    "YimoBattleResult": ("fightToken", "dmgHpRate", "score", "battleResult", "token"),
    "YimoBattleResultRes": ("errorCode", "rewardList", "rank", "totalValue", "remainCount"),
}

#: 协议消息号映射
PACKET_ID_MAP: Final[dict[str, int]] = {
    "YimoBattleRequest": packet_id("YimoBattleRequest"),        # 22107
    "YimoBattleRequestRes": packet_id("YimoBattleRequestRes"),  # 22108
    "YimoBattleResult": packet_id("YimoBattleResult"),          # 22109
    "YimoBattleResultRes": packet_id("YimoBattleResultRes"),    # 22110
}


class YimoBattleProtoMessage(ProtoMessage):
    """失控炼成阵报文基类，提供字段映射兜底保障。"""

    @classmethod
    def field_tag_map(cls) -> dict[str, int]:
        if cls.proto_class_name in _YIMO_PROTO_FIELDS:
            return {
                name: index
                for index, name in enumerate(_YIMO_PROTO_FIELDS[cls.proto_class_name], start=1)
            }
        return super().field_tag_map()


class YimoBattleRequest(YimoBattleProtoMessage):
    """进入失控炼成阵战斗请求（消息号 22107）。

    .. note::
       调用此接口会扣减每日挑战次数（``remainCount``）。
    """

    proto_class_name = "YimoBattleRequest"

    #: 挑战部位 ID（通常填 1 或指定部位编号，如抓包中 661000003）
    part: int = 1
    #: 当前挑战的 Boss 等级
    bossLv: int = 1


class YimoBattleRequestRes(YimoBattleProtoMessage):
    """进入失控炼成阵战斗响应（消息号 22108）。"""

    proto_class_name = "YimoBattleRequestRes"

    #: 错误码（0 = 成功）
    errorCode: int = 0
    #: ★ 本次战斗会话的随机令牌，必须在结算请求中回传并参与 Token 签名计算
    fightToken: int = 0
    #: Boss 当前血量百分比（基准 10000 = 100.00%）
    hpRate: int = 10000
    #: 本日最高得分
    topScore: int = 0
    #: 全局被动增益数据（原始 protobuf 字节）
    fightGlobalEffectClass: bytes = b""
    #: 我方出战队伍编队快照（原始 protobuf 字节）
    mineAssemble: bytes = b""

    def is_success(self) -> bool:
        """是否成功进入战斗。"""
        return self.errorCode == 0


class YimoBattleResult(YimoBattleProtoMessage):
    """提交失控炼成阵战斗成绩结算请求（消息号 22109）。"""

    proto_class_name = "YimoBattleResult"

    #: 从 22108 响应中拿到的战斗令牌
    fightToken: int = 0
    #: 造成的血量扣减万分比（例如 10000 代表造成 100% 击杀/全额伤害）
    dmgHpRate: int = 10000
    #: 本次挑战斩获的伤害积分
    score: int = 0
    #: 详细战斗结果载荷（支持空存证）
    battleResult: BattleResultClass = Field(default_factory=BattleResultClass)
    #: ★ 关键签名：MD5(fightToken + score + "03120908")
    token: str = ""


class YimoBattleResultRes(YimoBattleProtoMessage):
    """失控炼成阵结算响应（消息号 22110）。"""

    proto_class_name = "YimoBattleResultRes"

    #: 错误码（0 = 成功）
    errorCode: int = 0
    #: 结算获得的奖励道具列表
    rewardList: list[GetObjClass] = Field(default_factory=list)
    #: 当前最新全服排名
    rank: int = 0
    #: 累计总伤害值/积分
    totalValue: int = 0
    #: 结算后剩余的挑战次数
    remainCount: int = 0

    def is_success(self) -> bool:
        """业务是否成功发奖。"""
        return self.errorCode == 0
