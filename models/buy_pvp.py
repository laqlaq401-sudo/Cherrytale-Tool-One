"""PVP 竞技场/天命对决/荣耀之巅次数购买报文与阶梯计费模型。

【协议消息号与逆向依据】
- 竞技场（PvP Arena）：
  - 26015 BuyChallengePacket（空报文）→ 26016 BuyChallengeRes
- 天命对决（WuDou）：
  - 26047 WuDouBuyChallenge（空报文）→ 26048 WuDouBuyChallengeRes
- 荣耀之巅（TopPvp）：
  - 39005 TopPvpBuyChallengeCountPacket（空报文）→ 39006 TopPvpBuyChallengeCountRes

【计费规则（StepTollSystemData 表）】
- storeID: 570000001 (竞技场), 570000009 (天命对决), 570000014 (荣耀之巅)
- 计费阶梯（共 40 档）：
  第 1~2 次 10 钻；第 3 次 20 钻；第 4 次 30 钻；第 5 次 40 钻；
  第 6 次 55 钻；第 7 次 70 钻；第 8 次 85 钻；第 9 次 100 钻；第 10 次 120 钻 ...

【VIP 上限规则（VipData 表）】
- 竞技场：列 pvpChallengeTimes（VIP0: 10, VIP5: 15, VIP8: 20, VIP11: 25, VIP14: 30）
- 天命对决：列 wuDouBuyTimes（VIP0: 10, VIP5: 15, VIP8: 20, VIP11: 25）
- 荣耀之巅：列 pvp3v3ChallengeTimes（VIP0: 5, VIP5: 10, VIP7: 15, VIP9: 20, VIP12: 25, VIP15: 30）
"""

from __future__ import annotations

import logging
from typing import Final, Literal

from pydantic import Field

from models.daily import GetObjClass, SUCCESS_ERROR_CODE
from models.game_packet import ProtoMessage

_LOGGER: Final[logging.Logger] = logging.getLogger("models.buy_pvp")

#: 阶梯价格映射（1 ~ 40 次，源自 StepTollSystemData 表）
PVP_STEP_COSTS: Final[tuple[int, ...]] = (
    10, 10, 20, 30, 40, 55, 70, 85, 100, 120,
    140, 160, 200, 240, 280, 320, 360, 450, 540, 630,
    720, 810, 900, 1000, 1100, 1200, 1300, 1400, 1500, 1600,
    1700, 1800, 1900, 2000, 2100, 2200, 2300, 2400, 2500, 2600,
)

#: 各系统 VIP0~VIP15 每日最大可购买上限（源自 VipData 表）
VIP_ARENA_LIMITS: Final[tuple[int, ...]] = (
    10, 10, 10, 10, 10, 15, 15, 15, 20, 20, 20, 25, 25, 25, 30, 30
)
VIP_WUDOU_LIMITS: Final[tuple[int, ...]] = (
    10, 10, 10, 10, 10, 15, 15, 15, 20, 20, 20, 25, 25, 25, 25, 25
)
VIP_TOP_PVP_LIMITS: Final[tuple[int, ...]] = (
    5, 5, 5, 5, 5, 10, 10, 15, 15, 20, 20, 20, 25, 25, 25, 30
)

SystemType = Literal["arena", "wudou", "top_pvp"]


def calculate_pvp_cost(current_count: int, times_to_buy: int) -> int:
    """计算从已购 current_count 次起，再购买 times_to_buy 次所需的总钻石数。

    :param current_count: 今日已购买次数（从 0 开始）。
    :param times_to_buy: 本次计划购买次数。
    :return: 所需钻石总额。
    """
    if times_to_buy <= 0:
        return 0
    total = 0
    for nth in range(current_count + 1, current_count + times_to_buy + 1):
        if nth <= len(PVP_STEP_COSTS):
            total += PVP_STEP_COSTS[nth - 1]
        else:
            # 超过第 40 次时按最后一档 2600 封顶
            total += PVP_STEP_COSTS[-1]
    return total


def get_vip_max_buy_times(vip_lv: int, system: SystemType) -> int:
    """根据玩家 VIP 等级获取某系统的每日最大购买次数上限。

    :param vip_lv: 玩家 VIP 等级（0~15）。
    :param system: 玩法系统类型（"arena" / "wudou" / "top_pvp"）。
    :return: 每日允许购买的最大次数上限。
    """
    safe_vip = max(0, min(int(vip_lv), 15))
    if system == "arena":
        return VIP_ARENA_LIMITS[safe_vip]
    if system == "wudou":
        return VIP_WUDOU_LIMITS[safe_vip]
    if system == "top_pvp":
        return VIP_TOP_PVP_LIMITS[safe_vip]
    return 0


# ---------------------------------------------------------------------------
# 竞技场（Arena）购买协议：26015 -> 26016
# ---------------------------------------------------------------------------
class BuyChallengePacket(ProtoMessage):
    """竞技场购买挑战次数请求（消息号 26015，空包）。"""

    proto_class_name = "BuyChallengePacket"

    @classmethod
    def field_tag_map(cls) -> dict[str, int]:
        return {}


class BuyChallengeRes(ProtoMessage):
    """竞技场购买挑战次数响应（消息号 26016）。"""

    proto_class_name = "BuyChallengeRes"

    errorCode: int = 0
    gotList: list[GetObjClass] = Field(default_factory=list)
    buyedCount: int = 0
    costList: list[GetObjClass] = Field(default_factory=list)

    @classmethod
    def field_tag_map(cls) -> dict[str, int]:
        return {"errorCode": 1, "gotList": 2, "buyedCount": 3, "costList": 4}

    @property
    def is_success(self) -> bool:
        return self.errorCode == SUCCESS_ERROR_CODE


# ---------------------------------------------------------------------------
# 天命对决（WuDou）购买协议：26047 -> 26048
# ---------------------------------------------------------------------------
class WuDouBuyChallenge(ProtoMessage):
    """天命对决购买挑战次数请求（消息号 26047，空包）。"""

    proto_class_name = "WuDouBuyChallenge"

    @classmethod
    def field_tag_map(cls) -> dict[str, int]:
        return {}


class WuDouBuyChallengeRes(ProtoMessage):
    """天命对决购买挑战次数响应（消息号 26048）。"""

    proto_class_name = "WuDouBuyChallengeRes"

    errorCode: int = 0
    gotList: list[GetObjClass] = Field(default_factory=list)
    buyedCount: int = 0
    costList: list[GetObjClass] = Field(default_factory=list)

    @classmethod
    def field_tag_map(cls) -> dict[str, int]:
        return {"errorCode": 1, "gotList": 2, "buyedCount": 3, "costList": 4}

    @property
    def is_success(self) -> bool:
        return self.errorCode == SUCCESS_ERROR_CODE


# ---------------------------------------------------------------------------
# 荣耀之巅（TopPvp）购买协议：39005 -> 39006
# ---------------------------------------------------------------------------
class TopPvpBuyChallengeCountPacket(ProtoMessage):
    """荣耀之巅购买挑战次数请求（消息号 39005，空包）。"""

    proto_class_name = "TopPvpBuyChallengeCountPacket"

    @classmethod
    def field_tag_map(cls) -> dict[str, int]:
        return {}


class TopPvpBuyChallengeCountRes(ProtoMessage):
    """荣耀之巅购买挑战次数响应（消息号 39006）。"""

    proto_class_name = "TopPvpBuyChallengeCountRes"

    errorCode: int = 0
    gotList: list[GetObjClass] = Field(default_factory=list)
    buyedCount: int = 0
    costList: list[GetObjClass] = Field(default_factory=list)

    @classmethod
    def field_tag_map(cls) -> dict[str, int]:
        return {"errorCode": 1, "gotList": 2, "buyedCount": 3, "costList": 4}

    @property
    def is_success(self) -> bool:
        return self.errorCode == SUCCESS_ERROR_CODE
