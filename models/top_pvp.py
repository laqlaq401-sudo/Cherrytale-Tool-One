"""荣耀之巅（TopPvp）报文：消息号 39001/39002、39019/39020、39007/39008。

【这个系统是什么】
``SystemIDData`` 里 systemID=68「榮耀之巔」，描述是"白皇后與紅心皇后聯手打造的終極戰場"，
段位配置见 ``TopPvpClassData``（勇者III/II/I…），每日免费挑战次数是
``eOtherSettings.TopPvp_FreeChallengeTimes = 395``（表 ``OtherSettingsData``）。

【★ 为什么这个板块能做到"纯协议自动战斗"，而竞技场做不到】
实测抓包（``captures/荣耀之巅.saz``，解析结论见 ``notes/glory_summit_capture.md``）证明：

    39007 TopPvpChallengePacket 的**请求体 = 0 字节**（客户端什么都不用提供）
    39008 TopPvpChallengeRes = {errorCode, gotList, rewardList,
                               versusInfo_Me, versusinfo_Enemy, replayResult(46KB 战报 JSON)}

也就是说**整场战斗由服务器算完**，客户端只负责播回放。对照竞技场：

    26004 ChallengeRes 会把 opponentAssemble / mineAssemble（**双方阵容**）发下来
    26019 ChallengeResultPacket 要客户端**上报结果**（battleResult 里还含 13.9 万字节的逐施法存证）

一个"发阵容给你自己算"、一个"直接给结果"，形状完全不同 —— 这就是本板块不需要
任何战斗引擎的原因（详见 ``notes/battle_engine_project.md``）。

【本模块的四个报文，以及"跳过战斗演出"开关】

=====================================  ==============================================
39001 GetTopPvpHomePacket（空请求）     拉主页。字段 16 ``isSkipBattle`` = 「跳过战斗演出」
   → 39002 GetTopPvpHomeRes            开关在**服务端**的当前状态（本项目默认置 True）
39019 TopPvpSetSkipBattleTogglePacket  写这个开关（``isSkipBattle=True``）
   → 39020 …Res{errorCode}
39007 TopPvpChallengePacket（空请求）   发起一场挑战（**会消耗一次次数**）
   → 39008 …Res                        服务器算完整场 → 回战报 + 积分变化 + 奖励
=====================================  ==============================================

.. warning::
   本模块**只建模**上面这几个包，刻意**不建**以下报文 —— 让"花钻石买次数"这件事
   在类型层面就不存在（见 ``tasks/top_pvp.py`` 的四道防线）：

   - 39005 ``TopPvpBuyChallengeCountPacket``（**花钻石买挑战次数**，禁止）
   - 39003 ``ReceiveTopPvpRewardPacket``（领奖，本阶段不在范围内）

【⚠️ 一处诚实标注的未知】
``39002`` 里**没有**"剩余可挑战次数"字段（只有 ``buyChallengeCount`` = 已购买次数）。
因此本项目**不推算**次数，只用三重约束：用户显式场数开关 + 硬上限 + 服务器错误码
``challenge_times_not_enough(-1)``。第一次真机跑 ``--battles 0`` 时会把 39002 的
实际字段值原样打出来，便于后续确认次数到底藏在哪（或需改读挑战券 ``TopPvp_Ticket``）。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Final

from pydantic import Field

from models.daily import GetObjClass, SUCCESS_ERROR_CODE
from models.game_packet import ProtoMessage

_LOGGER: Final[logging.Logger] = logging.getLogger("models.top_pvp")


# ---------------------------------------------------------------------------
# 服务端错误码（来自客户端枚举 TopPvpNetWorkModule.eTopPvpPacketErrorCode）
# ---------------------------------------------------------------------------
#: 业务成功码（与其它模块共用同一个 0，见 :data:`models.daily.SUCCESS_ERROR_CODE`）
TOP_PVP_SUCCESS: Final[int] = SUCCESS_ERROR_CODE

#: 「挑战次数不足」——循环的**正常终止条件**（不是异常）。
#: ⚠️ 收到它时任务必须直接收工，**绝不能**转去调用 39005 购买次数。
TOP_PVP_ERROR_NO_CHALLENGE_TIMES: Final[int] = -1

#: 错误码 → 人类可读说明（数值来自客户端枚举，勿凭印象改）。
TOP_PVP_ERROR_NOTES: Final[dict[int, str]] = {
    0: "成功",
    -1: "挑战次数不足（按设计：停止，绝不购买）",
    -2: "找不到目标玩家数据",
    -3: "资源不足",
    -4: "购买次数已达上限",
    -11: "当前赛季未开启",
    -12: "休赛期",
    -13: "数据错误",
    -14: "奖励结算中",
}


def describe_error_code(error_code: int) -> str:
    """把错误码翻译成一行说明（未知码原样返回，便于发现新情况）。"""
    return TOP_PVP_ERROR_NOTES.get(error_code, f"未知错误码 {error_code}")


# ---------------------------------------------------------------------------
# 39001 / 39002：主页（含「跳过战斗演出」开关的服务端状态）
# ---------------------------------------------------------------------------
class GetTopPvpHomePacket(ProtoMessage):
    """荣耀之巅主页请求（消息号 39001）。

    .. note::
       空报文（dump.cs 里它没有任何 ``ProtoMember`` 字段）→ 编码结果就是 ``b""``，
       服务端靠 ``RootPacket.packetID`` 识别意图。**只读，不消耗任何次数**。
    """

    proto_class_name = "GetTopPvpHomePacket"


class TopPvpRankClass(ProtoMessage):
    """段位榜/总榜的一条记录（``GetTopPvpHomeRes.classRankClassList`` 的元素）。"""

    proto_class_name = "TopPvpRankClass"

    #: 名次
    ranking: int = 0
    #: 头像（协议里是 ``IconClass``，本项目未建模 → 保留原始字节，将来要展示再补）
    iconData: bytes = b""
    #: 段位积分
    rankPoing: int = 0
    #: 段位 ID（对应配置表 ``TopPvpClassData.id``，如 721000001 = 勇者III）
    rankClassID: int = 0
    #: 是不是"我"（客户端用它高亮自己那一行）
    isMy: bool = False


class GetTopPvpHomeRes(ProtoMessage):
    """荣耀之巅主页响应（消息号 39002）。"""

    proto_class_name = "GetTopPvpHomeRes"

    #: 业务错误码（0 = 成功）
    errorCode: int = 0
    #: ⚠️ 已**购买**的挑战次数（不是"剩余次数"，实测见模块文档的未知项说明）
    buyChallengeCount: int = 0
    #: ⚠️ 语义**待确认**：字段名是 ``rewardCount``，但真机实测值是 **2026**
    #: （既不像数量也不像次数，怀疑是赛季号/年份之类的 ID）。
    #: 因此本模型只**原样保存并打印**它，绝不据此做任何判断。
    rewardCount: int = 0
    #: 段位榜（同段位内的排名）
    classRankClassList: list[TopPvpRankClass] = Field(default_factory=list)
    #: 总榜
    allRankClassList: list[TopPvpRankClass] = Field(default_factory=list)
    #: 本场/本周之前的段位 ID、之后的段位 ID（升段动画用）
    beforeClassID: int = 0
    afterClassID: int = 0
    #: 今日加成角色
    bonusRoleID: int = 0
    #: 加成刷新时间（注意协议里字段名就是 ``RefreshTIme`` 这个大小写）
    topPvpBonusRefreshTIme: int = 0
    #: 三个加成 buff（对应 ``ExperimentBuffData``）
    bonusExperimentBuffID_1: int = 0
    bonusExperimentBuffID_2: int = 0
    bonusExperimentBuffID_3: int = 0
    #: 是否有新内容（红点）
    isNew: bool = False
    #: 战旗列表（``BattleFlagsClass`` 未建模 → 保留原始字节）
    battleFlags: list[bytes] = Field(default_factory=list)
    #: 可领的"荣耀证明"奖励 ID 列表
    proveRewardID: list[int] = Field(default_factory=list)
    #: ★ 「跳过战斗演出」开关的**服务端当前状态**。
    #:
    #: 【为什么它是服务端字段而不是本地设置】
    #: 客户端里有 ``TopPvpSetSkipBattleTogglePacket``（39019）专门写它，说明这个开关
    #: 存在服务器上 —— 服务器据此决定"还发不发回放数据"（省流量）。
    #: 本项目按需求"默认开启"，所以任务在真正开打之前会确保它为 ``True``。
    isSkipBattle: bool = False

    def is_success(self) -> bool:
        """业务是否成功。"""
        return self.errorCode == TOP_PVP_SUCCESS

    def rank_entries(self) -> list[TopPvpRankClass]:
        """段位榜 + 总榜（顺序：先段位榜后总榜），便于一次性找"我"。"""
        return [*self.classRankClassList, *self.allRankClassList]

    def my_rank(self) -> TopPvpRankClass | None:
        """找出"我"在榜单里的那条记录（``isMy=True``）；没有就返回 ``None``。

        【为什么要这个方法】"我现在第几名/多少分"是每日汇报里最有用的一行，
        而它藏在两个列表里；调用方不应该各自写一遍遍历。
        """
        for entry in self.rank_entries():
            if entry.isMy:
                return entry
        return None

    def summary(self) -> str:
        """一行摘要（日志用）。"""
        mine = self.my_rank()
        rank_text = (
            f"我第{mine.ranking}名（段位{mine.rankClassID}，积分{mine.rankPoing}）"
            if mine is not None
            else "榜单里没有我的记录（可能尚未参战）"
        )
        return (
            f"{rank_text}；已购次数={self.buyChallengeCount}，"
            f"rewardCount={self.rewardCount}（字段语义待确认，仅原样展示），"
            f"跳过战斗演出={'已开启' if self.isSkipBattle else '已关闭'}"
        )


# ---------------------------------------------------------------------------
# 39019 / 39020：「跳过战斗演出」开关
# ---------------------------------------------------------------------------
class TopPvpSetSkipBattleTogglePacket(ProtoMessage):
    """写入「跳过战斗演出」开关（消息号 39019）。

    :param isSkipBattle: ``True`` = 跳过战斗演出（本项目默认）。

    .. note::
       这是**服务端持久化**的开关；写一次即可，之后 39002 的字段 16 会一直回 ``True``。
       因此任务只在"发现它是关闭的、且确实要开打"时才写（见 ``tasks/top_pvp.py``）。

    .. note::
       写作背景：客户端 UI 文案实测为「已开启，跳过战斗演出」/「已关闭，跳过战斗演出」
       （``LanguageSettings_zhcn`` 第 14370 / 14469 行），UI 类 ``TopPvpMain_UIView``
       里对应 ``Toggle m_skipBattle``。本项目按需求把它**默认开启**。
    """

    proto_class_name = "TopPvpSetSkipBattleTogglePacket"

    #: 是否跳过战斗演出（True = 跳过）
    isSkipBattle: bool = True

    @classmethod
    def enable(cls) -> "TopPvpSetSkipBattleTogglePacket":
        """构造"开启跳过战斗演出"的请求（字段默认值就是 True，本方法只为可读性）。"""
        return cls(isSkipBattle=True)


class TopPvpSetSkipBattleToggleRes(ProtoMessage):
    """写入开关的响应（消息号 39020）。"""

    proto_class_name = "TopPvpSetSkipBattleToggleRes"

    #: 业务错误码（0 = 成功）
    errorCode: int = 0

    def is_success(self) -> bool:
        """业务是否成功。"""
        return self.errorCode == TOP_PVP_SUCCESS


# ---------------------------------------------------------------------------
# 39007 / 39008：挑战（**服务器算完整场**）
# ---------------------------------------------------------------------------
class TopTeamEditInfoPacket(ProtoMessage):
    """队伍编辑信息请求（消息号 39011）。

    :param sectionID: 赛区/关卡段 ID（抓包实测 **726010017**，来源未确认 →
        由 ``config.TOP_PVP_SECTION_ID`` 提供，可用环境变量覆盖）。
    :param actionType: 动作类型（抓包实测 **0**）。

    ⚠️ **实测：真实客户端在挑战前必发这一条**（``captures/荣耀之巅.saz`` 的 raw/01，
    紧邻其后的 raw/02 才是挑战 39007）。因此本任务把它作为"进入模式"的第一步镜像下来，
    而不是只发挑战 —— 少发这一步时，服务端对挑战的响应不是游戏协议
    （见 ``notes/recon_findings.md`` 的 39007 排查记录）。
    """

    proto_class_name = "TopTeamEditInfoPacket"

    #: 赛区/关卡段 ID
    sectionID: int = 0
    #: 动作类型（实测 0）
    actionType: int = 0

    @classmethod
    def for_section(cls, section_id: int, *, action_type: int = 0) -> "TopTeamEditInfoPacket":
        """构造"拉取/编辑某赛区队伍信息"的请求。"""
        return cls(sectionID=section_id, actionType=action_type)


class TopTeamEditInfoRes(ProtoMessage):
    """队伍编辑信息响应（消息号 39012）：可用角色榜 + **当前防守队伍**。"""

    proto_class_name = "TopTeamEditInfoRes"

    #: 可用角色榜（``PopularRoleRankingsClass`` 未建模 → 保留原始字节）
    roleRankList: list[bytes] = Field(default_factory=list)
    #: 上次更新时间（毫秒）
    lastUpdateTime: int = 0
    #: ★ 当前防守队伍（``TeamClass`` 未建模 → 保留原始字节）。
    #: 实测抓包里这条有 3 个元素 —— 说明该账号此前已配置过防守队伍。
    defTeamClass: list[bytes] = Field(default_factory=list)

    def summary(self) -> str:
        """一行摘要（日志用）。"""
        return (
            f"可选角色 {len(self.roleRankList)} 个、"
            f"防守队伍 {'已配置' if self.defTeamClass else '**未配置**'}"
            f"（{len(self.defTeamClass)} 队）"
        )


class TopPvpChallengePacket(ProtoMessage):
    """发起一场荣耀之巅挑战（消息号 39007）。

    .. warning::
       **本请求会消耗一次挑战次数**（实测：发起后被服务端扣减，见
       ``notes/glory_summit_capture.md``）。因此它只在用户显式给出场数
       （``--battles N``，N>0）时才会被发送 —— 默认的只读模式绝不发它。

    .. note::
       请求体是**空**的（服务器自己配对对手），所以编码结果就是 ``b""``。
    """

    proto_class_name = "TopPvpChallengePacket"


class TopPvpVersusInfoClass(ProtoMessage):
    """对战双方的对局信息（``TopPvpChallengeRes.versusInfo_Me`` 等）。"""

    proto_class_name = "TopPvpVersusInfoClass"

    #: 当前积分
    point: int = 0
    #: 生效的镜界 buff
    mirrorBuffID: list[int] = Field(default_factory=list)
    #: 战旗（``BattleFlagsClass`` 未建模 → 原始字节）
    flag: list[bytes] = Field(default_factory=list)
    #: 本场前后的段位 ID
    beforeClassID: int = 0
    afterClassID: int = 0
    #: 本场积分变化（正负都有）
    pointChange: int = 0
    #: 连胜额外积分
    winningStreakPoint: int = 0


class TopPvpChallengeRes(ProtoMessage):
    """挑战响应（消息号 39008）——**服务器算完整场战斗后把结果放在这里**。"""

    proto_class_name = "TopPvpChallengeRes"

    #: 业务错误码（0 = 成功；-1 = 次数不足，见 :data:`TOP_PVP_ERROR_NO_CHALLENGE_TIMES`）
    errorCode: int = 0
    #: 本场获得的东西
    gotList: GetObjClass | None = None
    #: 其它奖励（连胜/段位奖励等）
    rewardList: list[GetObjClass] = Field(default_factory=list)
    #: 我方对局信息（积分变化看这里）
    versusInfo_Me: TopPvpVersusInfoClass | None = None
    #: 对方对局信息（注意协议里字段名是小写 i 的 ``versusinfo_Enemy``，别写错）
    versusinfo_Enemy: TopPvpVersusInfoClass | None = None
    #: ★ 回放/战报 JSON（实测 46,032 字节）。
    #:
    #: .. warning::
    #:    它很大，**绝不要整段打进日志**。只取 :meth:`battle_summary` 里那几个键。
    replayResult: str = ""

    def is_success(self) -> bool:
        """业务是否成功。"""
        return self.errorCode == TOP_PVP_SUCCESS

    def battle_summary(self) -> dict[str, Any]:
        """从战报 JSON 里取**少量**关键键（胜负、耗时、步数、回放文件名）。

        【为什么只取几个键，而不是把 JSON 整个存下来】
        单个战报 46KB，一天几十场就是几 MB；日志里塞满战报会让"看日志"这件事失效，
        而每天真正要看的只有"这局赢了还是输了"。需要更详细内容时再按需扩展。

        :return: 形如 ``{"winOrFail": True, "elapsed": 1.0, "step": 418,
                 "replay_file_name": "..."}``；解析失败时返回 ``{}``
                 （**不抛异常**：战报只是展示信息，不该因为它解不动就把整场判成失败）。
        """
        if not self.replayResult:
            return {}
        try:
            parsed = json.loads(self.replayResult)
        except (ValueError, TypeError) as exc:  # 展示信息不该影响主流程
            _LOGGER.warning("战报 JSON 解析失败（不影响结果判定）：%s", exc)
            return {}
        if not isinstance(parsed, dict):
            return {}
        picked: dict[str, Any] = {}
        for key in ("winOrFail", "elapsed", "step", "isNpc", "error", "replay_file_name"):
            if key in parsed:
                picked[key] = parsed[key]
        return picked

    def point_change(self) -> int:
        """本场积分变化（拿不到返回 0）。"""
        info = self.versusInfo_Me
        return 0 if info is None else info.pointChange

    def reward_summary(self) -> str:
        """把 ``gotList`` / ``rewardList`` 里的道具汇总成一行（**仅开发者日志用**）。

        .. warning::
           39008 的数量口径**尚未实证** —— 同源的 6006 / 11010 / 37020 都已证实
           回的是"操作后持有量"（Q-021）。所以本方法的输出只允许进 DEBUG 日志，
           **不得**拼进用户可见文案或 INFO 级摘要（见 :meth:`summary`）。
        """
        groups: list[GetObjClass] = []
        if self.gotList is not None:
            groups.append(self.gotList)
        groups.extend(self.rewardList)
        items: list[str] = []
        for group in groups:
            items.extend(f"{item.itemID}×{item.itemAmount}" for item in group.itemList)
        return "道具 " + "、".join(items) if items else "<无道具>"

    def summary(self) -> str:
        """一行摘要（日志用，**不含**战报正文）。

        【★ 2026-10-04】这里**不再拼** ``奖励={reward_summary()}``：
        那会把裸 ``itemID``（例如 ``200000050×36610000``）直接送进 INFO 级日志，
        既让用户看不懂，又把一个口径未实证的数字当"获得"展示。
        逐件明细请单独调 :meth:`reward_summary` 并只写进 DEBUG 日志。
        """
        battle = self.battle_summary()
        outcome = battle.get("winOrFail")
        outcome_text = "胜" if outcome is True else "负" if outcome is False else "未知"
        return (
            f"errorCode={self.errorCode}（{describe_error_code(self.errorCode)}）"
            f"，本场={outcome_text}，积分变化={self.point_change():+d}"
            f"，step={battle.get('step', '-')}，elapsed={battle.get('elapsed', '-')}"
        )


class ReceiveTopPvpRewardPacket(ProtoMessage):
    """荣耀之巅宝箱领取请求（消息号 39003）。空包。"""

    proto_class_name = "ReceiveTopPvpRewardPacket"


class ReceiveTopPvpRewardRes(ProtoMessage):
    """荣耀之巅宝箱领取响应（消息号 39004）。"""

    proto_class_name = "ReceiveTopPvpRewardRes"

    #: 业务错误码（0 = 成功）
    errorCode: int = 0
    #: 获得道具列表（GetObjClass 原始字节列表）
    addList: list[bytes] = Field(default_factory=list)

    def is_success(self) -> bool:
        """是否成功。"""
        return self.errorCode == 0


# ---------------------------------------------------------------------------
# 出站消息号白名单 / 禁发清单（任务层与测试共同使用）
# ---------------------------------------------------------------------------
#: 本板块允许发出的消息号（由 ``tasks/top_pvp.py`` 在**每次发包前**强制校验）。
#:
#: 【为什么把白名单写在模型层，而不是任务层】
#: 它是"这个系统允许用哪些包"的**协议事实**（39001 只读 / 39019 开关 / 39007 挑战 / 39003 宝箱），
#: 与任务逻辑无关。放在模型层，任务层、测试、将来任何调用方拿到的是同一份定义，
#: 不会出现"某处少列了一个包、于是把次数买掉"这种事。
#:
#: ⚠️ 故意**不含** 39005（买次数）—— 本阶段严格禁止花钻石买次数。
ALLOWED_PACKET_IDS: Final[dict[str, int]] = {
    "GetTopPvpHomePacket": 39001,
    "TopPvpSetSkipBattleTogglePacket": 39019,
    "TopTeamEditInfoPacket": 39011,
    "TopPvpChallengePacket": 39007,
    "ReceiveTopPvpRewardPacket": 39003,
}

#: 明确禁止发出的消息号（写进回归测试，防止将来有人"顺手"加上）。
FORBIDDEN_PACKET_IDS: Final[dict[str, int]] = {
    "TopPvpBuyChallengeCountPacket": 39005,  # 花钻石买挑战次数 ← 本项目的红线
    "SetTopPvpDefTeamPacket": 39009,         # 改防守队伍（会改动账号配置）
    "TopPvpSeasonInfoPacket": 39013,         # 赛季信息（本阶段不需要）
    "TopPvpReplayDetialPacket": 39015,       # 拉回放详情（本阶段不需要）
    "TopPvpGetProvePacket": 39017,           # 领"荣耀证明"（本阶段不需要）
}

#: 消息号 → 用途说明（``--dry-run`` 演练与日志会打印，便于一眼看懂"这包干嘛的"）。
PACKET_PURPOSE: Final[dict[int, str]] = {
    39001: "拉主页（只读：段位/排名、已购次数、跳过战斗演出开关状态）",
    39019: "写入「跳过战斗演出」= 开启（服务端持久化）",
    39011: "拉取该赛区的队伍编辑信息（**挑战前必发**，响应含当前防守队伍）",
    39007: "发起一场挑战（**消耗一次次数**，战斗由服务器结算并回战报）",
    39003: "领取荣耀之巅宝箱奖励（0 消耗）",
}


