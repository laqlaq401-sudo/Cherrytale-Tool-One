"""竞技场（PVP 天梯）战斗协议数据模型。

【业务说明】
对应 SystemID 22「競技場」，协议模块 ``PvPNetWorkModule``（26001~26022）。
本模块定义天梯挑战对决与结算发奖的 Protobuf 协议结构：
- 26003 ``ChallengePacket``：向候选天梯对手发起挑战（扣减挑战次数）
- 26004 ``ChallengeRes``：进战响应，下发双方阵容（mineAssemble / opponentAssemble）与 ``fightToken``
- 26019 ``ChallengeResultPacket``：提交对决结算结果，携带精确 Token 签名与对决战绩
- 26020 ``ChallengeResultRes``：结算响应，下发最新天梯积分、胜场与排名
- 97025 ``PVPVerifyPacket``：可选的异步战斗过程存证审计上报（与发奖结算解耦）

【签名与加盐特征】
- 签名算法：``MD5(fightToken + targetPlayerID + "0" + "03120908")``
- 抓包证实发奖先行于存证包 0.6 秒，且结算请求不依赖存证复核。
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

_LOGGER: Final[logging.Logger] = logging.getLogger("models.battle_arena")

#: 动态注册字段表
_ARENA_PROTO_FIELDS: Final[dict[str, tuple[str, ...]]] = {
    "PVPResultsClass": ("myRanking", "myIntegral", "victory", "winning_streak"),
    "ChallengePacket": ("actionType", "playerID", "teamID"),
    "ChallengeRes": (
        "errorCode",
        "loseIntegral",
        "winIntegral",
        "items",
        "fightToken",
        "fightGlobalEffectClass",
        "opponentAssemble",
        "mineAssemble",
    ),
    "ChallengeResultPacket": (
        "result",
        "type",
        "playerID",
        "fightingNumber",
        "fightToken",
        "battleResult",
        "token",
    ),
    "ChallengeResultRes": ("errorCode", "myResults"),
    "ReceiveRewardPacket": ("missionID",),
    "ReceiveRewardRes": ("errorCode", "items"),
    "DailyRewardPacket": (),
    "DailyRewardRes": ("dailyReward",),
    "PVPDailyRewardClass": ("misID", "completedTimes", "isGotReward"),
    "PVPVerifyPacket": (
        "battleToken",
        "reportSid",
        "winOrLose",
        "atkTeamPlayId",
        "defTeamPlayId",
        "battleType",
        "message",
        "sectionID",
        "fightRecord",
        "fightData",
    ),
}

#: 协议消息号映射
PACKET_ID_MAP: Final[dict[str, int]] = {
    "ChallengePacket": packet_id("ChallengePacket"),              # 26003
    "ChallengeRes": packet_id("ChallengeRes"),                    # 26004
    "ChallengeResultPacket": packet_id("ChallengeResultPacket"),  # 26019
    "ChallengeResultRes": packet_id("ChallengeResultRes"),        # 26020
    "ReceiveRewardPacket": packet_id("ReceiveRewardPacket"),      # 26017
    "ReceiveRewardRes": packet_id("ReceiveRewardRes"),            # 26018
    "DailyRewardPacket": packet_id("DailyRewardPacket"),          # 26013
    "DailyRewardRes": packet_id("DailyRewardRes"),                # 26014
    "PVPVerifyPacket": packet_id("PVPVerifyPacket"),              # 97025
}

#: 默认 PVP 出战队伍 ID
PVP_DEFAULT_TEAM_ID: Final[int] = 60


class ArenaBattleProtoMessage(ProtoMessage):
    """竞技场报文基类，提供字段映射兜底保障。"""

    @classmethod
    def field_tag_map(cls) -> dict[str, int]:
        if cls.proto_class_name in _ARENA_PROTO_FIELDS:
            return {
                name: index
                for index, name in enumerate(_ARENA_PROTO_FIELDS[cls.proto_class_name], start=1)
            }
        return super().field_tag_map()


class PVPResultsClass(ArenaBattleProtoMessage):
    """竞技场结算后的个人天梯战绩状态（TypeDefIndex 9096）。"""

    proto_class_name = "PVPResultsClass"

    #: 当前个人天梯最新排名
    myRanking: int = 0
    #: 当前最新个人积分（天梯积分）
    myIntegral: int = 0
    #: 本次是否胜利（1=胜, 0=负）
    victory: int = 0
    #: 当前连胜次数
    winning_streak: int = 0


class ChallengePacket(ArenaBattleProtoMessage):
    """向指定玩家发起竞技场对决请求（消息号 26003）。

    .. note::
       请求一旦发出立即扣除 1 次免费或付费挑战次数。
    """

    proto_class_name = "ChallengePacket"

    #: 挑战动作类型（通常填 0）
    actionType: int = 0
    #: 目标对手玩家的 PlayerID
    playerID: int = 0
    #: 出战队伍预设 ID（默认 60）
    teamID: int = PVP_DEFAULT_TEAM_ID


class ChallengeRes(ArenaBattleProtoMessage):
    """发起竞技场挑战响应（消息号 26004）。"""

    proto_class_name = "ChallengeRes"

    #: 错误码（0 = 成功）
    errorCode: int = 0
    #: 战败预计扣除的积分
    loseIntegral: int = 0
    #: 获胜预计获得的积分奖励
    winIntegral: int = 0
    #: 进场附赠奖励或道具（通常为空）
    items: list[GetObjClass] = Field(default_factory=list)
    #: ★ 本次对决会话的随机令牌，必须在结算请求中回传并参与 Token 签名计算
    fightToken: int = 0
    #: 全局被动增益
    fightGlobalEffectClass: bytes = b""
    #: 对手阵容数据快照（原始 protobuf 字节）
    opponentAssemble: bytes = b""
    #: 我方阵容数据快照（原始 protobuf 字节）
    mineAssemble: bytes = b""

    def is_success(self) -> bool:
        """是否成功建立挑战会话。"""
        return self.errorCode == 0


class ChallengeResultPacket(ArenaBattleProtoMessage):
    """提交竞技场对决胜负结果请求（消息号 26019）。"""

    proto_class_name = "ChallengeResultPacket"

    #: 对决胜负状态（0 = 胜利, 1 = 失败）
    result: int = 0
    #: 挑战子类型（常规天梯填 0）
    type: int = 0
    #: 挑战的目标对手 playerID
    playerID: int = 0
    #: 我方出战队伍总战力（官方客户端 0x1804F4EA5: GetPVPTeam().GetAllMember() -> GetRolesPower()）
    fightingNumber: int = 0
    #: 从 26004 响应拿到的战斗令牌
    fightToken: int = 0
    #: 战斗详情数据载荷
    battleResult: BattleResultClass = Field(default_factory=BattleResultClass)
    #: ★ 关键签名：MD5(fightToken + targetPlayerID + "0" + "03120908")
    token: str = ""


class ChallengeResultRes(ArenaBattleProtoMessage):
    """竞技场对决结算发奖响应（消息号 26020）。"""

    proto_class_name = "ChallengeResultRes"

    #: 错误码（0 = 成功）
    errorCode: int = 0
    #: 最新的个人天梯积分与排名战报
    myResults: PVPResultsClass | None = None

    def is_success(self) -> bool:
        """结算是否成功。"""
        return self.errorCode == 0


class PVPVerifyPacket(ArenaBattleProtoMessage):
    """战斗全流程存证审计上报包（消息号 97025）。

    .. note::
       抓包证据显示此报文异步单向投递，用于服务器离线风控留痕，不阻塞发奖。
    """

    proto_class_name = "PVPVerifyPacket"

    #: 关联的战斗令牌
    battleToken: int = 0
    #: 战报 SID
    reportSid: int = 0
    #: 胜负标记（0 胜, 1 负）
    winOrLose: int = 0
    #: 进攻方玩家 ID
    atkTeamPlayId: int = 0
    #: 防守方玩家 ID
    defTeamPlayId: int = 0
    #: 玩法类型编号（如 4 代表竞技场，7 代表失控炼成阵，12 代表试炼）
    battleType: int = 4
    #: 战斗元信息 JSON 文本
    message: str = ""
    #: 关卡或配置编号（非试炼关卡填 0）
    sectionID: int = 0
    #: 战斗行为日志 JSON 文本
    fightRecord: str = ""
    #: 战斗初始数据 JSON 文本
    fightData: str = ""


class ReceiveRewardPacket(ArenaBattleProtoMessage):
    """一键领取竞技场胜场与连胜奖励请求（消息号 26017）。

    .. note::
       传入 missionID=-1 表示一键结算并领取当前全部达标未领取的胜场与连胜奖励。
    """

    proto_class_name = "ReceiveRewardPacket"

    missionID: int = -1


class ReceiveRewardRes(ArenaBattleProtoMessage):
    """领取竞技场奖励响应（消息号 26018）。"""

    proto_class_name = "ReceiveRewardRes"

    errorCode: int = 0
    items: list[GetObjClass] = Field(default_factory=list)

    def is_success(self) -> bool:
        """是否成功领取奖励。"""
        return self.errorCode == 0


class PVPDailyRewardClass(ArenaBattleProtoMessage):
    """竞技场每日胜场与连胜奖励达成项（TypeDefIndex 9094）。"""

    proto_class_name = "PVPDailyRewardClass"

    misID: int = 0
    completedTimes: int = 0
    isGotReward: int = 0


class DailyRewardPacket(ArenaBattleProtoMessage):
    """查询竞技场每日奖励状态请求（消息号 26013，只读空包）。"""

    proto_class_name = "DailyRewardPacket"


class DailyRewardRes(ArenaBattleProtoMessage):
    """查询竞技场每日奖励状态响应（消息号 26014）。"""

    proto_class_name = "DailyRewardRes"

    dailyReward: list[PVPDailyRewardClass] = Field(default_factory=list)

