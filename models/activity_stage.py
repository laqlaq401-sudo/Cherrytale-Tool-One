"""降临／活动关卡（SpecialArea / SpecialSection）报文：11025/11026、11003/11004、11009/11010。

【这个系统是什么】
主界面「降臨活動 / 常駐降臨」标签（``SpecialAreaTagData`` 的 tagID ``901000002`` /
``901000009``）下的一批**限时活动区域**：每个区域（``SpecialAreaData``，如
``128110053``「可疑的工作」）含若干关卡（``SpecialSectionData``，如 ``129110531``~``129110539``）。
区域**按周轮换**（实测 ``startTime`` / ``endTime`` 是毫秒时间戳、相差 14 天；
⚠️ ``weeks`` 字段实测为 ``-1``，不能当"周数"用），
所以每期开放的 ``areaID`` / ``sectionID`` 都不同 —— 详见 ``notes/sweep_capture.md``。

【为什么本板块能纯协议自动化（不需要战斗引擎）】
``11009 SweepSectionPacket`` 是**服务端结算**：整份抓包
（``captures/降临扫荡抓包1.saz``，29 MB）里只有 **4 个网关 POST**
（``11025 / 11003 / 11009 / 11009``），其余 6 个 HTTP 请求全是 CDN 图片；
进入战斗、结算、战报、逐施法存证**一个都没有**。
配置表也印证：``SpecialSectionData[129110539].cheatCheck = 0``（该关不开反外挂校验）。

【本模块刻意**不建模**的包（同 ``models/top_pvp.py`` 的哲学）】
===============================  ==================================================
11027 ``BuyActivityStagePacket``  买关卡次数（花钻石）
11005 ``ReceiveMapBonusPacket``   领地图奖励（额外收益，不在"扫荡"需求内）
11007 ``ReceiveStarRewardPacket`` 领星奖励
11011 ``GetLastSectionPacket``    关卡跳转（非扫荡流程）
===============================  ==================================================

让"花钱 / 白拿奖励"在**类型层面**就不存在：这样即使将来有人写错分支，
也不存在一个模型能把这些包发出去（白名单见 :data:`ALLOWED_PACKET_IDS`）。

【★ 两个必须记住的实测细节】
1. ``11010`` 的 ``rewardList`` / ``costList`` 里的数量是「**操作后的持有量**」，
   不是"本次获得/消耗了多少"（推导见 ``notes/sweep_capture.md`` 第四节）；
2. ``times`` 由**客户端**按体力削出来：用户点 5 次、体力只够 4 次时，包里就是 ``times=4``。
   所以 ``times`` 不是"用户意图"，而是"客户端算完的结果" —— 我方实现必须在发之前自己算。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Final

from pydantic import Field

from models.const_ids import int_const
from models.daily import GetObjClass, ItemClass, SUCCESS_ERROR_CODE
from models.game_packet import ProtoMessage
from models.packet_ids import packet_id

_LOGGER: Final[logging.Logger] = logging.getLogger("models.activity_stage")

# ---------------------------------------------------------------------------
# 常量与白名单
# ---------------------------------------------------------------------------
#: 业务成功码（与其它模块共用同一个 0，见 :data:`models.daily.SUCCESS_ERROR_CODE`）
ACTIVITY_STAGE_SUCCESS: Final[int] = SUCCESS_ERROR_CODE

#: 体力道具 ID（``RPS_Const.ENERGY_ID``）。实测 ``11010.costList`` 里的第一件就是它。
ENERGY_ITEM_ID: Final[int] = int_const("ENERGY_ID")

#: 扫荡券道具 ID（``RPS_Const.SWEEPTICKET_ID``）。实测每扫荡 1 次消耗 1 张。
SWEEP_TICKET_ITEM_ID: Final[int] = int_const("SWEEPTICKET_ID")

#: 客户端「扫荡档位」（``StageModule.eSweepType``，dump.cs **L530313**）：**1 / 5 / 10** 三档。
#:
#: 【这就是 Q-018 的答案】
#: 真实客户端的扫荡面板**没有**任意次数输入，只有这三档；用户实测的"5 次"就是
#: ``eSweepType.five``。而 ``eSweepErrorCode.VipNotEnough = -9`` 说明部分档位有
#: VIP 门槛（门槛具体值未定，且不再影响本项目 —— 我们只发档位内的次数，
#: 再按剩余体力削峰，与实测客户端行为一致）。
SWEEP_TIME_CHOICES: Final[tuple[int, ...]] = (1, 5, 10)

#: UI 哨兵值：**「扫荡到清空体力 / 次数耗尽」**。
#:
#: - 活动关（降临）：语义 = 反复发「扫荡 5 次」直到**体力清空**（服务端回体力不足）；
#: - 素材关：语义 = 反复发「扫荡 5 次」直到**次数耗尽**（服务端回 ``TimesIsFull = -7``）。
#:
#: 【为什么是 -1】``0`` 已被占用为"只读（什么都不扫）"，必须区分；负数天然不会
#: 和任何合法次数冲突。
#:
#: 【为什么**不放进** :data:`SWEEP_TIME_CHOICES`】那个元组是"线上真正会发出的
#: ``times`` 取值"。哨兵只是**界面概念**：真正发包时每一轮发的是 ``5``
#: （或被体力削出的其它值），**绝不会把 -1 发上网**。回归测试
#: ``req.times in SWEEP_TIME_CHOICES`` 因此继续有效，还能兜住"哨兵被误发"。
#:
#: .. warning::
#:    :func:`normalize_sweep_times` 会把 ``<= 0`` 一律当成"不扫"（返回 ``0``），
#:    所以调用方**必须**在归一之前先判断哨兵，否则 -1 会被**静默吞成"只读"**。
SWEEP_UNTIL_EMPTY: Final[int] = -1


def normalize_sweep_times(requested: int) -> tuple[int, str]:
    """把用户要的次数**向下归到客户端档位**。

    :param requested: 用户要求的次数（``<= 0`` 表示"不扫"）。
    :return: ``(档位次数, 说明)``；发生归一或截断时"说明"非空（要打印给用户看）。

    【为什么"向下取"而不是"就近取"】
    多扫一次就多扣一份体力与扫荡券，而且发出一个**真实客户端永远不会发**的数字
    并没有好处（服务端可能接受，但那就脱离了"镜像真实客户端"这条基线）。
    向下取保证"最多花用户说过的量"。
    """
    if requested <= 0:
        return 0, ""
    top = SWEEP_TIME_CHOICES[-1]
    if requested > top:
        return top, f"客户端档位最高 {top} 次（eSweepType），已从 {requested} 收到 {top}"
    for choice in reversed(SWEEP_TIME_CHOICES):
        if requested >= choice:
            if requested == choice:
                return choice, ""
            return choice, (
                f"客户端档位只有 {list(SWEEP_TIME_CHOICES)} 三档（eSweepType），"
                f"已把 {requested} 向下归到 {choice}"
            )
    return 0, ""

#: 本板块允许发出的报文（``tasks/sweep.py`` 用它做白名单）。
#: 扫荡主要使用 11025 / 11003 / 11009；若允许首次通关简单关卡，还可使用 11015 / 11017。
#:
#: ★ 2026-10-02：新增 **2007 ``GetAllItemPacket``**（只读拉背包）。
#: 原因：扫荡每 1 次固定消耗 1 张扫荡券（``200000016``），而"我到底有几张券"
#: 只能从背包里读。**没有这张只读包，就只能在被服务端拒绝（``NoTicket = -3``）
#: 之后才知道券不够** —— 那时玩家已经为这次尝试付出了风控暴露。
#: 它是纯只读包（模型见 ``models/wallet.py``，那边同样只建模 2007/2008），
#: 不消耗任何资源，也不改背包。
ALLOWED_PACKET_NAMES: Final[tuple[str, ...]] = (
    "GetAllItemPacket",  # 2007 只读拉背包（数扫荡券）
    "GetAllActivityStage",  # 11025 当期活动区域
    "GetSectionByAreaPacket",  # 11003 区域关卡状态
    "SweepSectionPacket",  # 11009 扫荡（扣体力 + 扫荡券）
    "FightingRequestPacket",  # 11015 首通握手
    "FightingResultPacket",  # 11017 首通结算
)

#: ``{类名: 消息号}``（供 ``tasks/packet_guard.guarded_send`` 使用）
ALLOWED_PACKET_IDS: Final[dict[str, int]] = {
    name: packet_id(name) for name in ALLOWED_PACKET_NAMES
}

#: 明确**禁发**的报文：命中即抛错，绝不发送。
FORBIDDEN_PACKET_NAMES: Final[tuple[str, ...]] = (
    "BuyActivityStagePacket",  # 11027 买关卡次数（花钻石）
    "ReceiveMapBonusPacket",  # 11005 领地图奖励（额外收益）
    "ReceiveStarRewardPacket",  # 11007 领星奖励
    "GetSectionLimitItemAmount",  # 11023 限次道具数量（本板块用不到，保持白名单最小）
)

#: ``{类名: 消息号}``
FORBIDDEN_PACKET_IDS: Final[dict[str, int]] = {
    name: packet_id(name) for name in FORBIDDEN_PACKET_NAMES
}

#: ``{消息号: 一句话说明}``（写进 DEBUG 日志，便于一眼看懂在发什么）
PACKET_PURPOSE: Final[dict[int, str]] = {
    2007: "拉取背包全量列表（只读；只用来数扫荡券够不够扫）",
    11025: "拉取当期开放的活动区域列表（只读，请求体为空）",
    11003: "拉取指定区域的关卡状态列表（只读）",
    11009: "扫荡一个关卡（**消耗体力 + 扫荡券**，服务端结算）",
    11015: "申请进入活动关卡战斗（首通简单关卡，获得 fightToken）",
    11017: "上报活动关卡战斗胜利与三星结果（首通简单关卡）",
}

#: ``11010`` 的 ``errorCode`` 说明。
#:
#: 【来源】客户端枚举 ``StageModule.eSweepErrorCode``（dump.cs **L530264**，
#: TypeDefIndex 11663）—— 逐条抄下来，**不是猜的**。
#: 其中只有 ``0`` 经可靠实测（抓包两次响应都是 0）；其余取值尚未逐个在真机上触发过，
#: 所以这里只翻译"是什么"，不做任何自动处置（一律停止并向用户原样上报）。
ERROR_NOTES: Final[dict[int, str]] = {
    0: "成功",
    -1: "未通关（NoClearance）—— 正是「只扫已通关关卡」护栏要挡的那一类",
    -2: "体力不足（NoEnergy）",
    -3: "扫荡券不足（NoTicket）",
    -4: "钻石不足（NoDreamStone；扫荡本身不该花钻石）",
    -6: "数据错误（DataError）",
    -7: "次数已满（TimesIsFull，例如当日/活动次数上限）",
    -8: "该活动今日关闭（DailyClose）",
    -9: "VIP 等级不足（VipNotEnough）—— 客户端的某些扫荡档位有 VIP 门槛",
    -10: "没有月卡（NoAnymonthCard）",
    -11: "该关卡未开放扫荡（NoOpenForSweep）",
}


def describe_error_code(error_code: int) -> str:
    """把 ``11010.errorCode`` 翻译成一行说明。

    :return: 已知码返回中文说明（来源见 :data:`ERROR_NOTES`）；
        未知码原样报出并提示登记 —— 服务端比客户端新是常态，不该因此崩掉。
    """
    known = ERROR_NOTES.get(error_code)
    if known is not None:
        return known
    return (
        f"未知错误码 {error_code}"
        "（客户端枚举里没有它，服务端可能比客户端新 —— "
        "请连同当时的分支反馈到 notes/open_questions.md）"
    )


# ---------------------------------------------------------------------------
# 时间：实测是**毫秒** Unix 时间戳，且 -1（无效）以无符号 2^64-1 出现
# ---------------------------------------------------------------------------
#: ``-1`` 以 ``long`` 无符号形式打印出来的样子（实测 ``permanentAreaStateList``
#: 里的 ``startTime`` / ``endTime`` 就是它）。
UNSET_TIMESTAMP: Final[int] = (1 << 64) - 1

#: 展示用的时区（游戏是台服，固定 +8；仅用于打印，不参与任何判断）
DISPLAY_TZ: Final[timezone] = timezone(timedelta(hours=8))


def timestamp_to_text(ms: int) -> str:
    """把毫秒时间戳转成 ``2026-09-23 21:00`` 这样的文本。

    :param ms: 毫秒时间戳；``0`` / ``-1`` / :data:`UNSET_TIMESTAMP` 视为"未设置"。

    .. note::
       时间戳只用于**打印**（"这期活动还剩几天"），绝不据此做安全判断 ——
       本地时钟不可信，真正的可玩与否以服务端响应为准。
    """
    if ms in (0, -1, UNSET_TIMESTAMP):
        return "<未设置>"
    return datetime.fromtimestamp(ms / 1000, tz=DISPLAY_TZ).strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# 11026：区域状态（areaStateList / memoryAreaStateList / permanentAreaStateList 的元素）
# ---------------------------------------------------------------------------
class ActivityAreaStateClass(ProtoMessage):
    """一个活动区域的状态（``11026`` 三个列表的元素，**14 个字段**）。

    【三个列表的区别（实测）】
    ==========================  =====  ============================================
    ``areaStateList``            16 条   **当期开放**的限时活动区（带起止时间）
    ``memoryAreaStateList``      28 条   回忆区（``isMemoryUnlock`` 相关）
    ``permanentAreaStateList``   37 条   常驻区（时间字段是 ``-1``）
    ==========================  =====  ============================================

    本次抓包扫荡的区域 ``128110053`` 就在 ``areaStateList`` 的第 5 项 ——
    所以"这期有哪些降临关卡"完全可以从这一个响应里读出来，不必抓包。
    """

    proto_class_name = "ActivityAreaStateClass"

    #: 区域 ID（对应配置表 ``SpecialAreaData.areaID``，如 128110053）
    areaID: int = 0
    #: 已通关关卡数。⚠️ 与 ``areaStars`` 不是一回事（实测 15 vs 0），
    #: 语义未逐个确认，本类只原样保存。
    passedCount: int = 0
    #: 额外挑战币（``ItemClass``）；未发放时为空
    extraChallengeCoin: ItemClass | None = None
    #: 列表横幅资源名（如 ``Level_Activity_Banner_DARK_Por…``）
    banner: str = ""
    #: 活动开始时间（毫秒时间戳；常驻区为 ``-1`` → :data:`UNSET_TIMESTAMP`）
    startTime: int = 0
    #: 活动结束时间（毫秒时间戳，规则同上）
    endTime: int = 0
    #: 详情页横幅资源名
    bannerCenter: str = ""
    #: 该区已获得的星数
    areaStars: int = 0
    #: 该区已获得的星数（⚠️ **实测是 repeated**：raw/02 里出现 3 次 ``18 01``，
    #: 所以声明成 ``list[int]``，不是单个 int。dump.cs 里它是 ``List<int> receiveStarReward``）
    receiveStarReward: list[int] = Field(default_factory=list)
    #: 活动持续周数（实测 ``2``）
    weeks: int = 0
    #: 区状态（``0`` / ``1`` …，**语义未确认**，只做展示）
    sectionState: int = 0
    #: 进度文本（实测形如 ``"5/5"`` —— 服务端直接给好的展示串）
    progressStr: str = ""
    #: 回忆区是否解锁
    isMemoryUnlock: bool = False
    #: 回忆关卡是否已完成（仅回忆区有意义）
    memoryStoreFinish: bool = False

    # ------------------------------------------------------------------
    # 展示辅助
    # ------------------------------------------------------------------
    @property
    def start_text(self) -> str:
        """开始时间（``2026-09-23 21:00``；常驻区是 ``<未设置>``）。"""
        return timestamp_to_text(self.startTime)

    @property
    def end_text(self) -> str:
        """结束时间（同上）。"""
        return timestamp_to_text(self.endTime)

    def describe(self, area_name: str = "") -> str:
        """一行摘要（任务列表里打印用）。

        :param area_name: 从配置表查到的区域名（``SpecialAreaData.areaName``）；
            没查到就留空，此时只显示 ID。
        """
        label = f"{self.areaID} {area_name}".strip()
        return (
            f"{label}｜进度 {self.progressStr or '?'}｜"
            f"{self.start_text} → {self.end_text}｜{self.weeks} 周"
        )


# ---------------------------------------------------------------------------
# 11004：关卡状态（sectionStateList 的元素）
# ---------------------------------------------------------------------------
class SectionStateClass(ProtoMessage):
    """一个关卡的状态（``11004`` 的元素，**12 个字段**）。"""

    proto_class_name = "SectionStateClass"

    #: 关卡 ID（对应 ``SpecialSectionData.sectionID``，如 129110539）
    sectionID: int = 0
    #: 关卡状态。实测取值（2026-09-21 真机 + 只读实验，见 Q-019）：
    #:
    #: - ``0`` = **未通关**（未通关区 128210047 的关卡就是这个值）；
    #: - ``2`` = **已通关**（已通关区 128110053 的 9 关全是它）。
    #:
    #: 目前只见过这两个取值，所以"是否已通关"的判断写成**白名单式**
    #: （只有 ``== 2`` 才算），实现在 ``tasks/sweep.py::PASSED_SECTION_STATE`` ——
    #: 本模型只如实保存，不在这一层下结论。
    sectionState: int = 0
    #: 已获得星数（⚠️ **实测是 repeated**：raw/03 里 ``18 01`` 出现 3 次 →
    #: dump.cs 是 ``List<int> starCount``）
    starCount: list[int] = Field(default_factory=list)
    #: 限次道具已用次数（⚠️ 同样是 repeated：``List<int> limitItemCount``）
    limitItemCount: list[int] = Field(default_factory=list)
    #: 地图奖励状态
    mapBonusState: int = 0
    #: 彩蛋状态
    colorEgg: int = 0
    #: 额外刷新次数
    extraRefreshCount: int = 0
    #: 额外刷新消耗（``ItemClass``）
    extraRefreshCoin: ItemClass | None = None
    #: 该关卡在周几开放（``-1`` = 每天都开）
    weekDay: int = 0
    #: HCG 奖励状态
    hcgBonusState: int = 0
    #: 满星奖励状态
    fullStarBonusState: int = 0
    #: 关卡配布 ID（对应 ``StageDistributeData.stageDistributeID``）
    stageDistributeID: int = 0


# ---------------------------------------------------------------------------
# 11025 / 11026：活动区域总表（**空请求**）
# ---------------------------------------------------------------------------
class GetAllActivityStage(ProtoMessage):
    """拉取活动区域总表（消息号 11025）。

    .. note::
       空报文：实测**请求体 0 字节**，服务端靠 ``RootPacket.packetID`` 识别意图。
       只读，不消耗任何资源 —— 所以每次运行都可以先发它，用来发现"这期有哪些区"。
    """

    proto_class_name = "GetAllActivityStage"


class GetAllActivityStageRes(ProtoMessage):
    """活动区域总表（消息号 11026）。"""

    proto_class_name = "GetAllActivityStageRes"

    #: 当期开放的限时活动区（**降临/活动关卡的入口**，实测 16 条）
    areaStateList: list[ActivityAreaStateClass] = Field(default_factory=list)
    #: 回忆区（实测 28 条）
    memoryAreaStateList: list[ActivityAreaStateClass] = Field(default_factory=list)
    #: 常驻区（实测 37 条）
    permanentAreaStateList: list[ActivityAreaStateClass] = Field(default_factory=list)

    def find_area(self, area_id: int) -> ActivityAreaStateClass | None:
        """在三个列表里按 ``areaID`` 找区域（找不到返回 ``None``）。"""
        for items in (
            self.areaStateList,
            self.memoryAreaStateList,
            self.permanentAreaStateList,
        ):
            for area in items:
                if area.areaID == area_id:
                    return area
        return None

    def all_areas(self) -> list[ActivityAreaStateClass]:
        """三个列表拼起来的全部区域（areaStateList → memory → permanent）。"""
        return [
            *self.areaStateList,
            *self.memoryAreaStateList,
            *self.permanentAreaStateList,
        ]


# ---------------------------------------------------------------------------
# 11003 / 11004：某一区域的关卡列表（**关卡轮换的落点**）
# ---------------------------------------------------------------------------
class GetSectionByAreaPacket(ProtoMessage):
    """拉取指定区域的关卡状态列表（消息号 11003）。只读。"""

    proto_class_name = "GetSectionByAreaPacket"

    #: 区域 ID（来自 ``11026`` 的 ``areaStateList``，实测 128110053）
    areaID: int = 0


class GetSectionByAreaRes(ProtoMessage):
    """指定区域的关卡状态列表（消息号 11004）。"""

    proto_class_name = "GetSectionByAreaRes"

    #: 该区域的关卡（实测 9 条：129110531 ~ 129110539）
    sectionStateList: list[SectionStateClass] = Field(default_factory=list)

    def section_ids(self) -> list[int]:
        """按服务端返回的顺序列出 ``sectionID``（扫荡的候选集合）。"""
        return [section.sectionID for section in self.sectionStateList]


# ---------------------------------------------------------------------------
# 11009 / 11010：扫荡本体（**本项目里唯一"主动花资源打关卡"的包**）
# ---------------------------------------------------------------------------
class SweepSectionPacket(ProtoMessage):
    """扫荡一个关卡（消息号 11009）。

    .. warning::
       这一发**会真的扣掉**体力与扫荡券（实测：每次 = ``energyCost`` 体力 + 1 张扫荡券）。
       ``times`` 必须是**我们自己算过**的数字 —— 实测客户端就是把用户点的"5 次"
       削成体力可支撑的 4 次才发出的（见 ``notes/sweep_capture.md`` 第五节）。
    """

    proto_class_name = "SweepSectionPacket"

    #: 关卡 ID（来自 ``11004``，如 129110539）
    sectionID: int = 0
    #: 扫荡次数（**由客户端算好的**，服务端按它结算）
    times: int = 0
    #: 是否使用扫荡券（实测客户端固定发 ``1``）
    useSweepTicket: int = 0


class SweepSectionRes(ProtoMessage):
    """扫荡响应（消息号 11010）。

    【★ 怎么读这两个列表（最容易搞错的地方）】
    列表里每件 ``ItemClass.itemAmount`` 是**操作后的持有量**，不是"本次变动量"：
    实测 4 次扫荡后体力 ``0``、扫荡券 ``889``（前一发是 893，差 4 = 次数），
    而活动币逐次递增 ``1450 → 1850 → 2250 → 2650``（每次 +400）。所以：

    - 想知道"还剩多少体力/券" → 直接读 :attr:`energy_left` / :attr:`sweep_ticket_left`；
    - 想展示"这次拿到了什么" → 用 :meth:`settlement_items`，
      **不要**把多个元素累加（会把同一件东西算好几遍）。
    """

    proto_class_name = "SweepSectionRes"

    #: 业务错误码（0 = 成功；其余取值未实测，见 :func:`describe_error_code`）
    errorCode: int = 0
    #: 逐次扫荡的结算明细（实测：非空元素数 = ``times``，末尾还会多一个空元素占位）
    rewardList: list[GetObjClass] = Field(default_factory=list)
    #: 消耗后的持有量（实测第一件是体力、第二件是扫荡券）
    costList: list[GetObjClass] = Field(default_factory=list)

    # ------------------------------------------------------------------
    # 读结果
    # ------------------------------------------------------------------
    @property
    def is_success(self) -> bool:
        """是否业务成功（判断依据见 :data:`ACTIVITY_STAGE_SUCCESS`）。"""
        return self.errorCode == ACTIVITY_STAGE_SUCCESS

    def cost_amount(self, item_id: int) -> int | None:
        """``costList`` 里某个道具的**当前持有量**（没有该项时返回 ``None``）。

        :param item_id: 道具 ID，例如 :data:`ENERGY_ITEM_ID` / :data:`SWEEP_TICKET_ITEM_ID`。
        """
        for obj in self.costList:
            for item in obj.itemList:
                if item.itemID == item_id:
                    return item.itemAmount
        return None

    @property
    def energy_left(self) -> int | None:
        """扫荡后的**剩余体力**（响应里没有该道具时返回 ``None``）。"""
        return self.cost_amount(ENERGY_ITEM_ID)

    @property
    def sweep_ticket_left(self) -> int | None:
        """扫荡后的**剩余扫荡券**（响应里没有该道具时返回 ``None``）。"""
        return self.cost_amount(SWEEP_TICKET_ITEM_ID)

    def settlement_items(self) -> list[ItemClass]:
        """最后一次扫荡的结算明细。

        :return: ``rewardList`` 里**最后一个非空元素**的 ``itemList``；全空则返回空列表。
        """
        for obj in reversed(self.rewardList):
            if obj.itemList:
                return list(obj.itemList)
        return []

    def reward_summary(self) -> str:
        """一行奖励摘要（**仅开发者日志（DEBUG）用**）。

        .. warning::
           ``rewardList[].itemAmount`` 是**扫荡后的持有量**，不是本次掉落量
           （Q-021 真机结案，见本类文档与 ``notes/sweep_capture.md`` 第九节）——
           所以这里的前缀刻意写成"持有"，**不是**"获得"。

           禁止把本方法的输出放进用户可见文案；运行视图要展示奖励时，
           请走 :func:`models.reward.reward_phrase` 并显式声明 ``QuantityScope``。
        """
        items = self.settlement_items()
        if not items:
            return "<无奖励明细>"
        return "持有 " + "、".join(f"{item.itemID}×{item.itemAmount}" for item in items)

    def cost_summary(self) -> str:
        """一行剩余量摘要（日志用）。

        为什么打印"剩余"而不是"消耗"：服务端回传的就是持有量（见类文档）——
        写成"消耗"会让对账得出错误结论。
        """
        parts: list[str] = []
        energy = self.energy_left
        if energy is not None:
            parts.append(f"体力剩 {energy}")
        ticket = self.sweep_ticket_left
        if ticket is not None:
            parts.append(f"扫荡券剩 {ticket}")
        return "；".join(parts) if parts else "<无消耗明细>"
