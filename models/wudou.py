"""天命对决（WuDou）报文模型与协议定义。

【架构说明】
- 官方在底层通过 ``is_WuDouPVP_Rp = true`` 启用了纯服务器推演模式；
- 真实开战触发包为 ``26051 WuDouReplayPacket``；
- 云端推演结算后直接下发 ``26052 WuDouReplayRes``（含对决结果与完整回放）；
- 开战前通过 ``26053 WuDouRankPacket`` 进行乐观并发锁校验（防排名过期）；
- 旧的 ``26037`` / ``26039`` 已被底层标记为 [Obsolete] 废弃。

.. warning::
   严格禁止发送 ``26047 WuDouBuyChallenge``（花钻石买次数）。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Final

from pydantic import Field

from models.game_packet import ProtoMessage


def _format_ms(value: int) -> str:
    """把毫秒时间戳格式化成 ``YYYY-MM-DD HH:MM:SS``；0/负数返回 ``<无>``。"""
    if value <= 0:
        return "<无>"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value / 1000))


# ---------------------------------------------------------------------------
# 错误码
# ---------------------------------------------------------------------------
WUDOU_SUCCESS: Final[int] = 0
WUDOU_ERROR_NO_CHALLENGE_TIMES: Final[int] = -1
WUDOU_ERROR_HAVE_BEEN_USED: Final[int] = 5

WUDOU_ERROR_NOTES: Final[dict[int, str]] = {
    0: "成功",
    -1: "挑战次数不足（按设计：停止，绝不购买）",
    -2: "找不到目标玩家数据",
    -3: "目标排名已变更（冲突锁）",
    -4: "不在挑战开放时段",
    3: "找不到目标玩家（Cant_Find_Target）",
    4: "找不到队伍（Cant_Find_Team）",
    5: "挑战次数已用尽（HaveBeenUsedChallengeCount）",
    6: "处于结算时段中（InSettlementTime）",
    7: "目标排名变动（Error_TargetRank）",
    8: "不可挑战该玩家（Error_CantChallengePlayer）",
    9: "目标正在战斗中（OnFighting）",
    11: "钻石不足（NonEnough_DiamondRequest）",
    17: "服务器繁忙（ServerBusy）",
}


def is_no_times_error(error_code: int) -> bool:
    """是否属于挑战次数耗尽的错误码（-1 或 5）。"""
    return error_code in (WUDOU_ERROR_NO_CHALLENGE_TIMES, WUDOU_ERROR_HAVE_BEEN_USED)


def describe_error_code(error_code: int) -> str:
    """把错误码翻译成可读说明。"""
    return WUDOU_ERROR_NOTES.get(error_code, f"服务端返回错误码 {error_code}")


#: 队伍编号（对齐 dump.cs TeamType，TypeDefIndex 11030）
TEAM_TYPE_WUDOU_DEF: Final[int] = 14  # WuDouPVP_Def（防守队）
TEAM_TYPE_WUDOU_ATK: Final[int] = 70  # Wudou_PVP1（进攻队）


# ---------------------------------------------------------------------------
# 对手数据模型
# ---------------------------------------------------------------------------
class PVPListClass(ProtoMessage):
    """天命对决挑战列表中的对手条目。"""

    proto_class_name = "PVPListClass"

    #: 玩家 UID（NPC 通常为负数）
    playerID: int = 0
    #: 当前天梯名次
    ranking: int = 0
    #: 头像数据（未建模 -> 原始字节）
    icon: bytes = b""
    #: 玩家昵称
    name: str = ""
    #: 防守战力
    fighting: int = 0
    #: 天梯积分
    integral: int = 0
    #: 公会名称
    allianceName: str = ""
    #: 点赞/崇拜数
    worship: int = 0
    #: 公会职位
    allianceTitle: int = 0

    @property
    def is_npc(self) -> bool:
        """是否为系统 NPC 训练家（依据底层 get_PID() < 0 或名称带 NPC）。"""
        return self.playerID < 0 or "NPC" in self.name.upper()


class TeamClass(ProtoMessage):
    """天命对决队伍出战配置简要模型。"""

    proto_class_name = "TeamClass"

    teamID: int = 0
    rolesSid: list[int] = Field(default_factory=list)
    teamUse: int = 0
    mirrorBuffID: int = 0
    flagID: int = 0


# ---------------------------------------------------------------------------
# 26031 / 26032：主页
# ---------------------------------------------------------------------------
class WuDouHome(ProtoMessage):
    """天命对决主页请求（消息号 26031）。"""

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
    #: 我的出战队伍
    team: TeamClass = Field(default_factory=TeamClass)
    #: 前列玩家（``PVPListClass``）
    topList: list[PVPListClass] = Field(default_factory=list)
    #: 分组列表（``WudouGroupListClass`` 未建模）
    wudouGroupListClz: list[bytes] = Field(default_factory=list)
    #: 我所在的分组 ID
    myGroupID: int = 0
    #: ★ **剩余挑战次数**
    challenge_times: int = 0

    def summary(self) -> str:
        """一行摘要（日志用）。"""
        # ★ 2026-10-18：`分组=501000003` 是内部编号，用户看不懂，删掉；
        # `=` 改成中文文案惯用的空格分隔。
        return (
            f"剩余挑战次数 {self.challenge_times}，"
            f"周榜 {len(self.weekRanking)} 条、前列 {len(self.topList)} 条"
        )


# ---------------------------------------------------------------------------
# 26033 / 26034：挑战页
# ---------------------------------------------------------------------------
class WuDouChallengeHome(ProtoMessage):
    """天命对决挑战页请求（消息号 26033）。"""

    proto_class_name = "WuDouChallengeHome"

    #: 测试开关（实测为空字符串）
    openTest: str = ""


class WuDouChallengeHomeRes(ProtoMessage):
    """天命对决挑战页响应（消息号 26034）。"""

    proto_class_name = "WuDouChallengeHomeRes"

    #: 我的名次
    myRanking: int = 0
    #: 可挑战的对手列表
    challengeList: list[PVPListClass] = Field(default_factory=list)
    #: 下次刷新时间（毫秒）
    reflash_time: int = 0
    #: 挑战计时/冷却（毫秒）
    challenge_time: int = 0
    #: NPC 挑战次数
    npc_times: int = 0
    #: ★ **剩余挑战次数**
    challenge_times: int = 0
    #: 已购买次数（只读）
    buyedCount: int = 0
    #: 结算时间（毫秒）
    settlement_time: int = 0

    def opponent_count(self) -> int:
        """可挑战对手数量。"""
        return len(self.challengeList)

    def summary(self) -> str:
        """一行摘要（日志用）。"""
        # ★ 2026-10-18：删掉 `NPC 次数` / `已购买`（内部/低频信息），
        # `=` 改空格 —— 这行会进"天命对决主页"的日志。
        return (
            f"剩余挑战次数 {self.challenge_times}，我的名次 {self.myRanking}，"
            f"对手 {self.opponent_count()} 个，"
            f"结算时间 {_format_ms(self.settlement_time)}"
        )


# ---------------------------------------------------------------------------
# 26053 / 26054：乐观并发排名核验
# ---------------------------------------------------------------------------
class WuDouRankPacket(ProtoMessage):
    """天命对决开战前排名核验请求（消息号 26053）。"""

    proto_class_name = "WuDouRankPacket"

    targetPid: int = 0
    myRanking: int = 0
    targetRanking: int = 0
    isBlock: bool = False


class WuDouRankRes(ProtoMessage):
    """天命对决开战前排名核验响应（消息号 26054）。"""

    proto_class_name = "WuDouRankRes"

    errorCode: int = 0

    def is_success(self) -> bool:
        return self.errorCode == WUDOU_SUCCESS


# ---------------------------------------------------------------------------
# 26051 / 26052：核心挑战与云端结算
# ---------------------------------------------------------------------------
class WuDouReplayPacket(ProtoMessage):
    """天命对决挑战请求（消息号 26051）。"""

    proto_class_name = "WuDouReplayPacket"

    #: 目标对手 UID
    targetPid: int = 0
    #: 我方出战队伍 ID
    teamID: int = 0
    #: 我方当前名次
    myRanking: int = 0
    #: 目标对手当前名次
    targetRanking: int = 0


class WuDouReplayRes(ProtoMessage):
    """天命对决挑战响应（消息号 26052）。"""

    proto_class_name = "WuDouReplayRes"

    errorCode: int = 0
    replayResult: str = ""
    challenge_time: int = 0
    myRanking: int = 0
    npc_times: int = 0
    challenge_times: int = 0
    gotList: list[bytes] = Field(default_factory=list)
    winRewardList: list[bytes] = Field(default_factory=list)

    def is_success(self) -> bool:
        return self.errorCode == WUDOU_SUCCESS

    def battle_summary(self) -> dict[str, Any]:
        """从战报 JSON 里解析胜负等关键信息。"""
        if not self.replayResult:
            return {}
        try:
            parsed = json.loads(self.replayResult)
        except (ValueError, TypeError):
            return {}
        if not isinstance(parsed, dict):
            return {}
        picked: dict[str, Any] = {}
        for key in ("winOrFail", "elapsed", "step", "isNpc", "error", "replay_file_name"):
            if key in parsed:
                picked[key] = parsed[key]
        return picked

    def summary(self) -> str:
        """一行摘要（日志用）。"""
        battle = self.battle_summary()
        outcome = battle.get("winOrFail")
        outcome_text = "胜" if outcome is True else "负" if outcome is False else "未知"
        # ★ 2026-10-18：逐场行不再堆内部字段（旧文案是
        # ``errorCode=0（成功），结果=胜，最新名次=12，剩余挑战次数=4，
        # NPC剩余次数=3，获得奖励=1 项``）。只留胜负 / 名次 / 剩余次数 / 奖励；
        # 失败时才报错误码。
        if self.errorCode != 0:
            return (
                f"本场未结算（errorCode={self.errorCode}："
                f"{describe_error_code(self.errorCode)}）"
            )
        reward = f"，获得 {len(self.winRewardList)} 项奖励" if self.winRewardList else ""
        return (
            f"本场{outcome_text}，最新名次 {self.myRanking}，"
            f"剩余次数 {self.challenge_times}{reward}"
        )


# ---------------------------------------------------------------------------
# 出站消息号白名单 / 禁发清单
# ---------------------------------------------------------------------------
#: 允许发出：只读查询包 + 排名核验包 + 纯云端挑战包。
ALLOWED_PACKET_IDS: Final[dict[str, int]] = {
    "WuDouHome": 26031,
    "WuDouChallengeHome": 26033,
    "WuDouRankPacket": 26053,
    "WuDouReplayPacket": 26051,
}

#: 明确禁发（进战 / 结算 / 买次数 / 刷新）。
FORBIDDEN_PACKET_IDS: Final[dict[str, int]] = {
    "WuDouChallengeReady": 26037,     # 进战：底层已废弃 [Obsolete]
    "WuDouChallenge": 26039,          # 结算上报：底层已废弃 [Obsolete]
    "WuDouBuyChallenge": 26047,       # **花钻石买次数** ← 严格红线
    "WuDouRefresh": 26035,            # 刷新对手
}

#: 消息号 → 用途说明。
PACKET_PURPOSE: Final[dict[int, str]] = {
    26031: "天命对决主页（只读：剩余挑战次数、分组、周榜）",
    26033: "天命对决挑战页（只读：剩余次数、NPC 次数、我的名次、结算时间、对手列表）",
    26053: "天命对决排名核验（乐观并发锁，确认对手名次未过期）",
    26051: "天命对决发起挑战（纯云端权威推演，下发全量战果）",
}
