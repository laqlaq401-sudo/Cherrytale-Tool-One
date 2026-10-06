"""竞技场（PvP）报文：**只读**部分（消息号 26001 → 26002）。

【这个系统是什么】
``SystemIDData`` systemID=22「競技場」→ UI 类 ``PvP_MainUI``；协议模块 ``PvPNetWorkModule``
（26001~26022）。帮助文案实测（``LanguageSettings_zhcn`` 第 10298 行）：

    1.競技場7天為一賽季，每周一05:00進行結算…
    3.每日可免費挑戰5次，次數不足可進行購買…
    11.競技場獲得的『競技證印』可於競技商店購買商品

【为什么本项目在这里**只做只读**】
竞技场的战斗是**客户端本地结算后上报**：

- ``26004 ChallengeRes`` 会把**双方阵容**（``opponentAssemble`` / ``mineAssemble``）发下来；
- ``26019 ChallengeResultPacket`` 要客户端**上报结果**：7.2KB 的 ``battleResult``
  （内含 13.9 万字节逐施法存证 ``battleServerVerify``）+ 32 位 hex 的 ``token``；
- 握手 ``jsonInfo.PVPVerify=1``，服务端会据此复核。

没有复刻战斗引擎就过不了服务端校验（见 ``notes/glory_summit_capture.md`` 第二、四节与
``notes/battle_engine_project.md``），所以这里只读次数与状态，**绝不进战**。

.. warning::
   本模块**刻意不建模**以下报文 —— 让"消耗次数 / 花钻石 / 需要战斗引擎"在类型层面就不存在：

   - ``26003 ChallengePacket``：进战（**一发出就扣次数**）
   - ``26019 ChallengeResultPacket``：结算上报（需要战斗引擎）
   - ``26015 BuyChallengePacket``：**花钻石买挑战次数**
   - ``26013`` / ``26017``：每日奖励 / 领奖（本阶段不在范围内）
"""

from __future__ import annotations

from typing import Final

from pydantic import Field

from models.game_packet import ProtoMessage


class PVPHomePacket(ProtoMessage):
    """竞技场主页请求（消息号 26001）。

    .. note::
       空报文（dump.cs 里没有 ``ProtoMember`` 字段）→ 编码结果 ``b""``。
       **只读，不消耗任何次数。**
    """

    proto_class_name = "PVPHomePacket"


class PVPHomeRes(ProtoMessage):
    """竞技场主页响应（消息号 26002）。

    .. note::
       本响应**没有** ``errorCode`` 字段（字段表实测如此）—— 也就是说"业务失败"
       在这里不用错误码表达；收到正常 protobuf 即视为成功（异常由 ``99004`` 异常包表达，
       见 ``client/game_client.py`` 的异常包检查）。
    """

    proto_class_name = "PVPHomeRes"

    #: 可挑战的对手列表（``PVPListClass`` 未建模 → 保留原始字节，只用来计数）
    pvpList: list[bytes] = Field(default_factory=list)
    #: 我的战绩（``PVPResultsClass`` 未建模 → 原始字节）
    myResults: bytes = b""
    #: ⚠️ 语义待确认（字段名像是"我的战力"，但 ``PVPNetWorkModule.PvP_HomeInfo`` 里
    #: 还同时存在 ``PvP_FightingPower`` / ``PvP_DefendPower``）。这里只原样展示。
    myFighting: int = 0
    #: ★ **剩余可挑战次数**（帮助文案："每日可免費挑戰5次，次數不足可進行購買"）
    challengeCount: int = 0
    #: 已购买次数（与"花钻石买次数"对应 —— 本项目只读它，绝不购买）
    buyedCount: int = 0
    #: 已刷新次数
    reflashCount: int = 0
    #: 当前出战队伍（``TeamClass`` 未建模 → 原始字节）
    team: bytes = b""
    #: 所属公会名
    allianceName: str = ""
    #: 公会徽章（协议里是 ``string``，不是消息）
    allianceIcon: str = ""

    def opponent_count(self) -> int:
        """可挑战对手的数量（只计数，不解析其内容）。"""
        return len(self.pvpList)

    def summary(self) -> str:
        """一行摘要（日志用）。"""
        return (
            f"可挑战次数={self.challengeCount}，已购买={self.buyedCount}，"
            f"已刷新={self.reflashCount}，对手 {self.opponent_count()} 个，"
            f"战力字段={self.myFighting}"
            + (f"，公会={self.allianceName}" if self.allianceName else "")
        )


# ---------------------------------------------------------------------------
# 出站消息号白名单 / 禁发清单
# ---------------------------------------------------------------------------
#: 本板块**允许**发出的消息号：只有一个只读包。
ALLOWED_PACKET_IDS: Final[dict[str, int]] = {
    "PVPHomePacket": 26001,
}

#: 明确禁发（写进测试做回归，防止将来有人"顺手"把进战/买次数加上）。
FORBIDDEN_PACKET_IDS: Final[dict[str, int]] = {
    "ChallengePacket": 26003,           # 进战：一发出就扣次数
    "ChallengeResultPacket": 26019,     # 结算上报：需要战斗引擎 + 存证
    "BuyChallengePacket": 26015,        # **花钻石买挑战次数** ← 红线
    "ReceiveRewardPacket": 26017,       # 领奖（不在本阶段范围）
    "DailyRewardPacket": 26013,         # 每日奖励（不在本阶段范围）
    "RefreshPacket": 26011,             # 刷新对手（会改动服务端的候选列表）
    "PVPVerifyPacket": 97025,           # 战报校验上报（战斗侧）
}

#: 消息号 → 用途说明（日志与演练输出用）。
PACKET_PURPOSE: Final[dict[int, str]] = {
    26001: "竞技场主页（只读：可挑战次数、已购次数、对手列表）",
}
