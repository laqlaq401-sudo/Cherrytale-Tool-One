"""宴席（BBQ）：两步、两个开关（消息号 32007 → 32008，15003 → 15004）。

【两条入口：默认（整场）+ 指定顿 ``meal``（早/晚）—— 2026-10-05 新增 / 10-06 修正】
宴席一天分**两顿**，各自只能吃一次，且各自的**邀请**只在各自的时间段里有效：
早宴席 **06:10–13:59**、晚宴席 **14:00–次日 06:00**（毫秒时间戳，实测见
``notes/bbq_second_meal_plan.md``）。因此前端把「执行」拆成**早宴席 / 晚宴席两个按钮**，
它们通过构造参数 ``meal``（``"morning"`` / ``"evening"``）落到本任务：

- **不给 ``meal``**（CLI、旧调用方、既有自动化）→ :meth:`BBQEnergyTask.execute`
  三条分支**逐字节不变**，行为与改造前完全一致；
- **给了 ``meal``** → 走 :meth:`BBQEnergyTask._execute_meal`：
  空档期（06:00–06:10）两顿都**不发包**；晚宴席仍**只在窗口内**能玩；
  **早宴席不看窗口** —— 只要本宴席日没吃过就能吃（窗口只用来限制**邀请**）。
  成功吃到后写一条"本顿已吃"的服务端记账（``services.banquet_state``）。

【★ 2026-10-06 第七版修正：窗口只管"邀请"，不管"吃"】
第五版曾判断"不存在补吃"，**这个判断是错的**。抓包实证（10-03 凌晨两份 saz）：
00:39 与 00:42 两次 ``15003{501000004, 无好友}`` 都发在该期窗口（06:10–13:59）**之外**，
服务端照样 ``errorCode=0``；而那份名为"过时间无法用钻石邀请的宴席"的抓包里
**一条付费邀请包都没有** —— 过点之后只剩"免费吃"这一条路。
原因：**宴席日是 06:10 → 次日 06:10**（其中 06:00–06:10 为空档期，两顿都不可吃），
早宴席在自己窗口结束之后仍处在**同一个宴席日**内，所以还能吃（补吃）。

【为什么"窗不覆盖"要优先于"邀请开关开着"】
用户要的是"在某一顿的时间之外，那一顿的邀请就默认不邀请"。
如果把开关放在窗口之前，窗口外的点击仍会真的发出 15003 邀请 —— 服务端会拒，
但那是**我们主动制造的一次无效请求**。故窗口判定（对**邀请**而言）不可绕过：
前端把开关直接置灰，后端再压一道闸门。


【本模块的定位：**用户主动选择的开关模块**】
宴席**不再属于日常清理流程**（已从 ``tasks/daily.py`` 的 ``steps`` 里移除）。
原因很直接：它的一次执行可能真的花掉钻石，必须由人当场决定 ——
混在"每天自动跑一遍"的流程里，一旦开关被误留成开启状态，损失不可逆。

【两步 —— 理解本模块的关键】

    ① 是否**邀请好友**来吃宴席    ← 花钻石的关键步骤（只走最高档，实测 31 钻）
    ② 是否**确认吃宴席**          ← 免费，**与消费闸门完全无关**

协议上只有一个动作包 ``15003 BBQFriendsPacket{groupID, bbqFriendList}``
（客户端调用链 ``BBQNetWorkModule.RequestBBQFriendsPacket(BBQ_Activity)``），
所以"两步"落在代码里就是：

    步骤① = 组包时决定 ``groupID``（用哪一档）与 ``bbqFriendList``（请谁）
    步骤② = 到底发不发这个包

【两个开关：总闸 + 用途开关（与关系）】

=====================================  ==============================================
``--eat``（任务参数 ``eat=True``）       步骤②：真的发 15003（免费档也靠它）
总闸 ``--allow-diamond``               全局：没开就一律不许花钻石
用途开关 ``--invite-friends``            步骤①：本任务是否允许花钻石请客
=====================================  ==============================================

- **只开 ``--eat``** → 走免费档（``itemAmount=0``）、好友列表为空 → **零钻石**；
- **``--invite-friends`` 隐含 ``--eat``**：命令行层会替你把 ``eat`` 补上并在日志里明说
  ——"开了邀请却不吃"没有任何意义。反过来，配置里长期开着
  ``BBQ_INVITE_FRIENDS`` 而命令行不给任何参数时**不会**自动吃：多留一道手动的门。
- 两个钻石开关都不开时，任务**不是失败**，而是只读跑完：列出档位、好友数与各自状态。
  这正是"开关模块"该有的样子（改造前会在任务开始前整体报错，
  那种设计只适合"整条流程都会花钱"的任务）。

【档位：只要最高的那一档（但档位**不进包**）】
实测 ``ExtraEnergyData`` 五档（``menuID|number|itemAmount``）::

    500000001|1|0      500000002|2|7      500000003|3|15
    500000004|4|24     500000005|5|31

实际游戏里几乎不会有人去点小档位，所以本项目**只走最大档**：
邀请人数 ``N = min(可邀请好友数, config.BBQ_MAX_INVITE_FRIENDS)``，
档位取 ``number == N`` 那一行（查不到就退到 ``number ≤ N`` 的最大档）。
好友不足 5 人时自然退到小档，不会出现"请 3 个人却按 5 人付钱"。

【⚠️ 真机实测（2026-09-21，#113）：``groupID`` 与 ``menuID`` **不是同一个编号**】
``32008`` 返回的活动期间是 ``groupID=[501000004, 501000005]``，而配置表 ``menuID``
是 ``500000001~5`` —— 两者前缀都不同。因此：

- ``15003.groupID`` **必须取自 32008**（见 :meth:`BBQEnergyTask._current_group_id`）；
- 配置表档位只用于**估算"请 N 位好友要花多少钻"**（也就是闸门里的金额），
  **不参与组包**；实际扣费由服务端按 ``bbqFriendList`` 决定。

``config.BBQ_MENU_ID`` 仍保留，但已降级为**逃生门**：显式指定后不再按人数挑档，
而是用你指定的这一档（金额仍从配置表读，读不到就拒绝）。它只影响费用估算与闸门金额。

【还没实测确认的两件事（登记在 notes/open_questions.md）】
1. ``ExtraEnergyData.number`` 是否就是"邀请好友数"（本模块的档位策略以此为前提）；
2. 免费档（不带好友）时 ``15003.bbqFriendList`` 传空列表是否被服务端接受。

【两顿的识别依据（为什么不用 groupID 硬编码）】
两期活动由服务端下发，``weeks`` 字段区分：``weeks == 0`` 是早宴席
（groupID 501000004）、``weeks == 2`` 是晚宴席（groupID 501000005）—— 这是
10.3/10.4 抓包的实测对应。但**编号由服务端给**，所以这里只按 ``weeks``
找期，再取其 ``groupID``；找不到就退回按顺序（第一段=早、最后一段=晚）。
``MEAL_WEEKS`` 是这张映射表的唯一出处。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

import config
from client.exceptions import SpendBlockedError
from client.game_client import GameClient
from client.session import GameSession
from client.spending import SpendPurpose, SpendPolicy
from models.bbq import (
    ActivityIntervalDataClass,
    BBQFriendsPacket,
    BBQFriendsRes,
    GetBBQDataPacket,
    GetBBQDataRes,
)
from models.daily_reset import in_banquet_gap
from models.activity_stage import DISPLAY_TZ
from models.game_config import ExtraEnergyTier, load_extra_energy_tiers
from services import banquet_state
from tasks.base import (
    BaseTask,
    QuantityScope,
    TaskResult,
    cost_phrase,
    register_task,
    reward_phrase,
)

_LOGGER: Final[logging.Logger] = logging.getLogger("tasks.bbq")

#: 早宴席的餐次标识（与 ``services.banquet_state.MEALS`` 对应）。
MEAL_MORNING: Final[str] = "morning"
#: 晚宴席的餐次标识。
MEAL_EVENING: Final[str] = "evening"

#: 合法的 ``meal`` 取值。
MEALS: Final[tuple[str, ...]] = (MEAL_MORNING, MEAL_EVENING)

#: 餐次 → ``32008.activityIntervalDataList[].weeks`` 的映射（实测依据见模块文档）。
MEAL_WEEKS: Final[dict[str, int]] = {MEAL_MORNING: 0, MEAL_EVENING: 2}

#: 餐次的中文名（日志与结果展示用；与前端「早宴席 / 晚宴席」保持一致）。
MEAL_LABELS: Final[dict[str, str]] = {
    MEAL_MORNING: "早宴席",
    MEAL_EVENING: "晚宴席",
}

#: 窗口相对当前时刻的状态。
#: ``not_started`` = 还没到；``covering`` = 正在该顿时间里；``ended`` = 已过。
MealWindowState = Literal["not_started", "covering", "ended"]


def _ms_to_clock(ms: int) -> str:
    """把协议里的**毫秒**时间戳转成 ``HH:MM``（只用于日志/结果里的可读提示）。

    :param ms: Unix 时间戳（毫秒）。``0`` 或负数视为"未知"，返回 ``"未知时间"``。
    :return: 本机时区的 ``"HH:MM"``。

    只取时分、不带日期：这两顿本来就是"每天的某个时段"，用户看提示时
    关心的是"几点到几点"，多写日期反而让人以为是某一天。
    """
    if ms <= 0:
        return "未知时间"
    return datetime.fromtimestamp(ms / 1000).strftime("%H:%M")




@dataclass(frozen=True)
class InvitePlan:
    """一次"邀请好友"的计划（**组包之前**就把金额算清楚，便于审计与日志）。"""

    #: 选中的档位（金额来自它的 ``item_amount``）
    tier: ExtraEnergyTier
    #: 实际要邀请的好友 ID（按服务端返回顺序取前 N 位）
    friend_ids: tuple[int, ...]
    #: 计划发出去的 ``groupID`` —— **必须取自 32008 的活动期间**，
    #: 不是配置表 ``menuID``（实测两者编号体系不同，见 :meth:`BBQEnergyTask._current_group_id`）
    group_id: int

    @property
    def amount(self) -> int:
        """预计消耗的钻石数。"""
        return self.tier.item_amount

    def describe(self) -> str:
        """一行摘要（日志与结果展示用）。"""
        return (
            f"{self.tier.describe()}；邀请 {len(self.friend_ids)} 位好友"
            f"（ID：{list(self.friend_ids)}）"
        )


@register_task
class BBQEnergyTask(BaseTask):
    """宴席（BBQ）：按需邀请好友 + 确认吃（**默认只读，不发 15003**）。"""

    name = "bbq_energy"

    #: 本任务**不再**属于"无条件花钱"的一类（见 ``BaseTask.diamond_purposes`` 的说明）：
    #: 花钱只发生在"邀请好友"那一个分支，未授权时其余部分照常执行。
    spends_diamond = False

    #: 用途级声明：本任务存在花钻石的分支（邀请好友）。
    #: ``check_preconditions`` 会据此提示"该用途未授权、对应步骤将被跳过"，
    #: 真正的硬拦发生在组包前的 :meth:`BaseTask.request_diamond_spend`。
    diamond_purposes = (SpendPurpose.BBQ_INVITE,)

    #: 任务列表（``python main.py tasks``）里的提示语。
    opt_in_hint = "需 --eat 才真的吃；--invite-friends 才会花钻石请客"

    def __init__(
        self,
        session: GameSession,
        *,
        logger: logging.Logger | None = None,
        dry_run: bool = False,
        spend_policy: SpendPolicy | None = None,
        eat: bool = False,
        friend_ids: Sequence[int] | None = None,
        meal: str | None = None,
        invite: bool | None = None,
        now: datetime | None = None,
    ) -> None:
        """构造任务。

        :param eat: 步骤② —— 是否**真的发送** ``15003``（默认 ``False`` = 只读）。
            免费档也依赖它：不给这个开关就什么都不发。
        :param friend_ids: 显式指定要邀请的好友；``None`` 表示按服务端返回顺序取前 N 位。
            做成参数是为了让调用方（以及将来的设置界面）能自己决定请谁，
            而不是把策略写死在任务里。
        :param meal: **指定顿入口** —— ``"morning"`` / ``"evening"``；``None``（默认）
            表示走原有的"整场"逻辑（CLI 与旧调用方）。给了值时走
            :meth:`execute` 里的**窗口判定 + 记账**新路径（见模块文档）。
        :param invite: **该顿自己的邀请开关**（网页那两个复选框）。``None`` = 未指定
            → 沿用整场的判据（消费策略里的 ``BBQ_INVITE`` 用途授权）。
            ``True`` 时仍要过消费闸门；``False`` 时**明确不邀请**（走免费档）。
            只在 ``meal`` 模式下有意义。
        :param now: 计算"今天是哪个宴席日"与窗口判定的基准（测试用；``None`` = 现在）。
            只在 ``meal`` 模式下参与记账；每顿的"现在覆盖没覆盖"一律以
            **服务端下发的毫秒时间戳**为准，与它无关。
        """
        super().__init__(
            session, logger=logger, dry_run=dry_run, spend_policy=spend_policy
        )
        #: 步骤②的开关：是否真的吃（False = 只读）
        self.eat = eat
        #: 显式指定的邀请名单（``None`` = 用服务端返回的顺序）
        self.friend_ids: tuple[int, ...] | None = (
            tuple(friend_ids) if friend_ids is not None else None
        )
        #: 指定顿（``None`` = 原有整场逻辑）；非法值视为 ``None``（与"没给"同义，
        #: 不做静默纠正为某一顿 —— 猜错会往错误的期发包）。
        self.meal: str | None = meal if meal in MEALS else None
        #: 该顿的邀请开关（``None`` = 未指定 → 沿用整场判据）
        self.invite: bool | None = None if invite is None else bool(invite)
        #: 宴席日记账的基准时刻（``None`` = 现在）
        self._now = now

    # ------------------------------------------------------------------
    # 指定顿：期定位与窗口判定（**只在 meal 模式下使用**）
    # ------------------------------------------------------------------
    @staticmethod
    def _meal_interval(
        data: GetBBQDataRes, meal: str
    ) -> ActivityIntervalDataClass | None:
        """找出 ``meal`` 对应的那一期活动。

        :return: ``activityIntervalDataList`` 里的元素；服务端一期都没下发时返回 ``None``。

        规则（按优先级）：

        1. ``weeks`` 等于 :data:`MEAL_WEEKS` 里该顿的值（早 0 / 晚 2，实测映射）；
        2. 找不到就按**顺序**兜底：早 = 第一段，晚 = 最后一段。
           兜底存在的意义是"服务端换了 weeks 编码时不至于直接罢工"，
           但这是一条**有风险的猜测**，所以会记一条 WARNING 让日志留下痕迹。
        """
        intervals = data.activityIntervalDataList
        if not intervals:
            return None
        wanted = MEAL_WEEKS.get(meal)
        if wanted is not None:
            matched = next(
                (item for item in intervals if item.weeks == wanted), None
            )
            if matched is not None:
                return matched
        fallback = intervals[0] if meal == MEAL_MORNING else intervals[-1]
        _LOGGER.warning(
            "服务端下发的活动期间里没有 weeks=%s 的那一期（meal=%s），"
            "已按顺序兜底取 groupID=%s —— 若抓包确认 weeks 编码变了，请更新 MEAL_WEEKS。",
            wanted,
            meal,
            fallback.groupID,
        )
        return fallback

    @staticmethod
    def _window_state(interval: ActivityIntervalDataClass, now_ms: int) -> MealWindowState:
        """判定该期活动的窗口相对 ``now_ms`` 处于什么状态。

        :param interval: 该顿对应的活动期间（``ActivityIntervalDataClass``）。
        :param now_ms: 当前时刻（Unix 时间戳，**毫秒** —— 与协议字段同口径）。
        :return: ``"not_started"`` / ``"covering"`` / ``"ended"``。

        .. note::
           **必须用毫秒比较**：10.3 踩过"拿秒比毫秒 → 恒不覆盖"的坑
           （见 :meth:`_current_group_id` 的说明）。start/end 都是毫秒。
        """
        start = int(interval.startDateTime)
        end = int(interval.endDateTime)
        if now_ms < start:
            return "not_started"
        if now_ms > end:
            return "ended"
        return "covering"

    # ------------------------------------------------------------------
    # 配置表读取与档位选择（花钱分支的**金额依据**）
    # ------------------------------------------------------------------
    @staticmethod
    def _load_tiers() -> list[ExtraEnergyTier] | None:
        """读宴席档位配置表（``ExtraEnergyData``）。

        :return: 档位列表（按 ``number`` 升序）；读不到时返回 ``None``。

        【为什么这里吞掉异常、返回 None】
        档位表属于本地素材（``Cherrytale Asset/TextAsset``）。在**只读**分支里
        它只是展示信息，缺了不该让任务崩掉；而在**花钱**分支里它是金额的唯一依据，
        拿不到就必须拒绝发送。两种场景对"查不到"的反应不同，
        所以这里只负责读，判断交给调用方 —— 一个函数只做一件事。
        """
        try:
            tiers = load_extra_energy_tiers()
        except (OSError, ValueError) as exc:  # TableError 继承自 ValueError
            _LOGGER.warning("读取宴席档位配置失败（本次只能展示已有信息）：%s", exc)
            return None
        return sorted(tiers, key=lambda tier: tier.number)

    @staticmethod
    def _pick_tier(
        tiers: Sequence[ExtraEnergyTier], friend_count: int
    ) -> ExtraEnergyTier | None:
        """"只走最大档"的落点：按邀请人数挑档位。

        :param friend_count: 这次要邀请的好友数 ``N``（**不含自己**，上限见
            ``config.BBQ_MAX_INVITE_FRIENDS = 4``）。
        :return: 选中档位；一个可用档都没有时返回 ``None``。

        规则（按优先级）：

        1. ``config.BBQ_MENU_ID``（**逃生门**）显式指定 → 用它；
        2. ``number == N + 1`` 的档（★ 2026-10-04 实测：``number`` 是**含自己**
           的参与人数** —— 免费吃 0 好友对应 number=1；10.3 抓包 4 好友对应
           number=5 档（31 钻）；发 5 位好友被服务端拒 -6）；
        3. ``number ≤ N + 1`` 的最大档（配置表没有正好对应的那一行时兜底）。
        """
        if not tiers:
            return None
        if config.BBQ_MENU_ID is not None:
            matched = next(
                (tier for tier in tiers if tier.menu_id == config.BBQ_MENU_ID), None
            )
            _LOGGER.warning(
                "逃生门 config.%s=%d 生效：档位不再按邀请人数挑选。",
                config.BBQ_MENU_ID_ENV_NAME,
                config.BBQ_MENU_ID,
            )
            return matched
        target_number = friend_count + 1  # number 含自己
        exact = next((tier for tier in tiers if tier.number == target_number), None)
        if exact is not None:
            return exact
        lower = [tier for tier in tiers if tier.number <= target_number]
        return max(lower, key=lambda tier: tier.number) if lower else None


    @staticmethod
    def _current_group_id(data: GetBBQDataRes, *, for_free: bool = False) -> int | None:
        """选出"本次要参与的那一期宴席"的 ``groupID``。

        :param for_free: ``True`` = 免费吃的选取规则；``False`` = 付费邀请。
        :return: 要填进 ``15003.groupID`` 的编号；服务端一期都没下发时返回 ``None``。

        【⚠️ 实测结论（2026-09-21 真机 #113）：协议 groupID ≠ 配置表 menuID】
        服务端下发的是 ``[501000004, 501000005]``，而 ``ExtraEnergyData.menuID`` 是
        ``500000001~5`` —— 两者**不是同一编号体系**（Q-004 的答案）。
        所以「用哪一期活动」这个问题**只能从响应里回答**，绝不能拿 menuID 去填；
        配置表的档位只用来**估算"请 N 位好友要花多少钻"**，不参与组包。

        【★ 2026-10-03 规则修订（依据 10.3 双抓包：免费+付费宴席 / 过时间宴席）】
        ① **时间戳是毫秒**：旧代码拿 ``time.time()``（秒）去比 ``startDateTime``
           （毫秒），"覆盖当前时段"永远为空，于是恒走"回退最后一期"——
           免费吃被错误地发到了付费那一期。
        ② **免费与付费不是同一期**：两个 10.3 抓包里，免费吃固定落在
           ``weeks == 0`` 的那一期（501000004；甚至在其开始时间之前发出，
           服务端照常接受并加能量），付费邀请落在覆盖当前时刻的那一期
           （501000005）。所以选取规则按用途分流：

           - 免费吃：取 ``weeks == 0`` 的**第一期**；一期都没有就退回第一期；
           - 付费邀请：取**覆盖当前时刻**（毫秒比较）、其中开始最晚的那一期；
             没有覆盖的就退回服务端顺序里的**最后一个**。
        """
        intervals = data.activityIntervalDataList
        if not intervals:
            return None
        if for_free:
            free = [item for item in intervals if item.weeks == 0]
            return (free or intervals)[0].groupID
        now_ms = int(time.time() * 1000)
        covering = [
            item
            for item in intervals
            if item.startDateTime <= now_ms <= item.endDateTime
        ]
        if covering:
            return max(covering, key=lambda item: item.startDateTime).groupID
        return intervals[-1].groupID

    # ------------------------------------------------------------------
    # ① 拉数据（**永远只读**，任何模式都会执行）
    # ------------------------------------------------------------------
    def _fetch(self, game: GameClient) -> GetBBQDataRes:
        """拉取宴席数据（消息号 32007 → 32008）。"""
        view = game.send(
            GetBBQDataPacket(),
            expect=GetBBQDataRes,
            include_defaults=True,
        )
        return view.decode_sub_packet(GetBBQDataRes)

    # ------------------------------------------------------------------
    # ② 计划"邀请谁、用哪一档"（**组包之前**就把金额算清）
    # ------------------------------------------------------------------
    def _plan_invite(self, data: GetBBQDataRes) -> InvitePlan | None:
        """算出这次邀请的方案。

        :return: :class:`InvitePlan`；没有可邀请的好友、或读不到档位表时返回 ``None``
            （调用方会跳过邀请并在结果里写清原因）。

        邀请人数：``N = min(可邀请好友数, config.BBQ_MAX_INVITE_FRIENDS=4)`` ——
        ★ 实测服务端最多收 4 位好友（10.3 抓包 = 4 位；发 5 位被拒 -6），
        "只要最大档"就落在这一行；好友不足 4 位时才自然退到小档。

        .. note::
           这里**只做计划**，不申请消费放行、也不组包。顺序必须保持
           "计划 → 申请 → 组包 → 发送"：一旦组包早于申请，
           "开关关闭"就不再等于"字节从未被构造"。
        """
        candidates = (
            list(self.friend_ids) if self.friend_ids is not None else data.friend_ids()
        )
        if not candidates:
            self.logger.warning("服务端没有返回可邀请的好友，跳过邀请。")
            return None

        friend_count = min(len(candidates), config.BBQ_MAX_INVITE_FRIENDS)
        tiers = self._load_tiers()
        if tiers is None:
            self.logger.warning(
                "读不到档位配置表 → 无法确认这一顿要花多少钻，已跳过邀请（不猜金额）。"
            )
            return None
        tier = self._pick_tier(tiers, friend_count)
        if tier is None:
            self.logger.warning(
                "档位配置里找不到 %d 人对应的档位，已跳过邀请（不猜档位）。", friend_count
            )
            return None

        group_id = self._current_group_id(data)
        if group_id is None:
            self.logger.warning(
                "服务端没有下发任何活动期间（groupID）→ 不知道该参与哪一期，已跳过邀请。"
            )
            return None
        # 配置表档位只用于**估算费用**，不参与组包（见 _current_group_id 的实测结论）。
        self.logger.info(
            "本期活动 groupID=%d；按 %d 位好友估算费用：%s",
            group_id,
            friend_count,
            tier.describe(),
        )
        return InvitePlan(
            tier=tier,
            friend_ids=tuple(candidates[:friend_count]),
            group_id=group_id,
        )

    # ------------------------------------------------------------------
    # ③ 发送"吃"（本任务**唯一有副作用**的请求）
    # ------------------------------------------------------------------
    def _eat(
        self, game: GameClient, *, group_id: int, friend_ids: Sequence[int]
    ) -> tuple[bool, str]:
        """发送 ``15003 BBQFriendsPacket``。

        :return: ``(是否成功, 可读结果)``。

        .. warning::
           **不重试**：本请求有副作用（可能扣钻石、可能计次）。超时后服务端
           可能已经受理，重试等于加倍消费 —— 必须人工确认后再决定是否再跑。
           与 ``DMReward`` / ``GetFreeGiftPacket`` 同一原则。
        """
        packet = BBQFriendsPacket.for_friends(group_id=group_id, friend_ids=friend_ids)
        view = game.send(packet, expect=BBQFriendsRes, include_defaults=True)
        res = view.decode_sub_packet(BBQFriendsRes)
        if not res.is_success():
            # -6 实测（2026-10-04）：好友数超过服务端上限（4 位）等条件不符。
            hint = (
                "（-6：常见原因是好友数超过 4 位上限，或该期不可邀请）"
                if res.errorCode == -6
                else ""
            )
            return False, f"errorCode={res.errorCode}{hint}"
        # 15004 的 addList / costList 数量口径**尚未实证**（是本次增量还是操作后
        # 持有量，没有抓包证据）—— 按「保守口径」只报**件数**，不报数量，
        # 也不用"获得/消耗"去描述一个我们并不知道含义的数字。
        # 详见 models/reward.py 的模块文档（QuantityScope.UNKNOWN）。
        rewards = reward_phrase(res.addList, scope=QuantityScope.UNKNOWN)
        costs = cost_phrase(res.costList, scope=QuantityScope.UNKNOWN)
        return True, "；".join(part for part in (rewards, costs) if part)


    # ------------------------------------------------------------------
    # ④ 主流程
    # ------------------------------------------------------------------
    def execute(self) -> TaskResult:
        """只读拉数据 →（可选）邀请好友 →（可选）真的吃。

        【入口分流：``meal`` 参数决定走哪条路】

            ``meal`` 为 ``None``          → 原有整场逻辑（下面的三条分支，**未改动**）
            ``meal`` 为 ``"morning"/"evening"`` → :meth:`_execute_meal`：
                                            窗口判定 → 只吃那一顿 → 记账

        【三条分支都在这里，方便一眼看全】

            ``eat=False``               → 只读：列档位/好友/状态，一个 15003 都不发
            ``eat=True`` 且邀请已授权    → 最高档邀请好友一起吃（先过闸门再组包）
            ``eat=True`` 且邀请未授权    → 免费档自己吃（**完全不碰消费闸门**）
        """
        with self.open_game_client() as game:
            if self.dry_run:
                return self._dry_run(game)

            data = self._fetch(game)
            self.logger.info("宴席数据：%s", data.summary())
            for friend in data.bbqFriendList:
                # 状态字段含义未实测确认（Q-005）：先原样记录供对照，不据此筛人。
                self.logger.debug(
                    "好友 %s(%d) 状态：%s",
                    friend.name,
                    friend.playerID,
                    friend.state_hint(),
                )

            # ★ 指定顿入口：从拉完数据起分流，下面整场逻辑保持原样。
            if self.meal is not None:
                return self._execute_meal(game, data)

            if not self.eat:
                return self._read_only_result(data)

            if self.spend_policy.allows_purpose(SpendPurpose.BBQ_INVITE):
                return self._invite_and_eat(game, data)

            self.logger.info(
                "用途「BBQ_INVITE」未获授权（%s）→ 本次走**免费档**，不会花任何钻石。",
                self.spend_policy.describe(),
            )
            return self._eat_free(game, data)

    # ------------------------------------------------------------------
    # ④' 指定顿（``meal``）模式：空档期 → 只吃那一顿（邀请另受窗口约束）→ 记账
    # ------------------------------------------------------------------
    def _banquet_now(self) -> datetime:
        """宴席时间判定的"现在"：显式给了 ``now`` 就用它，否则由 ``time.time()`` 推出。

        【为什么不用 ``datetime.now()``】
        **为了可测**。指定顿的用例会把 ``tasks.bbq.time.time`` 钉死成一个固定时刻；
        空档期判定若走 ``datetime.now()``，测试就只在"真实时钟恰好落在 06:00–06:10"
        时才会变红 —— 一天里随机崩 10 分钟，是最难查的那种失败。
        走 ``time.time()`` 以后，"现在"在同一进程里只有**一个**来源，
        测试钉住的时刻就是判定用的时刻。

        :return: 带展示时区（``DISPLAY_TZ`` = UTC+8）的时刻。
        """
        if self._now is not None:
            return self._now
        return datetime.fromtimestamp(time.time(), DISPLAY_TZ)

    def _meal_wants_invite(self) -> bool:
        """指定顿模式下"这一顿要不要邀请好友"。

        规则（与用户拍板的设计一致）：

        - ``self.invite is False`` → **明确不邀请**（网页那个复选框没勾）；
        - ``self.invite is True`` → 要邀请，但**仍要过消费闸门**
          （``allows_purpose``；总闸没开一样不会花钻石）；
        - ``self.invite is None``（CLI / 旧调用方没给这个开关）→ 沿用整场判据：
          消费策略里 ``BBQ_INVITE`` 用途是否授权。

        注意本函数**只管"用户的意愿"**，不管窗口是否覆盖 —— 真正的邀请还要由
        :meth:`_execute_meal` 再叠一道**窗口闸门**（"窗不覆盖优先于开关开着"）；
        两道条件都满足才会走花钱的邀请分支，否则一律退回免费档。
        """
        if self.invite is None:
            return self.spend_policy.allows_purpose(SpendPurpose.BBQ_INVITE)
        if not self.invite:
            return False
        return self.spend_policy.allows_purpose(SpendPurpose.BBQ_INVITE)

    def _execute_meal(self, game: GameClient, data: GetBBQDataRes) -> TaskResult:
        """吃**指定的那一顿**（早 / 晚），带记账与「只对邀请生效」的窗口判定。

        流程（顺序即优先级）：

        1. **空档期**（每天 06:00–06:10，:func:`models.daily_reset.in_banquet_gap`）⇒
           两顿**都一个 15003 都不发** —— 晚宴席 06:00 已结束、早宴席 06:10 才刷新，
           此刻服务端没有可参与的期，发了只会白挨一次拒绝；
        2. **定位该顿的期**（:meth:`_meal_interval`，按 ``weeks``）；
        3. **晚宴席**：窗口不覆盖 ⇒ **不发包**（它的"吃"与"邀请"都只在
           ``14:00–次日06:00`` 内 —— 用户 2026-10-06 拍板"晚宴席不放宽"）；
           **早宴席**：**不看窗口** —— 只要本宴席日没吃过就能吃（第七版修正）。
           它的可吃期从 ``06:10`` 一直到次日 ``06:00``（同一宴席日），
           窗口（``06:10–13:59``）**只用来限制邀请**；
        4. **已吃过就跳过**（``services.banquet_state`` 记账）—— 省一次注定被拒的包；
        5. 组包发送：**用户想邀请 且 窗口覆盖** → 邀请分支（先过消费闸门）；
           否则走**免费档**。窗口不覆盖时的邀请被**强制压掉**
           （"窗不覆盖优先于开关开着"），结果里会说明"已过邀请时间，只吃不邀请"；
        6. 成功吃到 → 写一条"本顿已吃"。

        .. note::
           **"吃"与"邀请"的窗口是两件事** —— 这是第七版的核心修正。抓包实证：
           10-03 凌晨 00:39 / 00:42 两次 ``15003{501000004, 无好友}`` 都发在该期窗口
           （06:10–13:59）**之外**，服务端照样 ``errorCode=0``；而那份"过时间无法用钻石
           邀请"的抓包里**一条付费邀请包都没有**。所以窗口只能管邀请，不能管吃。

        .. note::
           ``eat=False`` 在指定顿模式下**依然生效**：只读点开某一顿不会发包。
           这保住了"开关模块"的本意 —— 想看状态不该产生副作用。
        """
        meal = self.meal  # 调用方已保证非 None
        assert meal is not None  # for type-narrowing
        label = MEAL_LABELS[meal]

        # ① 空档期（06:00–06:10）：两顿都不可吃（用户 2026-10-06 确认）。
        #    放在最前面 —— 它不需要任何服务端数据，而且此刻发出去必然是无效请求。
        if in_banquet_gap(self._banquet_now()):
            self.logger.info(
                "跳过%s：处于宴席空档期（06:00–06:10），未发送任何 15003。", label
            )
            return TaskResult(
                task=self.name,
                ok=True,
                message=(
                    f"{label}：现在是宴席空档期（每天 06:00–06:10）—— "
                    "晚宴席已结束、早宴席 06:10 才刷新，两顿都不可吃，未发送任何请求。"
                ),
                data={
                    "mode": "banquet_gap",
                    "meal": meal,
                    "meals": self._meals_view(data),
                },
            )

        interval = self._meal_interval(data, meal)
        if interval is None:
            return TaskResult(
                task=self.name,
                ok=False,
                message=(
                    f"{label}：服务端没有下发任何活动期间（groupID）→ "
                    "不知道该参与哪一期，已放弃发送 —— 宁可什么都不做，也不猜一个编号。"
                ),
                data={"mode": "meal_skipped", "meal": meal, "meals": self._meals_view(data)},
            )

        now_ms = int(time.time() * 1000)
        state = self._window_state(interval, now_ms)
        covering = state == "covering"

        # ② 晚宴席只在窗口内可玩；早宴席不看窗口（窗口外即"补吃"）。
        if meal == MEAL_EVENING and not covering:
            # ★ 不发包。"未开始"与"已结束"用不同措辞，
            #   因为这俩在用户看来是两件不同的事（一个"还早"，一个"错过了"）。
            reason = (
                f"{label}还没到开放时间（本顿："
                f"{_ms_to_clock(interval.startDateTime)} 起）"
                if state == "not_started"
                else f"{label}已经过了今天的开放时间（本顿已于 "
                f"{_ms_to_clock(interval.endDateTime)} 结束）"
            )
            self.logger.info("跳过%s：%s，未发送任何 15003。", label, reason)
            return TaskResult(
                task=self.name,
                ok=True,
                message=f"{reason} —— 未发送任何请求。",
                data={
                    "mode": "meal_window_closed",
                    "meal": meal,
                    "window_state": state,
                    "group_id": interval.groupID,
                    "meals": self._meals_view(data),
                },
            )

        # ③ 这顿吃过没有（服务端记账，跨窗口/端口都不丢）。
        done = banquet_state.meals_done(self._session_info(), now=self._now)
        if meal in done:
            return TaskResult(
                task=self.name,
                ok=True,
                message=(
                    f"{label}本宴席日已吃过，跳过重复发送"
                    f"（记账：{', '.join(done)}）。"
                ),
                data={
                    "mode": "meal_already_done",
                    "meal": meal,
                    "done": list(done),
                    "meals": self._meals_view(data),
                },
            )

        # ④ 本顿此刻"能不能邀请"：用户想邀请 **且** 该期窗口覆盖 now。
        #    两者缺一都走免费档；"想邀请但窗口不覆盖"要单独说明（被强制压掉）。
        wants_invite = self._meal_wants_invite()
        blocked_by_window = wants_invite and not covering
        if blocked_by_window:
            self.logger.info(
                "%s：本顿邀请窗口（%s–%s）未覆盖当前时刻 → **强制不邀请**，"
                "本次只吃自己的那一份。",
                label,
                _ms_to_clock(interval.startDateTime),
                _ms_to_clock(interval.endDateTime),
            )

        if not self.eat:
            # 只读：告诉用户"如果现在点会发生什么"，但绝不发包。
            if wants_invite and covering:
                plan_text = "将以最高档邀请好友"
            elif blocked_by_window:
                plan_text = "已过邀请时间，只吃不邀请"
            else:
                plan_text = "将走免费档"
            return TaskResult(
                task=self.name,
                ok=True,
                message=(
                    f"{label}处在可吃时段（groupID={interval.groupID}，"
                    f"邀请窗口 {_ms_to_clock(interval.startDateTime)}–"
                    f"{_ms_to_clock(interval.endDateTime)}）；"
                    f"只读模式未发送 15003（{plan_text}）。"
                ),
                data={
                    "mode": "meal_read_only",
                    "meal": meal,
                    "group_id": interval.groupID,
                    "invite_authorized": wants_invite and covering,
                    "invite_blocked_by_window": blocked_by_window,
                    "meals": self._meals_view(data),
                },
            )

        # —— 真的要吃 ——
        if wants_invite and covering:
            return self._meal_invite_and_eat(game, data, meal=meal, interval=interval)
        if blocked_by_window:
            self.logger.info(
                "%s：因窗口未覆盖，邀请开关被**强制忽略** → 走免费档，不会花任何钻石。",
                label,
            )
        else:
            self.logger.info(
                "%s：本顿不邀请好友（开关未开或用途未授权）→ 走**免费档**，不会花任何钻石。",
                label,
            )
        return self._meal_eat_free(
            game,
            data,
            meal=meal,
            interval=interval,
            invite_blocked_by_window=blocked_by_window,
        )

    def _meal_invite_and_eat(
        self,
        game: GameClient,
        data: GetBBQDataRes,
        *,
        meal: str,
        interval: ActivityIntervalDataClass,
    ) -> TaskResult:
        """指定顿的"邀请 + 吃"分支：期**锁定为该顿**，先过闸门再组包。

        与整场的 :meth:`_invite_and_eat` 唯一区别是 ``groupID`` 不再靠
        :meth:`_current_group_id` 猜，而是**用该顿自己的期** —— 否则"上午点晚宴席的
        邀请"可能把包发到另一期去。
        """
        label = MEAL_LABELS[meal]
        plan = self._plan_invite(data)
        if plan is None:
            self.logger.warning("%s：邀请未能安排 → 回退免费档（只吃自己的那一份）。", label)
            fallback = self._meal_eat_free(game, data, meal=meal, interval=interval)
            return TaskResult(
                task=fallback.task,
                ok=fallback.ok,
                message=(
                    f"{label}邀请未安排（无可邀请好友或档位表不可用），"
                    f"已回退免费档：{fallback.message}"
                ),
                data=dict(fallback.data, invite_skipped=True),
            )

        # 关键：把期换成"该顿的期"，而不是 _plan_invite 默认挑的覆盖期。
        plan = InvitePlan(
            tier=plan.tier, friend_ids=plan.friend_ids, group_id=interval.groupID
        )

        try:
            self.request_diamond_spend(
                plan.amount,
                reason=(
                    f"{label}邀请好友（{plan.tier.describe()}，{len(plan.friend_ids)} 位好友）"
                ),
                purpose=SpendPurpose.BBQ_INVITE,
            )
        except SpendBlockedError as exc:
            self.logger.warning("⚠ %s 邀请好友被闸门拦下，已跳过：%s", label, exc)
            return TaskResult(
                task=self.name,
                ok=True,
                message=f"{label}邀请好友被消费闸门拦下（未花钻石、未发送邀请）：{exc}",
                data={
                    "mode": "meal_invite_blocked",
                    "meal": meal,
                    "planned_diamond": plan.amount,
                    "meals": self._meals_view(data),
                },
            )

        ok, text = self._eat(game, group_id=plan.group_id, friend_ids=plan.friend_ids)
        if not ok:
            if not self.dry_run:
                self.spend_policy = self.spend_policy.refund(plan.amount)
            self.logger.info(
                "%s 邀请被服务端拒绝（%s），已把预扣的 %d 钻从消耗统计冲回",
                label,
                text,
                plan.amount,
            )
            return TaskResult(
                task=self.name,
                ok=False,
                message=(
                    f"{label}邀请好友失败：{text}"
                    f"（未实际扣钻，已从消耗统计冲回 {plan.amount} 钻）"
                ),
                data={
                    "mode": "meal_invite",
                    "meal": meal,
                    "plan": plan.describe(),
                    "refunded": plan.amount,
                    "meals": self._meals_view(data),
                },
            )

        recorded = banquet_state.mark_meal_done(meal, self._session_info(), now=self._now)
        return TaskResult(
            task=self.name,
            ok=True,
            message=(
                f"{label}已花 {plan.amount} 钻邀请 {len(plan.friend_ids)} 位好友：{text}"
                f"（记账：{', '.join(recorded)}）"
            ),
            data={
                "mode": "meal_invite",
                "meal": meal,
                "plan": plan.describe(),
                "diamond_spent": self.spend_policy.spent_so_far,
                "meals_done": list(recorded),
                "meals": self._meals_view(data),
            },
        )

    def _meal_eat_free(
        self,
        game: GameClient,
        data: GetBBQDataRes,
        *,
        meal: str,
        interval: ActivityIntervalDataClass,
        invite_blocked_by_window: bool = False,
    ) -> TaskResult:
        """指定顿的免费分支：不带好友、用**该顿自己的 groupID** —— 不碰消费闸门。

        :param invite_blocked_by_window: 本次走免费档是因为"窗口外强制不邀请"
            （而不是用户没开开关）。**只影响结果文案与界面提示**，不改变行为。
        """
        label = MEAL_LABELS[meal]
        ok, text = self._eat(game, group_id=interval.groupID, friend_ids=())
        result_data = {
            "mode": "meal_free",
            "meal": meal,
            "group_id": interval.groupID,
            "diamond_spent": 0,
            "invite_blocked_by_window": invite_blocked_by_window,
            "meals": self._meals_view(data),
        }
        if not ok:
            return TaskResult(
                task=self.name,
                ok=False,
                message=f"{label}免费档吃宴席失败：{text}",
                data=result_data,
            )
        recorded = banquet_state.mark_meal_done(meal, self._session_info(), now=self._now)
        note = (
            "（已过邀请时间，本次只吃不邀请）"
            if invite_blocked_by_window
            else "（未邀请好友、未花钻石）"
        )
        return TaskResult(
            task=self.name,
            ok=True,
            message=(
                f"{label}免费档吃宴席成功{note}：{text}"
                f"（记账：{', '.join(recorded)}）"
            ),
            data=dict(result_data, meals_done=list(recorded)),
        )

    @staticmethod
    def _session_info() -> dict[str, object]:
        """给宴席记账用的身份信息（``describe_session()`` 形态的字典）。

        【为什么这里现查而不用 ``self.session``】
        ``services.daily_state.identity_key``（宴席记账复用它）要的是
        ``runner.describe_session()`` 返回的**字典**（含 ``server_id`` / ``player_id``），
        而 ``self.session`` 是 ``GameSession`` 对象 —— 传错类型会在 ``.get`` 时炸。
        统一走 ``describe_session()``，与 ``webapi`` 侧「不传 session 即现查」
        的做法保持一致，也就只有一处身份来源。
        """
        from services import runner

        try:
            return dict(runner.describe_session())
        except Exception as exc:  # noqa: BLE001
            # 查不到就退到"无会话"，记账自然落空 —— 与 identity_key 的取舍一致。
            _LOGGER.warning("读取会话信息失败，本次宴席记账按“未登录”处理：%s", exc)
            return {}

    # ------------------------------------------------------------------
    # ⑤ 三条分支的结果组装
    # ------------------------------------------------------------------
    def _meals_view(self, data: GetBBQDataRes) -> list[dict[str, object]]:
        """给界面用的"两顿各自什么状态"（只读、不发包）。

        :return: 形如 ``[{"meal":"morning","label":"早宴席","group_id":501000004,
            "state":"makeup","edible":true,"invitable":false,
            "window_state":"ended","window_text":"06:10–13:59"}, ...]``。

        【字段含义（第七版新增 ``state`` / ``edible`` / ``invitable`` / ``gap``）】
        - ``window_state``：该期**自己的窗口**相对现在的状态
          （``not_started`` / ``covering`` / ``ended``）—— 这是协议事实，原样给出；
        - ``state``：**界面直接照着渲染**的状态，已经把第七版的规则算进去了：
          ``gap``（06:00–06:10 空档）/ ``ready``（可吃可邀请）/
          ``makeup``（**可补吃**：早宴席窗口外，能吃但不能邀请）/
          ``not_started`` / ``expired`` / ``unknown``；
        - ``edible``：此刻**能不能吃**；``invitable``：此刻**能不能邀请好友**。
          两者在早宴席上会分叉（窗口外 edible=True / invitable=False），这正是
          "窗口只管邀请、不管吃"的落点。

        【为什么把状态判定放后端】
        两条理由：(1) "现在覆盖没覆盖"要用**服务端下发的毫秒时间戳**（本地时钟可能不准），
        所以判定必须在拿到 32008 之后做；(2) 第七版的"早宴席窗口外仍可吃"是**业务规则**，
        前端再实现一遍就会出现两套口径 —— 前端只照着 ``state`` 渲染。
        （唯一的例外是空档期：它是**每天固定的 10 分钟**，与 :func:`banquet_day`
        的换日点同源，故用本机时钟判定，见 :func:`models.daily_reset.in_banquet_gap`。）
        """
        now_ms = int(time.time() * 1000)
        gap = in_banquet_gap(self._banquet_now())
        view: list[dict[str, object]] = []
        for meal in MEALS:
            interval = self._meal_interval(data, meal)
            if interval is None:
                view.append(
                    {
                        "meal": meal,
                        "label": MEAL_LABELS[meal],
                        "group_id": None,
                        "window_state": "unknown",
                        "state": "gap" if gap else "unknown",
                        "edible": False,
                        "invitable": False,
                        "gap": gap,
                        "window_text": "服务端未下发该时段",
                        "start_text": "",
                        "end_text": "",
                    }
                )
                continue

            window_state = self._window_state(interval, now_ms)
            covering = window_state == "covering"
            if gap:
                # 空档期：协议里两顿都不在覆盖中，界面统一显示"休整中"。
                ui_state = "gap"
                edible = invitable = False
            elif meal == MEAL_MORNING:
                # ★ 第七版：早宴席窗口外 = 补吃 —— 能吃，但不能邀请。
                ui_state = "ready" if covering else "makeup"
                edible = True
                invitable = covering
            else:
                # 晚宴席不放宽：吃与邀请都只在窗口内。
                ui_state = {
                    "covering": "ready",
                    "not_started": "not_started",
                    "ended": "expired",
                }[window_state]
                edible = covering
                invitable = covering

            view.append(
                {
                    "meal": meal,
                    "label": MEAL_LABELS[meal],
                    "group_id": interval.groupID,
                    "window_state": window_state,
                    "state": ui_state,
                    "edible": edible,
                    "invitable": invitable,
                    "gap": gap,
                    "window_text": (
                        f"{_ms_to_clock(interval.startDateTime)}–"
                        f"{_ms_to_clock(interval.endDateTime)}"
                    ),
                    "start_text": _ms_to_clock(interval.startDateTime),
                    "end_text": _ms_to_clock(interval.endDateTime),
                }
            )
        return view

    def _read_only_result(self, data: GetBBQDataRes) -> TaskResult:
        """只读分支：把"如果吃会怎样"完整摆在用户面前，但不发任何动作请求。"""
        tiers = self._load_tiers()
        tier_lines = [tier.describe() for tier in tiers] if tiers else []
        plan = self._plan_invite(data)
        group_id = self._current_group_id(data)
        message = (
            f"只读完成（未发送 15003）：本期 groupID={group_id}；"
            f"可邀请好友 {len(data.friend_ids())} 人；"
            f"档位（仅用于估算费用）"
            f"{'；'.join(tier_lines) if tier_lines else '（读不到配置表）'}"
        )
        if plan is not None:
            message += f"；若开启邀请将执行：{plan.describe()}（预计 {plan.amount} 钻）"
        return TaskResult(
            task=self.name,
            ok=True,
            message=message,
            data={
                "mode": "read_only",
                "group_id": group_id,
                "friend_count": len(data.friend_ids()),
                "tiers": tuple(tier_lines),
                "planned_invite": plan.describe() if plan is not None else None,
                "planned_diamond": plan.amount if plan is not None else 0,
                # ★ 给前端双按钮用的两顿状态（含窗口与"现在覆盖没覆盖"）
                "meals": self._meals_view(data),
            },
        )

    def _invite_and_eat(self, game: GameClient, data: GetBBQDataRes) -> TaskResult:
        """邀请分支：**先过闸门、后组包**，再发送。"""
        plan = self._plan_invite(data)
        if plan is None:
            # 想请客却请不了（没好友 / 档位表不可用）→ 回退免费档：
            # "eat=True"这个用户意图是明确的 —— 他就是要吃这一顿。
            self.logger.warning("邀请未能安排 → 回退到免费档（只吃自己的那一份）。")
            fallback = self._eat_free(game, data)
            return TaskResult(
                task=fallback.task,
                ok=fallback.ok,
                message=(
                    f"邀请未安排（无可邀请好友或档位表不可用），已回退免费档：{fallback.message}"
                ),
                data=dict(fallback.data, invite_skipped=True),
            )

        # 顺序很关键：先申请放行，再组包（见 request_diamond_spend 的说明）。
        try:
            self.request_diamond_spend(
                plan.amount,
                reason=(
                    f"宴席邀请好友（{plan.tier.describe()}，{len(plan.friend_ids)} 位好友）"
                ),
                purpose=SpendPurpose.BBQ_INVITE,
            )
        except SpendBlockedError as exc:
            # 走到这里说明"询问"与"放行"之间出现了不一致（例如策略被换过）。
            # 这属于**正常状态**：跳过邀请并如实说明，绝不吞掉原因。
            self.logger.warning("⚠ 邀请好友被闸门拦下，已跳过：%s", exc)
            return TaskResult(
                task=self.name,
                ok=True,
                message=f"邀请好友被消费闸门拦下（未花钻石、未发送邀请）：{exc}",
                data={"mode": "invite_blocked", "planned_diamond": plan.amount},
            )

        ok, text = self._eat(game, group_id=plan.group_id, friend_ids=plan.friend_ids)
        if not ok:
            # ★ 2026-10-04：闸门是"预扣"制（先记账再发包），服务端拒绝时钻石
            # 并没有真花出去 —— 必须冲回，否则运行汇总的"共消耗 N 钻"是假的
            # （实测：errorCode=-6 被拒，汇总却显示消耗 31 钻）。
            if not self.dry_run:
                self.spend_policy = self.spend_policy.refund(plan.amount)
            self.logger.info(
                "邀请被服务端拒绝（%s），已把预扣的 %d 钻从消耗统计冲回",
                text,
                plan.amount,
            )
            return TaskResult(
                task=self.name,
                ok=False,
                message=f"邀请好友吃宴席失败：{text}（未实际扣钻，已从消耗统计冲回 {plan.amount} 钻）",
                data={"mode": "invite", "plan": plan.describe(), "refunded": plan.amount},
            )
        return TaskResult(
            task=self.name,
            ok=True,
            message=f"已花 {plan.amount} 钻邀请 {len(plan.friend_ids)} 位好友吃宴席：{text}",
            data={
                "mode": "invite",
                "plan": plan.describe(),
                "diamond_spent": self.spend_policy.spent_so_far,
            },
        )


    def _eat_free(self, game: GameClient, data: GetBBQDataRes) -> TaskResult:
        """免费分支：不带好友、用**服务端下发的本期 groupID** —— 完全不碰消费闸门。

        这是"步骤②与消费闸门无关"的落点：请求里既没有好友、也没有付费档位，
        所以它不可能产生任何消费，自然也不需要（也不应该）过闸门。

        .. note::
           本分支**不读配置表**：免费档的 ``groupID`` 同样来自服务端
           （实测 groupID 与配置表 menuID 不是同一编号体系），
           少依赖一张本地素材就少一个失败点。
           选取规则见 :meth:`_current_group_id`（★ 10.3 抓包：免费吃锚定
           ``weeks == 0`` 的那一期，与付费邀请不是同一期）。
        """
        group_id = self._current_group_id(data, for_free=True)
        if group_id is None:
            return TaskResult(
                task=self.name,
                ok=False,
                message=(
                    "服务端没有下发任何活动期间（groupID）→ 不知道该参与哪一期，"
                    "已放弃发送 —— 宁可什么都不做，也不猜一个编号。"
                ),
                data={"mode": "free_skipped"},
            )
        ok, text = self._eat(game, group_id=group_id, friend_ids=())
        result_data = {"mode": "free", "group_id": group_id, "diamond_spent": 0}
        if not ok:
            return TaskResult(
                task=self.name,
                ok=False,
                message=f"免费档吃宴席失败：{text}",
                data=result_data,
            )
        return TaskResult(
            task=self.name,
            ok=True,
            message=f"免费档吃宴席成功（未邀请好友、未花钻石）：{text}",
            data=result_data,
        )

    # ------------------------------------------------------------------
    # ⑥ 演练
    # ------------------------------------------------------------------
    def _dry_run(self, game: GameClient) -> TaskResult:
        """演练：只组装"拉数据"这一包，把两种模式与闸门状态完整打出来。"""
        request = game.build(GetBBQDataPacket(), include_defaults=True)
        print(request.describe())
        print()
        tiers = self._load_tiers()
        print("宴席档位（本地配置表 ExtraEnergyData；本项目**只走最大档**）：")
        for tier in tiers or ():
            print(f"  · {tier.describe()}")
        if not tiers:
            print("  （读不到配置表 —— 花钱分支会因此拒绝发送）")
        print()
        authorized = self.spend_policy.allows_purpose(SpendPurpose.BBQ_INVITE)
        print(f"步骤① 邀请好友：{'已授权（最高档）' if authorized else '未授权（将跳过）'}")
        print(f"步骤② 确认吃宴席：{'会发送 15003' if self.eat else '不发 15003（只读）'}")
        print(f"本次消费许可：{self.spend_policy.describe()}")
        print(
            f"邀请人数上限：{config.BBQ_MAX_INVITE_FRIENDS} 人"
            "（好友不足时按人数退档，不会多花钱）"
        )
        print()
        print("真实可邀请的好友与本期 groupID 都要等 32008 响应（本演练不联网）：")
        print("  · 32007 拉数据 → 取本期 groupID + 按人数选档位（仅用于估算费用） → 15003 发送")
        print("—— 以上为演练输出，未发送任何请求 ——")
        return TaskResult(
            task=self.name,
            ok=True,
            message=f"演练完成：32007 请求体 {len(request.body)} 字节，未发送",
            data={
                "body_size": len(request.body),
                "eat": self.eat,
                "invite_authorized": authorized,
                "spend_policy": self.spend_policy.describe(),
            },
        )


@register_task
class BBQStatusTask(BBQEnergyTask):
    """宴席两顿状态的**只读**查询任务（``bbq_status``，2026-10-05 新增）。

    【为什么要有它（而不是给 ``bbq_energy`` 传 ``eat=False``）】
    网页的宴席卡片展开时要联网问"两顿现在什么状态"（``/api/banquet/status``）。
    一开始的做法是跑一次 ``bbq_energy`` 并带上 ``params={"eat": False}`` ——
    但 ``eat`` **不在规格表的参数里**，``normalize_params`` 会把它当未知键丢弃，
    而 runner 对缺失的 ``eat`` 默认按 **True**（"主动任务默认执行即吃"）处理。
    结果：一个"只读"端点会真的发出 15003、**吃掉一顿**。
    与其靠调用方"记得传对参数"，不如造一个**结构上不可能有副作用**的任务 ——
    与 ``main_stage_status`` / ``arena_status`` 同一模式（只读状态任务，隐藏）。

    【只读保证】本任务**只重写** :meth:`execute`：拉一次 32008 就返回，
    ``_eat`` / 邀请 / 记账的代码路径根本不会被走到。
    """

    name = "bbq_status"
    #: 只读数据类任务，没有花钱分支 —— 清空从父类继承的用途声明，
    #: 免得 ``check_preconditions`` 每次都打"该用途未授权"的无效警告。
    diamond_purposes = ()
    opt_in_hint = "宴席状态查询：只读拉取两顿的开放时段与当前状态（零消耗）"

    def execute(self) -> TaskResult:
        """拉一次 32008 → 返回两顿的窗口状态视图（不组装、不发送 15003）。"""
        with self.open_game_client() as game:
            if self.dry_run:
                request = game.build(GetBBQDataPacket(), include_defaults=True)
                return TaskResult(
                    task=self.name,
                    ok=True,
                    message=f"演练完成：32007 请求体 {len(request.body)} 字节，未发送",
                    data={"body_size": len(request.body), "meals": []},
                )
            data = self._fetch(game)
            meals = self._meals_view(data)
            # 文案按第七版口径描述 —— "能不能吃"与"能不能邀请"是两件事。
            parts: list[str] = []
            for item in meals:
                label = str(item["label"])
                state = item["state"]
                if state == "gap":
                    parts.append(f"{label}休整中（06:00–06:10 空档）")
                elif state == "makeup":
                    parts.append(f"{label}可补吃（已过邀请时间）")
                elif state == "ready":
                    parts.append(f"{label}开放中（窗口内可邀请）")
                elif state == "not_started":
                    parts.append(f"{label}未开始")
                elif state == "expired":
                    parts.append(f"{label}已结束")
                else:
                    parts.append(f"{label}状态未知")
            message = (
                "宴席两顿状态：" + "、".join(parts)
                + f"；可邀请好友 {len(data.friend_ids())} 人"
            )
            return TaskResult(
                task=self.name,
                ok=True,
                message=message,
                data={
                    "meals": meals,
                    "friend_count": len(data.friend_ids()),
                    "sent_packets": (32007,),
                },
            )
