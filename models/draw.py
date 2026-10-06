"""抽卡相关报文模型（消息号 12101~12106）。

【抓包与逆向依据】
- captures/每日免费一次抽卡.saz
- 12105 GetDrawMachineDataNewPacket（空包）→ 12106 GetDrawMachineDataNewRes
- 12101 DrawNewPacket{drawMachineID, actionType=0, erogesType=-1, platformToken, useTicket=1}
  → 12102 DrawNewRes{errorCode, getList, costList, drawMachineDataClass}

【零钻石红线】
在组装/发送 12101 之前，必须核对 12106 下发的 drawMachineDataClass.drawCount 与
配置表 DrawMachineData.drawFreeTimes：
只有在 drawCount < drawFreeTimes 时才执行免费抽卡，绝不发任何需钻石的请求。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Final

from pydantic import Field

import config
from models.game_packet import ProtoMessage

_LOGGER: Final[logging.Logger] = logging.getLogger("models.draw")


class GetDrawMachineDataNewPacket(ProtoMessage):
    """拉取卡池数据请求（消息号 12105）。空包。"""

    proto_class_name = "GetDrawMachineDataNewPacket"


class DrawMachineNewDataClass(ProtoMessage):
    """单个卡池的实时状态数据（消息号 12106 子包的元素）。"""

    proto_class_name = "DrawMachineNewDataClass"

    #: 卡池 ID（对应 DrawMachineData 配置表主键，如 252000710）
    drawMachineID: int = 0
    #: 今日已抽次数（免费池抽完后会从 0 变 1）
    drawCount: int = 0
    #: 上次抽取时间戳
    lastDrawTime: int = 0
    #: 保底目标次数
    targetCount: int = 0
    #: 单抽文案
    oneDrawDesc: str = ""
    #: 十连文案
    tenDrawDesc: str = ""
    #: 小 Banner 资源名
    samllBanner: str = ""
    #: 大 Banner 资源名
    bigBanner: str = ""
    #: 卡池关闭时间（毫秒时间戳）
    closeTime: int = 0
    #: 活动提示
    activityTip: str = ""
    #: 是否常驻池
    isForver: bool = False
    #: 累计次数
    accumulateCount: int = 0
    #: 奖励计数
    rewardCount: int = 0


class GetDrawMachineDataNewRes(ProtoMessage):
    """拉取卡池数据响应（消息号 12106）。"""

    proto_class_name = "GetDrawMachineDataNewRes"

    #: 各卡池数据列表
    drawMachineDataClass: list[DrawMachineNewDataClass] = Field(default_factory=list)


class DrawNewPacket(ProtoMessage):
    """抽卡请求（消息号 12101）。"""

    proto_class_name = "DrawNewPacket"

    #: 卡池 ID
    drawMachineID: int = 0
    #: 抽取类型：0=单抽, 1=十连
    actionType: int = 0
    #: 平台类型，抓包实测为 -1
    erogesType: int = -1
    #: 平台认证令牌：base64("jwt=" + accessToken) + "$v2"
    platformToken: str = ""
    #: 是否使用券/免费：抓包实测为 1
    useTicket: int = 1


class DrawNewRes(ProtoMessage):
    """抽卡响应（消息号 12102）。"""

    proto_class_name = "DrawNewRes"

    #: 业务错误码（0 = 成功）
    errorCode: int = 0
    #: 获得的角色/道具明细（GetObjClass 原始字节列表）
    getList: list[bytes] = Field(default_factory=list)
    #: 消耗的道具明细（CostClass 原始字节列表，免费抽卡时为空）
    costList: list[bytes] = Field(default_factory=list)
    #: 抽卡后更新的卡池状态数据
    drawMachineDataClass: bytes = b""

    def is_success(self) -> bool:
        """是否成功。"""
        return self.errorCode == 0


def load_free_draw_machines(
    asset_dir: Path | None = None,
) -> dict[int, dict[str, Any]]:
    """从配置表 ``DrawMachineData`` 解析具有每日免费次数的卡池。

    :return: ``{drawMachineID: {"freeTimes": int, "cd": int, "group": str, "type": int}}``。
    """
    directory = asset_dir or config.TEXT_ASSET_DIR
    file_path = directory / "DrawMachineData"
    if not file_path.is_file():
        _LOGGER.warning("找不到 DrawMachineData 配置表：%s", file_path)
        # 兜底返回抓包中已验证的两个常驻池：角色常驻池与装备常驻池
        return {
            252000710: {"freeTimes": 1, "cd": -1, "group": "NULL", "type": 1},
            252000810: {"freeTimes": 1, "cd": -1, "group": "NULL", "type": 4},
        }

    free_pools: dict[int, dict[str, Any]] = {}
    with file_path.open("r", encoding="utf-8", errors="ignore") as f:
        line1 = f.readline().strip()
        header = [c.strip("\ufeff").strip("┤") for c in line1.split("|")]
        for line in f:
            line = line.strip().rstrip("┤")
            if not line:
                continue
            parts = line.split("|")
            row = dict(zip(header, parts))
            m_id_str = row.get("drawMachineID")
            free_times_str = row.get("drawFreeTimes")
            if not m_id_str or not free_times_str:
                continue
            try:
                m_id = int(m_id_str)
                free_times = int(free_times_str)
            except ValueError:
                continue

            if free_times > 0:
                try:
                    cd = int(row.get("drawFreeTimesCD", -1))
                except ValueError:
                    cd = -1
                try:
                    m_type = int(row.get("machineType", 0))
                except ValueError:
                    m_type = 0
                free_pools[m_id] = {
                    "freeTimes": free_times,
                    "cd": cd,
                    "group": row.get("drawMachineGroupID", "NULL"),
                    "type": m_type,
                }
    return free_pools


# ---------------------------------------------------------------------------
# 出站消息号白名单
# ---------------------------------------------------------------------------
ALLOWED_PACKET_IDS: Final[dict[str, int]] = {
    "GetDrawMachineDataNewPacket": 12105,
    "DrawNewPacket": 12101,
}

FORBIDDEN_PACKET_IDS: Final[dict[str, int]] = {}

PACKET_PURPOSE: Final[dict[int, str]] = {
    12105: "拉取卡池状态数据（0 消耗）",
    12101: "执行卡池免费单抽（必须判定 drawCount < drawFreeTimes）",
}
