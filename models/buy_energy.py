"""钻石购买体力报文（消息号 24011/24012 VIP详情 与 24005/24006 购买体力）。

【接口与底层逆向依据】
- 来源：``GameAssembly.dll`` (VipNetWorkModule.RequestBuyEnergyPacket / RequestVIPDetailPacket)
- 查询包：``VIPDetailPacket`` (24011, 空包) → ``VIPDetailRes`` (24012)
  - ``buyEnergyCount`` (Tag 1, int32)：今日已购买体力次数
  - ``pointGoldCount`` (Tag 2, int32)：今日点金次数
  - ``buySkillCount`` (Tag 3, int32)：今日买技能点次数
- 购买包：``BuyEnergyPacket`` (24005)
  - ``buyTimes`` (Tag 1, int32)：本次要购买的次数
  → ``BuyEnergyRes`` (24006)
  - ``errorCode`` (Tag 1, int32)：0 成功；非 0 失败（如 -1 次数耗尽，-5 钻石不足）
  - ``addList`` (Tag 2, List<GetObjClass>)：获得道具列表（含体力 ENERGY_ID = 200000002）
  - ``costList`` (Tag 3, List<GetObjClass>)：扣除道具列表（含钻石 DIAMOND_ID = 200000001）

【费用规则（StepTollSystemData / storeID=570000004）】
- 第 1 ~ 5 次：每次 100 钻石
- 第 6 ~ 10 次：每次 200 钻石
- 第 11 ~ 15 次：每次 300 钻石
- 每次购买产出：120 点体力（OtherSettingsData 行 81: StaminaBuyAmount=120）
"""

from __future__ import annotations

from typing import Final

from pydantic import Field

from models.const_ids import CONSTS
from models.daily import GetObjClass, SUCCESS_ERROR_CODE
from models.game_packet import ProtoMessage

#: 体力产出量（每次固定 120 点）
ENERGY_PER_PURCHASE: Final[int] = 120

#: 本任务允许发出的报文（类名 → 消息号）；guarded_send 白名单校验
ALLOWED_PACKET_IDS: Final[dict[str, int]] = {
    "VIPDetailPacket": 24011,
    "BuyEnergyPacket": 24005,
}

#: 明确禁发其它高危报文（防越界发包）
FORBIDDEN_PACKET_IDS: Final[dict[str, int]] = {
    "PointGoldPacket": 24007,
    "BuySkillPacket": 24009,
    "BuyVipGiftPacket": 24001,
}

PACKET_PURPOSE: Final[dict[int, str]] = {
    24011: "拉取VIP详情（只读：查询今日已购买体力次数）",
    24012: "VIP详情响应",
    24005: "用钻石购买体力请求",
    24006: "用钻石购买体力响应",
}


def calculate_energy_cost(current_count: int, times_to_buy: int) -> int:
    """计算从已购 ``current_count`` 次起，再购买 ``times_to_buy`` 次所需的总钻石数。

    阶梯费用规则（StepTollSystemData 表 storeID=570000004）：
    - 1 ~ 5 次：100 钻 / 次
    - 6 ~ 10 次：200 钻 / 次
    - 11 ~ 15 次及以上：300 钻 / 次
    """
    total = 0
    for nth in range(current_count + 1, current_count + times_to_buy + 1):
        if nth <= 5:
            total += 100
        elif nth <= 10:
            total += 200
        else:
            total += 300
    return total


class VIPDetailPacket(ProtoMessage):
    """请求 VIP 详情（空报文，只读当前购买体力/点金等计数）。"""

    proto_class_name = "VIPDetailPacket"


class VIPDetailRes(ProtoMessage):
    """VIP 详情响应。"""

    proto_class_name = "VIPDetailRes"

    #: 今日已购买体力次数
    buyEnergyCount: int = 0
    #: 今日点金次数
    pointGoldCount: int = 0
    #: 今日买技能点次数
    buySkillCount: int = 0


class BuyEnergyPacket(ProtoMessage):
    """用钻石购买体力请求。"""

    proto_class_name = "BuyEnergyPacket"

    #: 本次要购买的体力次数（如 1 次）
    buyTimes: int = 0


class BuyEnergyRes(ProtoMessage):
    """用钻石购买体力响应。"""

    proto_class_name = "BuyEnergyRes"

    #: 0 成功；非 0 失败（-1 次数超限，-5 钻石不足等）
    errorCode: int = 0
    #: 获得道具列表（一般包含体力）
    addList: list[GetObjClass] = Field(default_factory=list)
    #: 消耗道具列表（一般包含钻石）
    costList: list[GetObjClass] = Field(default_factory=list)

    def is_success(self) -> bool:
        """是否成功。"""
        return int(self.errorCode) == SUCCESS_ERROR_CODE

    def energy_gained(self) -> int:
        """``addList`` 里体力的 ``itemAmount`` 之和。

        .. warning::
           **数量口径尚未实证**。6006 / 11010 / 37020 的同类字段都已被证实是
           "操作后的持有量"（Q-021），24006 很可能相同 —— 所以本方法返回的
           可能**不是本次增量**。

           因此：**禁止**把这个值当"获得"写进用户可见文案。
           要展示，先按 :class:`models.reward.QuantityScope` 明确口径；
           拿不准就用 ``UNKNOWN``（只报件数）。
        """
        energy_id = int(CONSTS["ENERGY_ID"])
        total = 0
        for obj in self.addList:
            for item in getattr(obj, "itemList", []):
                if int(item.itemID) == energy_id:
                    total += int(item.itemAmount)
        return total

    def diamond_spent(self) -> int:
        """``costList`` 里钻石的 ``itemAmount`` 之和。

        .. warning::
           **数量口径尚未实证**（同 :meth:`energy_gained`）：可能是"操作后持有量"
           而不是"本次消耗量"。禁止直接当"消耗"写进用户可见文案。
        """
        diamond_id = int(CONSTS["DIAMOND_ID"])
        total = 0
        for obj in self.costList:
            for item in getattr(obj, "itemList", []):
                if int(item.itemID) == diamond_id:
                    total += int(item.itemAmount)
        return total
