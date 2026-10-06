"""失控炼成阵（Yimo）报文：**只读**部分（消息号 22101 → 22102）。

【这个系统是什么】
``SystemIDData`` systemID=51「失控煉成陣」→ UI 类 ``Yimo_MainUI``；
协议模块 ``YimoBattleModule``（22101~22114）。帮助文案实测
（``LanguageSettings_zhcn`` 第 10221 行）：

    1. 每日 01:00~23:00 開放失控煉成陣
    2. 破壞部位可造成暴走錫樵夫無法施放特殊技能，全部位破壞後暴走錫樵夫死亡
    3. 暴走錫樵夫被擊殺後將會重生並提高等級，攻擊後獲得的積分也會提高

也就是说这是一个"打 bosses 刷伤害/积分"的 PvE 玩法（配置表 ``YimoBossData`` /
``YimoPartData`` / ``YimoPointRewardData`` …）。

【为什么本项目在这里**只做只读**】
结果上报 ``22109 YimoBattleResult{fightToken, dmgHpRate, score, battleResult, token}``
里有 32 位 hex 的 ``token`` —— 实测 **我们还没拿到它的算法**（vCode 那套算法不适用，
详见 ``notes/battle_engine_project.md`` 的 U1）。因此在拿到公式之前，本模块只读状态。

> 有意思的一条实测：``22109`` 的存证可以**为空**（``{"BattleType":7,…,"Proofs":[]}``，
> 50 字节）且服务端返回 ``errorCode=0`` 正常发奖 —— 所以这个模式**不需要战斗引擎**，
> 只差 token 签名（见 ``notes/glory_summit_capture.md`` 第四节）。

.. warning::
   本模块**刻意不建模**：``22107``（进战）、``22109``（结算上报）、
   ``22103 BuyYimoCount``（**花钻石买次数**）、``22105``（开箱领奖）。
"""

from __future__ import annotations

from typing import Final

from pydantic import Field

from models.game_packet import ProtoMessage


class YimoBossDailyRewardClass(ProtoMessage):
    """失控炼成阵全服达标宝箱单项（消息号 22102 子包 rewardData 的元素）。"""

    proto_class_name = "YimoBossDailyRewardClass"

    #: 宝箱 ID（如 662000001, 662000002, 662000003）
    rewardID: int = 0
    #: 宝箱状态：0=未达成(未解锁), 1=可领取, 2=已领取
    rewardState: int = 0


class GetYimoState(ProtoMessage):
    """失控炼成阵状态请求（消息号 22101）。

    .. note::
       空报文 → 编码结果 ``b""``。**只读，不消耗任何次数。**
    """

    proto_class_name = "GetYimoState"


class OpenYimoRewardPacket(ProtoMessage):
    """失控炼成阵全服宝箱领取请求（消息号 22105）。空包。"""

    proto_class_name = "OpenYimoRewardPacket"


class OpenYimoRewardRes(ProtoMessage):
    """失控炼成阵全服宝箱领取响应（消息号 22106）。"""

    proto_class_name = "OpenYimoRewardRes"

    #: 业务错误码（0 = 成功）
    errorCode: int = 0
    #: 获得道具列表（GetObjClass 原始字节列表）
    getList: list[bytes] = Field(default_factory=list)

    def is_success(self) -> bool:
        """是否成功。"""
        return self.errorCode == 0


class GetYimoStateRes(ProtoMessage):
    """失控炼成阵状态响应（消息号 22102）。"""

    proto_class_name = "GetYimoStateRes"

    #: ★ **剩余挑战次数**
    remainCount: int = 0
    #: 我的排名
    nowRank: int = 0
    #: 我的积分
    nowScore: int = 0
    #: 已购买次数（只读；购买属另一板块的功能）
    buyExtraCount: int = 0
    #: **下一次购买次数的钻石价格**（``ItemClass``）。
    #:
    #: ⚠️ 本模块只是**展示**它，让使用者知道"再买要花多少" ——
    #: 本项目在竞技板块**绝不发购买请求**（``22103`` 已进禁发清单）。
    nextDiamondCost: bytes = b""
    #: 当前 boss 数据（``YimoBossDataClass`` 未建模 → 原始字节；用 :meth:`has_boss_data` 判断有无）
    bossData: bytes = b""
    #: 弹窗提示文案（``List<string>``，协议里就是字符串列表）
    popUpInfo: list[str] = Field(default_factory=list)
    #: 全服达标宝箱数据（共 3 份宝箱：662000001~662000003）
    rewardData: list[YimoBossDailyRewardClass] = Field(default_factory=list)
    #: 活动期间数据（``WorldBossActivityClass`` 未建模 → 计数）
    worldBossActivityClass: list[bytes] = Field(default_factory=list)

    def has_boss_data(self) -> bool:
        """服务端是否下发了 boss 数据（活动开放时才有）。"""
        return len(self.bossData) > 0

    def all_chests_claimed(self) -> bool:
        """3 份全服宝箱是否全部已经领取（全部 rewardState == 2）。"""
        if len(self.rewardData) < 3:
            return False
        return all(r.rewardState == 2 for r in self.rewardData)

    def has_claimable_chests(self) -> bool:
        """是否有达标且未领取的宝箱（rewardState == 1）。"""
        return any(r.rewardState == 1 for r in self.rewardData)

    def claimable_chest_ids(self) -> list[int]:
        """所有可领取的宝箱 ID 列表。"""
        return [r.rewardID for r in self.rewardData if r.rewardState == 1]

    def claimed_chest_count(self) -> int:
        """已领取的宝箱数量。"""
        return sum(1 for r in self.rewardData if r.rewardState == 2)

    def chest_summary(self) -> str:
        """宝箱状态摘要。"""
        claimed = self.claimed_chest_count()
        claimable = len(self.claimable_chest_ids())
        total = len(self.rewardData)
        return f"全服宝箱 {claimed}/{total} 已领（当前可领 {claimable} 个）"

    def summary(self) -> str:
        """一行摘要（日志用）。"""
        text = (
            f"剩余次数={self.remainCount}，已购买={self.buyExtraCount}，"
            f"积分={self.nowScore}，排名={self.nowRank}，"
            f"boss 数据={'有' if self.has_boss_data() else '无'}，"
            f"{self.chest_summary()}"
        )
        if self.nextDiamondCost:
            text += f"，下次购买价格字段 {len(self.nextDiamondCost)}B（仅展示，不购买）"
        if self.popUpInfo:
            text += f"，提示：{' / '.join(self.popUpInfo[:2])}"
        return text


# ---------------------------------------------------------------------------
# 出站消息号白名单 / 禁发清单
# ---------------------------------------------------------------------------
#: 允许发出：只读状态包 + 全服宝箱领取包。
ALLOWED_PACKET_IDS: Final[dict[str, int]] = {
    "GetYimoState": 22101,
    "OpenYimoRewardPacket": 22105,
}

#: 明确禁发（进战 / 结算 / 买次数）。
FORBIDDEN_PACKET_IDS: Final[dict[str, int]] = {
    "YimoBattleRequest": 22107,      # 进战：会扣次数
    "YimoBattleResult": 22109,       # 结算上报：需要 token 签名（见 battle_engine_project.md）
    "BuyYimoCountPacket": 22103,     # **花钻石买次数** ← 红线
}

#: 消息号 → 用途说明。
PACKET_PURPOSE: Final[dict[int, str]] = {
    22101: "失控炼成阵状态（只读：剩余次数、积分、排名、全服宝箱状态）",
    22105: "失控炼成阵全服通关宝箱领取（0 消耗）",
}

