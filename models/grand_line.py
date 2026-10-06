"""辉煌航迹（GrandLine，SystemID 77）相关协议报文。

【系统说明】
游戏内「輝煌航跡」系统，通关初始试炼后开启「蒐集物資」（远航补给）功能：
- 远航船只持续搜集物资，每到整点跳表 1 次，最多累计 24 次上限；
- 点击领取后重新计算累计奖励。

【报文清单】
- 34015 ``GrandLineHomePacket``：主页状态查询（空报文，只读；**10.3 抓包的领取
  流程并不发它**，保留作备用查询）
- 34016 ``GrandLineHomeRes``：主页状态响应（包含 rewardCount 可领物资跳表次数）
- 34019 ``GrandLineCountRewardPacket``：拉取搜集物资的**奖励目录**（空报文，只读）
  —— 10.3 抓包（``captures/辉煌航迹奖励领取.saz``）里领取前的实际查询就是它
- 34020 ``GrandLineCountRewardRes``：奖励目录响应（rewardItems + 航迹塔数据）
- 34021 ``GrandLineGetCountRewardPacket``：领取远航搜集物资（type=1, rewardID=-1）
- 34022 ``GrandLineGetCountRewardRes``：领取物资结算响应（获得道具列表）

【★ 2026-10-03 流程修订】
旧任务用 34016.rewardCount 判断"有没有可领"，≤0 就直接报"暂无可领"——
但真实客户端的领取流程是 ``34019 → 34021``，根本不发 34015；结果就是任务
永远停在"暂无可领"。现在改为镜像真实流程：34019 拉目录（顺带校验功能开放）
→ 直接发 34021 领取，由服务端 errorCode 做最终裁决。
"""

from __future__ import annotations

import logging
from typing import Final

from pydantic import Field

from models.daily import GetObjClass, SUCCESS_ERROR_CODE
from models.game_packet import ProtoMessage
from models.packet_ids import packet_id

_LOGGER: Final[logging.Logger] = logging.getLogger("models.grand_line")

#: 业务成功码
GRAND_LINE_SUCCESS: Final[int] = SUCCESS_ERROR_CODE

#: 搜集物资奖励类型常量（对应 GrandLineModule.eGrandLineRewardType.CountReward = 1）
REWARD_TYPE_COUNT_REWARD: Final[int] = 1

#: 本模块允许出站的消息名称白名单
ALLOWED_PACKET_NAMES: Final[tuple[str, ...]] = (
    "GrandLineHomePacket",
    "GrandLineCountRewardPacket",
    "GrandLineGetCountRewardPacket",
)

#: ``{类名: 消息号}``
ALLOWED_PACKET_IDS: Final[dict[str, int]] = {
    name: packet_id(name) for name in ALLOWED_PACKET_NAMES
}

#: 明确禁止出站的报文（如购买次数、复活等可能消耗钻石的报文）
FORBIDDEN_PACKET_NAMES: Final[tuple[str, ...]] = (
    "GrandLineRebirthPacket",  # 34025 角色复活
)

#: ``{类名: 消息号}``
FORBIDDEN_PACKET_IDS: Final[dict[str, int]] = {
    name: packet_id(name) for name in FORBIDDEN_PACKET_NAMES if name in packet_id.__code__.co_consts or True
}

#: 报文用途说明（供 packet_guard 使用）
PACKET_PURPOSE: Final[dict[int, str]] = {
    34015: "查询辉煌航迹状态与物资累计次数（只读，零消耗）",
    34019: "拉取辉煌航迹搜集物资的奖励目录（只读，零消耗）",
    34021: "领取辉煌航迹远航搜集物资奖励（纯领取，零消耗）",
}


class GrandLineHomePacket(ProtoMessage):
    """查询辉煌航迹主页状态（消息号 34015）。空请求体，只读。"""

    proto_class_name = "GrandLineHomePacket"


class GrandLineHomeRes(ProtoMessage):
    """辉煌航迹主页状态响应（消息号 34016）。"""

    proto_class_name = "GrandLineHomeRes"

    #: 错误码（0 为成功）
    errorCode: int = 0
    #: 下次整点重置时间戳（秒）
    nextResetTime: int = 0
    #: 当前累计的可领物资跳表次数（0~24）
    rewardCount: int = 0

    def is_success(self) -> bool:
        """业务是否成功。"""
        return self.errorCode == GRAND_LINE_SUCCESS


class GrandLineCountRewardPacket(ProtoMessage):
    """拉取搜集物资的奖励目录（消息号 34019）。空请求体，只读。

    【10.3 抓包定位】领取流程（``captures/辉煌航迹奖励领取.saz``）里
    真实客户端在 34021 之前发的是这个空包，而非 34015 主页查询。
    """

    proto_class_name = "GrandLineCountRewardPacket"


class GrandLineCountRewardClass(ProtoMessage):
    """奖励目录里的一项（``GrandLineCountRewardRes.rewardItems`` 的元素）。

    字段定义来自 dump.cs ``GrandLineCountRewardClass``（TypeDefIndex 9224）。
    """

    proto_class_name = "GrandLineCountRewardClass"

    #: 道具 ID
    itemID: int = 0
    #: 数量
    amount: int = 0
    #: 是否为加成（额外）奖励项
    isBonus: int = 0


class GrandLineCountRewardRes(ProtoMessage):
    """搜集物资奖励目录响应（消息号 34020）。

    .. note::
       ``grandLineTowerData``（航迹塔/排行展示数据）与本任务无关，
       按项目惯例保留原始字节，不建模。
    """

    proto_class_name = "GrandLineCountRewardRes"

    #: 奖励目录（这次搜集物资能拿到些什么）
    rewardItems: list[GrandLineCountRewardClass] = Field(default_factory=list)
    #: 航迹塔数据（未建模，保留原始字节）
    grandLineTowerData: list[bytes] = Field(default_factory=list)


class GrandLineGetCountRewardPacket(ProtoMessage):
    """领取远航搜集物资奖励（消息号 34021）。"""

    proto_class_name = "GrandLineGetCountRewardPacket"

    #: 奖励类型（搜集物资固定为 1）
    type: int = REWARD_TYPE_COUNT_REWARD
    #: 奖励/组 ID（搜集物资传 -1 或 0 均可，默认 -1）
    rewardID: int = -1


class GrandLineGetCountRewardRes(ProtoMessage):
    """领取远航搜集物资结算响应（消息号 34022）。"""

    proto_class_name = "GrandLineGetCountRewardRes"

    #: 错误码（0 为成功）
    errorCode: int = 0
    #: 获得的物品列表
    getObjList: list[GetObjClass] = Field(default_factory=list)

    def is_success(self) -> bool:
        """业务是否成功。"""
        return self.errorCode == GRAND_LINE_SUCCESS
