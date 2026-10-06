"""天命对决（WuDou）报文：**只读**部分（26031 → 26032、26033 → 26034）。

【这个系统是什么】
``SystemIDData`` systemID=48「天命對決」→ UI 类 ``WuDouPvP_MainUI``；
协议模块 ``WuDouPvPNetWorkModule``（26031~26056）。帮助文案实测
（``LanguageSettings_zhcn`` 第 10243 行）：

    1.天命對決是分區跨服對決排名制。#2.每日23:00(UTC+8)會結算一次獎勵…
    3.每週一23:00~23:30(UTC+8)會結算一次獎勵，結算期間無法戰鬥。

【为什么本项目在这里**只做只读**】
- ``26038 WuDouChallengeReadyRes`` 会下发**双方参战角色明细**
  （``opponentData`` / ``battleClass``，``List<RoleBattleOrDetailClass>``）——
  这是"让客户端自己算"的输入；
- ``26039 WuDouChallenge`` 的字段（``result/type/playerID/fightingNumber/fightToken/
  battleResult/token``）与竞技场 ``26019`` **逐字相同**，即同样要客户端上报带存证的结果。

**外加一个更硬的障碍**：实测两次抓包里都**没有**进战/结算包（只有战后流量），
而次数确实被扣、战报时间戳落在抓包窗口内 —— 说明它们走的很可能不是 Fiddler 能看到的
HTTP 通道（握手曾下发 ``serverPort=1888``，客户端里也存在 ``NetWorkUdpModule`` /
``CommonTcpClient``）。详见 ``notes/glory_summit_capture.md`` 第三节。

.. warning::
   本模块**刻意不建模**：``26037``（进战）、``26039``（结算上报）、
   ``26047 WuDouBuyChallenge``（**花钻石买次数**）、``26035``（刷新对手）。
   ``26031`` 的 ``groupID`` 实测客户端发的是 **-1**（见下）。
"""

from __future__ import annotations

import time
from typing import Final

from pydantic import Field

from models.game_packet import ProtoMessage


def _format_ms(value: int) -> str:
    """把毫秒时间戳格式化成 ``YYYY-MM-DD HH:MM:SS``；0/负数返回 ``<无>``。

    【为什么需要它】``reflash_time`` / ``settlement_time`` 都是毫秒时间戳，
    直接打进日志没人看得懂"结算还剩多久"。
    """
    if value <= 0:
        return "<无>"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value / 1000))


class WuDouHome(ProtoMessage):
    """天命对决主页请求（消息号 26031）。

    :param groupID: 活动组 ID。**实测真实客户端发的是 ``-1``**（``08 ffffffffffffffffff01``）——
        照抄实测值，不做本地推算（按本项目"宁可照抄实测也不猜"的原则）。
    """

    proto_class_name = "WuDouHome"

    #: 活动组 ID（实测评测值：``-1``）
    groupID: int = -1

    @classmethod
    def for_default_group(cls) -> "WuDouHome":
        """构造实测形态的请求（``groupID=-1``）。"""
        return cls(groupID=-1)


class WuDouHomeRes(ProtoMessage):
    """天命对决主页响应（消息号 26032）。"""

    proto_class_name = "WuDouHomeRes"

    #: 周榜（``WuDouWeekRankClass`` 未建模 → 原始字节列表）
    weekRanking: list[bytes] = Field(default_factory=list)
    #: 我的出战队伍（``TeamClass`` 未建模）
    team: bytes = b""
    #: 前列玩家（``PVPListClass`` 未建模）
    topList: list[bytes] = Field(default_factory=list)
    #: 分组列表（``WudouGroupListClass`` 未建模）
    wudouGroupListClz: list[bytes] = Field(default_factory=list)
    #: 我所在的分组 ID
    myGroupID: int = 0
    #: ★ **剩余挑战次数**
    challenge_times: int = 0

    def summary(self) -> str:
        """一行摘要（日志用）。"""
        return (
            f"剩余挑战次数={self.challenge_times}，分组={self.myGroupID}，"
            f"周榜 {len(self.weekRanking)} 条、前列 {len(self.topList)} 条"
        )


class WuDouChallengeHome(ProtoMessage):
    """天命对决"挑战页"请求（消息号 26033）。

    :param openTest: 测试开关（协议里是 **string**）。**抓包实测子包为空**（没有字段），
        所以正常路径下不要给它赋值。
    """

    proto_class_name = "WuDouChallengeHome"

    #: 测试开关（实测为空字符串）
    openTest: str = ""


class WuDouChallengeHomeRes(ProtoMessage):
    """天命对决"挑战页"响应（消息号 26034）—— **本板块只读信息的主要来源**。"""

    proto_class_name = "WuDouChallengeHomeRes"

    #: 我的名次
    myRanking: int = 0
    #: 可挑战的对手列表（``PVPListClass`` 未建模 → 只计数）
    challengeList: list[bytes] = Field(default_factory=list)
    #: 下次刷新时间（毫秒）
    reflash_time: int = 0
    #: 挑战计时/冷却（毫秒）
    challenge_time: int = 0
    #: NPC 挑战次数
    npc_times: int = 0
    #: ★ **剩余挑战次数**
    challenge_times: int = 0
    #: 已购买次数（只读；购买属另一板块的功能）
    buyedCount: int = 0
    #: 结算时间（毫秒）—— 帮助文案："每周一23:00~23:30結算，結算期間無法戰鬥"
    settlement_time: int = 0

    def opponent_count(self) -> int:
        """可挑战对手数量（只计数）。"""
        return len(self.challengeList)

    def summary(self) -> str:
        """一行摘要（日志用）。"""
        return (
            f"剩余挑战次数={self.challenge_times}，NPC 次数={self.npc_times}，"
            f"已购买={self.buyedCount}，我的名次={self.myRanking}，"
            f"对手 {self.opponent_count()} 个，结算时间={_format_ms(self.settlement_time)}"
        )


# ---------------------------------------------------------------------------
# 出站消息号白名单 / 禁发清单
# ---------------------------------------------------------------------------
#: 允许发出：两个只读查询包。
ALLOWED_PACKET_IDS: Final[dict[str, int]] = {
    "WuDouHome": 26031,
    "WuDouChallengeHome": 26033,
}

#: 明确禁发（进战 / 结算 / 买次数 / 刷新）。
FORBIDDEN_PACKET_IDS: Final[dict[str, int]] = {
    "WuDouChallengeReady": 26037,     # 进战：会扣次数
    "WuDouChallenge": 26039,          # 结算上报：需要战斗引擎 + 存证
    "WuDouBuyChallenge": 26047,       # **花钻石买次数** ← 红线
    "WuDouRefresh": 26035,            # 刷新对手（改动服务端候选列表）
    "WuDouReplayPacket": 26051,       # 拉战报回放（战斗侧）
    "WuDouRankPacket": 26053,         # 排行榜（本阶段不需要）
}

#: 消息号 → 用途说明。
PACKET_PURPOSE: Final[dict[int, str]] = {
    26031: "天命对决主页（只读：剩余挑战次数、分组、周榜）",
    26033: "天命对决挑战页（只读：剩余次数、NPC 次数、我的名次、结算时间）",
}
