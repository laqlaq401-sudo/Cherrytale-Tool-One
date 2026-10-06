"""降临／活动关卡「体力扫荡」—— 把当期开放的活动关卡按体力扫一遍。

【目标是什么】
游戏主界面「降臨活動 / 常駐降臨」下的限时活动区（``SpecialAreaData``）里的关卡
（``SpecialSectionData``）。这些区域**按周轮换**，所以每期的 ``areaID`` / ``sectionID``
都不同 —— 因此本任务**从不缓存关卡号**：每次都重新拉一遍"当期有哪些区、哪些关"。

【为什么能纯协议自动化】
抓包实测（``captures/降临扫荡抓包1.saz``，整份 29 MB 里只有 **4 个网关 POST**）：
``11025``（空包）→ ``11026`` 区域表；``11003{areaID}`` → ``11004`` 关卡表；
``11009{sectionID, times, useSweepTicket}`` → ``11010`` 结算。
**没有任何战斗、结算、存证上传**，扫荡由服务端直接算完（配置表里该关 ``cheatCheck=0``）。
字节级结论见 ``notes/sweep_capture.md``。

【默认只读，四道闸门（都过才会真的扫）】
1. ``--times N``：**显式**给出次数，默认 ``0`` = 只读（只列清单，一发都不扫）；
2. ``config.SWEEP_MAX_TIMES`` 硬上限（防手误把 5 写成 50）；
3. 每关的实际次数再按 **"当前体力 // 该关体力消耗"** 削一次
   —— 这正是实测里真实客户端的行为（点 5 次、体力只够 4 次 → 包里 ``times=4``）。
   ★ 2026-10-05：体力有两个来源，优先用最近一次 ``11010`` 回传的余额，没有时才
   退回扫荡前从只读 ``2007`` 背包读到的**起始快照** —— 后者让**第一个关卡、
   第一次扫荡**也能削峰（旧实现此时体力未知，会发出注定被拒的 ``times=5``）。
4. ★ 2026-10-02：**扫荡券张数**（扫之前用只读的 ``2007 GetAllItemPacket`` 读出来）。
   券 0 张 → **一个 11009 都不发**；券不够 → 向下归到**官方档位**
   （只有 3 张 → 只扫 1 次，而不是扫 3 次）。

【★ 2026-10-04 新增：``--times -1`` = 「扫荡到清空体力」】
需求是"远超 10 次"的扫荡，但客户端**一次操作最多就 5 次**（抓包：一次操作 = 一个
``times=N`` 的包，见 ``notes/sweep_capture.md`` §五）。所以清空模式 = 对**指定关卡**
**反复发 11009**（每轮一个包，轮间隔随机 1~2 秒），直到服务端回"体力不足 / 券不足"为止；
**绝不**把 50 次塞进一个包（那既不是游戏行为、也更惹风控）。
- 必须配合 ``--section``（只对单关清空；未指定则直接失败）；
- 停止条件以服务端为权威；另设 ``SWEEP_UNTIL_EMPTY_MAX_TIMES`` 安全上限。
- ★ 2026-10-05 修订：**每一轮的次数都按当前体力削**
  （``times = min(5, 当前体力 // energyCost)``）—— 这才是实测客户端的行为：
  它每次点按钮都现算（抓包 raw/08 点 1 次、raw/10 因体力只剩 160 而把 5 削成 4）。
  旧实现固定发 ``times=5``，滚到"体力只够 3 次"时仍发 5 → 白挨一发 ``NoEnergy``。
  首轮的体力来自背包快照（见下），之后用每轮响应回传的余额滚动覆盖。

【流程】
::

    11025（空包，只读）→ 11026：打印当期开放区域（带编号，供 --area 选择）
      └─ --area <areaID|编号|名称关键字> 选定目标区
    11003{areaID}（只读）→ 11004：该区的关卡
      └─ 与 SpecialSectionData 求交集 →「可扫清单」+「被挡清单（附原因）」
    2007（空包，只读）→ 2008：数出扫荡券张数（★ 2026-10-02）
      └─ 0 张 → 直接收工（跳过），绝不发 11009
      └─ times = min(--times, SWEEP_MAX_TIMES, 剩余体力//energyCost, 券数归档)
    逐关：11009 → 11010
      · errorCode == 0 → 打印奖励摘要 + 剩余体力/券，继续下一关
      · errorCode != 0 → **立刻收工**，把服务端原话报出来（不猜、不重试）

【屏蔽规则（需求里明确要求的那一条）】
不消耗体力的关卡（``energyCost <= 0``）、配置表禁止扫荡的（``sweepOff != -1``）、
开了反外挂校验的（``cheatCheck != 0``）**一律不扫** —— 唯一权威实现是
``models.game_config.sweep_block_reason()``，本任务只调用它，不另写一份判断。
⚠️ 实测零体力关卡里有 **99 行** 的 ``sweepOff`` 仍是 ``-1``（配置表没挡住），
所以这一层**必须**由客户端负责。

【安全边界】
- 出站消息号白名单：``models.activity_stage.ALLOWED_PACKET_IDS``
  （``2007`` 只读背包 / ``11025`` / ``11003`` / ``11009`` / ``11015`` / ``11017``），
  越界即抛错；买次数的 ``11027`` 等包**连模型都没有建**；
- 扫荡请求有副作用（扣体力与扫荡券）→ **绝不重试**、绝不并发；
- 只读模式下连 ``11009`` 都不会被组装出来；**零券时同样一个 11009 都不发**
  （★ 2026-10-02：先读券后发包，而不是"发出去等被拒"）。
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Final

import config
from client.game_client import GameClient
from client.session import GameSession
from client.spending import SpendPolicy
from models import game_config
from models.activity_stage import (
    ALLOWED_PACKET_IDS,
    FORBIDDEN_PACKET_IDS,
    PACKET_PURPOSE,
    SWEEP_TICKET_ITEM_ID,
    SWEEP_TIME_CHOICES,
    SWEEP_UNTIL_EMPTY,
    ActivityAreaStateClass,
    GetAllActivityStage,
    GetAllActivityStageRes,
    GetSectionByAreaPacket,
    GetSectionByAreaRes,
    SectionStateClass,
    SweepSectionPacket,
    SweepSectionRes,
    describe_error_code,
    normalize_sweep_times,
)
from models.envelope import EnvelopeView
from models.game_config import SpecialSection
from models.game_packet import ProtoMessage
from models.wallet import GetAllItemPacket, GetAllItemRes
from models.main_stage import (
    TEAM_TYPE_PVE1,
    BattleAchiAwakeClass,
    BattleAchiComboClass,
    BattleAchiInterupClass,
    BattleResultClass,
    FightDataClass,
    FightingRequestPacket,
    FightingResponseRes,
    FightingResultPacket,
    FightingResultRes,
    make_battle_server_verify,
    make_fight_verify_token,
)
from services import cancellation
from tasks.base import BaseTask, TaskResult, register_task
from tasks.packet_guard import guarded_send

_LOGGER: Final[logging.Logger] = logging.getLogger("tasks.sweep")

#: ``SectionStateClass.sectionState`` 里代表「**已通关**」的取值。
#:
#: 【实测依据（Q-019）】
#: - 已通关的区域："可疑的工作"（128110053，进度 5/5）9 个关卡全是 ``2``；
#:   抓包里另一个已通关账号的 9 关也全是 ``2``；
#: - 未通关的区域：见 ``notes/sweep_capture.md`` 第十节（2026-09-21 只读实验）。
#:
#: 因为"未通关"的取值只在实验里见过一次，所以这里的护栏是**白名单式**的：
#: 只有 ``sectionState == 2`` 才认为已通关，其余一律不扫（fail-closed）——
#: 万一将来出现新取值（如 3 = 已通关且满星），最多是"少扫"，不会"误扫"。
PASSED_SECTION_STATE: Final[int] = 2

#: 「扫荡到清空体力」每一轮发的次数。固定 **5** —— 就是客户端面板上的
#: ``eSweepType.five``；实测**一次操作 = 一个 ``times=5`` 的包**（见
#: ``notes/sweep_capture.md`` §五：素材抓包 raw/08/10/12 就是连点三次"扫荡 5 次"，
#: 三个形状完全相同的 ``times=5`` 包）。清空模式 = 把这个动作**反复发**，
#: 而不是把 50 次塞进一个包。
UNTIL_EMPTY_ROUND_TIMES: Final[int] = 5

#: 清空模式里「**预期内**的停止码」：命中这些不是程序错误，而是"扫不动了"的
#: 自然收尾（服务端原话由 ``describe_error_code`` 翻出来打印）。
#: 其余错误码（如 ``-6 DataError``）才算任务失败。
UNTIL_EMPTY_NATURAL_STOP: Final[frozenset[int]] = frozenset(
    {
        -1,  # NoClearance 未通关
        -2,  # NoEnergy    体力不足
        -3,  # NoTicket    扫荡券不足
        -7,  # TimesIsFull 次数已满
        -8,  # DailyClose  该活动今日关闭
    }
)


@dataclass(frozen=True)
class SweepCandidate:
    """一个**通过全部屏蔽检查**的候选关卡。"""

    #: 清单里的 1-based 编号（供 ``--section`` 与日志引用）
    index: int
    #: 关卡 ID（``11009`` 的 ``sectionID``）
    section_id: int
    #: 关卡名（配置表 ``sectionName``）
    section_name: str
    #: 每次扫荡消耗的体力
    energy_cost: int
    #: 服务端给的关卡状态（``SectionStateClass.sectionState``）。
    #: ⚠️ 语义未完全确认（见 :data:`PASSED_SECTION_STATE`），本字段**只用于日志**，
    #: 判断在 :meth:`SweepActivityTask._build_candidates` 里做。
    section_state: int
    #: 是否需要首次通关（未通关且勾选了「允许首次通关简单关卡」）
    needs_first_pass: bool = False

    def describe(self) -> str:
        """一行摘要（``1) 關卡1（体力 8｜状态 2）``）。

        【★ 2026-10-05 去掉 section_id】清单里那一串 9 位数字对用户没有意义，
        而它会被逐条打进运行日志、占满篇幅。保留的三样各有用途：
        ``index`` 供 ``--section`` 引用、``section_name`` 是人话、体力与状态解释
        了"这关扫几管血"。ID 仍留在 ``as_view()`` 里（下拉取值要用）。
        """
        tag = "｜需首通" if self.needs_first_pass else f"｜状态 {self.section_state}"
        return f"{self.index}) {self.section_name}（体力 {self.energy_cost}{tag}）"

    def as_view(self) -> dict[str, object]:
        """压成**给界面用的扁平 dict**（网页下拉的候选项）。"""
        return {
            "index": self.index,
            "section_id": self.section_id,
            "name": self.section_name,
            "energy_cost": self.energy_cost,
            "state": self.section_state,
            "passed": self.section_state == PASSED_SECTION_STATE,
            "needs_first_pass": self.needs_first_pass,
        }


@dataclass(frozen=True)
class BlockedSection:
    """被屏蔽的关卡（连同原因，便于用户核对"为什么没扫它"）。"""

    section_id: int
    reason: str
    #: 关卡名（配置表 ``sectionName``）。★ 2026-10-05 新增：``describe()`` 现在
    #: 用名字而不是 ``section_id`` 展示，否则被挡清单只能印一串 9 位数字。
    #: 配置表里查不到这个关卡时留空（那句被挡原因本身已说明"表里没有"）。
    section_name: str = ""

    def describe(self) -> str:
        """一行摘要（``關卡7：不消耗体力``）。

        【★ 2026-10-05 去掉 section_id】理由同 :meth:`SweepCandidate.describe`：
        被挡清单也会逐条进运行日志，裸 ID 只会占篇幅。
        """
        label = self.section_name or "（未知关卡）"
        return f"{label}：{self.reason}"

    def as_view(self) -> dict[str, object]:
        """压成**给界面用的扁平 dict**（被挡关卡 + 原因）。

        【为什么被挡的关卡也要给界面】
        用户点开下拉只看到 5 个候选、而游戏里明明是 9 关时，第一反应是"工具漏了关卡"。
        把被挡关卡连原因一起返回，界面才能显示"另有 4 关被挡（不消耗体力 / 配置表禁止 …）"。
        """
        return {"section_id": self.section_id, "reason": self.reason}


def area_view(area: ActivityAreaStateClass, area_name: str = "") -> dict[str, object]:
    """把一个 ``11026`` 的区域压成**给界面用的扁平 dict**（网页下拉的候选项）。

    :param area: ``11026``（``GetAllActivityStageRes.areaStateList``）里的一项。
    :param area_name: 配置表查到的区域名（``SpecialAreaData.areaName``）；查不到就留空。

    【为什么起止时间要给"文本"而不是时间戳】
    界面要的是"09-23 21:00 → 10-07 21:00"这种能直接读的文字。
    时间戳转文本的逻辑已经写在 :class:`ActivityAreaStateClass` 的
    ``start_text`` / ``end_text`` 属性里（含时区 ``DISPLAY_TZ``），
    这里复用属性而不是再转一次 —— 两处各转一次迟早会不一致。
    """
    return {
        "area_id": area.areaID,
        "name": area_name,
        "progress": area.progressStr or "",
        "passed_count": area.passedCount,
        "stars": area.areaStars,
        "start": area.start_text,
        "end": area.end_text,
        "weeks": area.weeks,
        "state": area.sectionState,
    }


@register_task
class SweepActivityTask(BaseTask):
    """降临/活动关卡体力扫荡（默认只读）。

    :param area: 目标区域选择器（``areaID`` / 清单编号 / 名称关键字）；``None`` = 只列清单。
    :param times: 每个关卡最多扫几次（``0`` = 只读）。
        另有一个哨兵值 ``-1`` = **「扫荡到清空体力」**（``SWEEP_UNTIL_EMPTY``）：
        对**指定关卡**反复发「扫荡 5 次」直到服务端回体力/券不足。
        该模式**必须**配合 ``section`` 使用，否则直接失败。
    :param section: 只扫指定的一个关卡（``sectionID`` / 候选清单编号）；``None`` = 全部候选。
    :param only_passed: 是否**只扫已通关的关卡**（默认 ``True``）。
        未通关的关卡会被列出来但**不扫**（见 :data:`PASSED_SECTION_STATE`）；
        确实想扫它们必须显式 ``--allow-unpassed``。
    """

    name = "sweep_activity"

    #: 需要登录态（这三个包都要带会话令牌）
    requires_auth = True

    #: 本任务**不存在**花钻石的路径：买次数的 ``11027`` 连模型都没建。
    #: 显式声明是为了让 ``main.py tasks`` 的输出如实反映"这个任务不花钱"。
    spends_diamond = False

    def __init__(
        self,
        session: GameSession,
        *,
        logger: logging.Logger | None = None,
        dry_run: bool = False,
        spend_policy: SpendPolicy | None = None,
        area: str | None = None,
        times: int = 0,
        section: str | None = None,
        only_passed: bool = True,
        allow_unpassed: bool | None = None,
        prefetch_sections: bool = False,
    ) -> None:
        super().__init__(
            session, logger=logger, dry_run=dry_run, spend_policy=spend_policy
        )
        #: 是否在只读发现开放区域时，顺便预拉取所有区域的关卡候选
        self.prefetch_sections = bool(prefetch_sections)
        #: 目标区域选择器（原样保存，**等到有清单时**再解析 —— 那时出错才能列出候选）
        self.area_selector = (area or "").strip()
        #: ★ 2026-10-04：「扫荡到清空体力」模式（哨兵 ``-1``）。
        #:
        #: 【为什么必须先判断哨兵】
        #: ``normalize_sweep_times(-1)`` 会把 ``<= 0`` 当成"不扫"返回 ``0`` —— 若不
        #: 先拦截，``-1`` 会被**静默吞成"只读"**（看起来像"什么都没发生"）。
        #: 同理，下面的 ``max(..., 0)`` 也会把 -1 变成 0，所以先算 ``until_empty``。
        self.until_empty = int(times) == SWEEP_UNTIL_EMPTY
        #: 用户要求的次数（``0`` = 只读；清空模式下固定为 0，真正的次数在循环里逐轮算）
        self.requested_times = 0 if self.until_empty else max(int(times), 0)
        #: 只扫某一个关卡的选择器（``""`` = 全部候选）
        self.section_selector = (section or "").strip()
        #: 是否只扫**已通关**的关卡（默认 ``True``）。
        #: 若勾选「允许首次通关简单关卡」（allow_unpassed=True），则 only_passed=False。
        if allow_unpassed is not None:
            self.only_passed = not bool(allow_unpassed)
        else:
            self.only_passed = bool(only_passed)
        #: 本次**实际发出**的消息号（测试用它断言"没有越界发包"）
        self.sent_packet_ids: list[int] = []
        #: 最近一次响应里的**剩余体力**（``None`` = 还不知道）。
        #:
        #: 【为什么要有这个滚动状态】
        #: 扫荡响应里正好带着剩余体力（``11010.costList`` 的第一件，见
        #: ``models/activity_stage.py``）。每扫一次就用**最新体力**再削一次 ——
        #: 复刻了真实客户端"按体力削次数"的行为。它是**滚动值**：每次都覆盖。
        self.energy_left: int | None = None
        #: **扫荡开始前**从背包（只读 2007 / 2008）读到的体力快照（``None`` = 还没读）。
        #:
        #: 【★ 2026-10-05：为什么还要这一份"快照"】
        #: 只有 :attr:`energy_left` 时，**第一个关卡、第一次扫荡**永远拿不到体力
        #: （响应还没回来）—— 体力剩 3、每关耗 8 → 依旧发 ``times=5`` → 服务端回
        #: ``NoEnergy = -2`` → 白扫。而这正是真实客户端**绝不会**发生的事：它本地就
        #: 持有体力，发之前就算好 ``times = min(档位, 体力 // energyCost)``
        #: （抓包实测：点 5 次、体力只够 4 次 → 包里 ``times=4``，见
        #: ``notes/sweep_capture.md`` §五）。
        #: 背包里本来就有体力（``GetAllItemRes.stamina()``），而数扫荡券的
        #: ``2007`` 请求**已经在本任务里发了** —— 于是顺手把它读出来，**零新增发包**。
        #: 它只是"起始快照"，会被每次响应的 :attr:`energy_left` 覆盖而失效。
        self.stamina_snapshot: int | None = None
        #: 最近一次知道的**扫荡券持有量**（``None`` = 还不知道）。
        #:
        #: 【★ 2026-10-02：券也必须成为一道闸门】
        #: 每扫 1 次固定扣 1 张券（``SWEEP_TICKET_ITEM_ID``）。过去只在被服务端
        #: 拒绝（``NoTicket = -3``）之后才知道券不够 —— 那已经是"发过一次注定失败
        #: 的包"了。现在改成：扫之前先用 **2007（只读）** 把券数读出来，
        #: 0 张就**一个 11009 都不发**；券少于计划次数就向下归到**官方档位**
        #: （1 / 5 / 10，见 ``normalize_sweep_times``）。
        self.ticket_left: int | None = None

    # ------------------------------------------------------------------
    # 发包：一律走公共守卫（tasks/packet_guard.py）
    # ------------------------------------------------------------------
    def _guarded_send(
        self, game: GameClient, packet: ProtoMessage, expect: type[ProtoMessage]
    ) -> EnvelopeView:
        """发送一个**白名单内**的包。

        ``include_defaults=True``：实测客户端会把"值为 0 的字段"也带上
        （荣耀之巅的 ``39011`` 就是这样，见 ``notes/glory_summit_capture.md`` 第九节）。
        本板块三个包字段都很少，照抄"全字段"最贴近真实客户端。
        """
        return guarded_send(
            game,
            packet,
            expect,
            allowed=ALLOWED_PACKET_IDS,
            forbidden=FORBIDDEN_PACKET_IDS,
            record=self.sent_packet_ids,
            purpose=PACKET_PURPOSE,
            logger=self.logger,
            include_defaults=True,
        )

    # ------------------------------------------------------------------
    # 只读发现：当期区域 / 该区关卡
    # ------------------------------------------------------------------
    def _fetch_areas(self, game: GameClient) -> GetAllActivityStageRes:
        """``11025``（空包）→ ``11026``：当期开放的活动区域。**零消耗**。"""
        view = self._guarded_send(game, GetAllActivityStage(), GetAllActivityStageRes)
        return view.decode_sub_packet(GetAllActivityStageRes)

    def _fetch_sections(self, game: GameClient, area_id: int) -> GetSectionByAreaRes:
        """``11003{areaID}`` → ``11004``：该区的关卡列表。**零消耗**。"""
        view = self._guarded_send(
            game, GetSectionByAreaPacket(areaID=area_id), GetSectionByAreaRes
        )
        return view.decode_sub_packet(GetSectionByAreaRes)

    def _fetch_ticket_count(self, game: GameClient) -> int:
        """``2007 GetAllItemPacket``（空包）→ ``2008``：数出背包里的**扫荡券**。

        顺带把**体力**一并读出来（★ 2026-10-05）—— 同一个响应里就有
        （``GetAllItemRes.stamina()``），所以这是**零新增发包**的顺手之举。
        体力存进 :attr:`stamina_snapshot`，供**首轮削峰**使用（否则第一个关卡、
        第一次扫荡永远拿不到体力，见该字段的说明）。

        :return: 当前持有的扫荡券张数（背包里一条都没有时返回 ``0``）。

        【为什么这一步必须在发 11009 之前做】
        每扫 1 次固定扣 1 张券。没有这一步，就只能在服务端回
        ``NoTicket = -3`` 之后才知道券不够 —— 那次请求已经发出去了：
        既白扣了一次风控暴露，也让用户看到一条"莫名其妙的失败"。
        先读后发，才能做到"没券就一个字节都不发"。

        .. note::
           这是**只读**包（``models/wallet.py`` 里没有建模任何写背包的报文），
           所以即使读错也只会导致"少扫"，不会"多扣"。
        """
        view = self._guarded_send(game, GetAllItemPacket(), GetAllItemRes)
        res = view.decode_sub_packet(GetAllItemRes)
        self.ticket_left = max(int(res.amount_of(SWEEP_TICKET_ITEM_ID)), 0)
        self.logger.info(
            "扫荡券：当前持有 %d 张（每扫 1 次扣 1 张）",
            self.ticket_left,
        )
        # ★ 2026-10-05：顺手读体力（同一个 2008 响应，零新增发包）。
        # 只在"还不知道"时播种：已经有过 11010 回传的滚动值就不覆盖它
        # （滚动值比这份快照新）。
        stamina = max(int(res.stamina()), 0)
        if self.energy_left is None:
            self.stamina_snapshot = stamina
            self.logger.info("体力：当前持有 %d（来自背包快照，供首轮削峰）", stamina)
        else:
            self.logger.debug(
                "体力：背包快照 %d（已被更早的响应回传值 %s 覆盖，忽略）",
                stamina,
                self.energy_left,
            )
        # 裸 itemID 属于开发者日志（运行视图不出现标识符）
        self.logger.debug("扫荡券 itemID=%d", SWEEP_TICKET_ITEM_ID)
        return self.ticket_left

    def current_energy(self) -> int | None:
        """当前**可用于削峰**的体力值（``None`` = 完全未知）。

        【取哪个】
        优先用 :attr:`energy_left`（最近一次 ``11010`` 响应回传的**刷新后**余额，
        永远最新）；没有时才退回 :attr:`stamina_snapshot`（扫荡开始前从背包
        ``2007`` 读到的**起始**快照）。

        【为什么不能只用快照】
        快照是"扫之前"的，多轮扫荡后它早就过期了 —— 用它会**高估**体力、
        发出注定被拒的包。所以只要拿到过一次响应值，就再也不用快照。

        :return: ``(体力值 或 None)``；两个来源都没有时返回 ``None``
            （调用方按"未知 = 不削"处理，与改造前行为一致）。
        """
        if self.energy_left is not None:
            return self.energy_left
        return self.stamina_snapshot

    # ------------------------------------------------------------------
    # 选择器解析（三段规则，**写清楚以免靠猜**）
    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_area(
        selector: str,
        areas: list[ActivityAreaStateClass],
        area_names: dict[int, str],
    ) -> tuple[int | None, str]:
        """把 ``--area`` 的选择器解析成 ``areaID``。

        依次尝试三种写法（**先精确、后模糊**）：

        1. **areaID**：如 ``128110053``（与当期清单里的某个 ID 完全相同）；
        2. **清单编号**：如 ``3``（对应任务打印出来的第 3 行，从 1 开始）；
        3. **名称关键字**：如 ``可疑`` / ``英雄降臨``（对区域名做**不区分大小写**的子串匹配）。

        :return: ``(areaID, 说明)``。无法唯一确定时 ``areaID`` 为 ``None``，
            第二个值就是给用户看的错误原因（会列出可选清单）。
        """
        text = selector.strip()
        if not text:
            return None, "没有指定区域（用 --area <areaID|编号|名称关键字>）"

        # ① areaID 精确匹配
        for area in areas:
            if str(area.areaID) == text:
                return area.areaID, f"按 areaID 匹配到 {area.areaID}"

        # ② 清单编号（从 1 开始）
        if text.isdigit():
            position = int(text)
            if 1 <= position <= len(areas):
                area = areas[position - 1]
                return area.areaID, f"按清单编号 {position} 匹配到 {area.areaID}"

        # ③ 名称关键字
        lowered = text.lower()
        hits = [
            area
            for area in areas
            if lowered in (area_names.get(area.areaID, "") or "").lower()
        ]
        if len(hits) == 1:
            return hits[0].areaID, f"按名称关键字「{text}」匹配到 {hits[0].areaID}"
        if len(hits) > 1:
            listed = "、".join(
                f"{area.areaID} {area_names.get(area.areaID, '')}" for area in hits[:5]
            )
            return None, (
                f"名称关键字「{text}」匹配到 {len(hits)} 个区域，无法确定是哪一个："
                f"{listed} —— 请改用 areaID 或清单编号"
            )
        return None, f"没有任何区域的 ID / 编号 / 名称能匹配「{text}」，请先看上面的清单"

    def _resolve_section(
        self, selector: str, candidates: list[SweepCandidate]
    ) -> tuple[SweepCandidate | None, str]:
        """把 ``--section`` 解析成某个候选关卡。

        规则与 :meth:`_resolve_area` 同源（先 ID、后编号），但**只在候选里找** ——
        被屏蔽的关卡不在候选里，所以你不会误扫一个被挡掉的关卡。

        :return: ``(候选, 说明)``；无法确定时第一个值为 ``None``（第二个值是原因）。
        """
        text = selector.strip()
        if not text:
            return None, ""

        for candidate in candidates:
            if str(candidate.section_id) == text:
                return candidate, f"按 sectionID 匹配到「{candidate.section_name}」"
        if text.isdigit():
            position = int(text)
            if 1 <= position <= len(candidates):
                candidate = candidates[position - 1]
                return candidate, f"按候选编号 {position} 匹配到「{candidate.section_name}」"
        # 【★ 2026-10-05 去掉 section_id】错误提示里原本列的是前几个候选的裸 ID，
        # 用户拿到也认不出是哪一关；改成列关卡名，才对得上弹窗里看到的清单。
        listed = "、".join(candidate.section_name for candidate in candidates[:5])
        return None, (
            f"没有候选关卡的 ID / 编号能匹配「{text}」（前几个候选：{listed}…）"
            " —— 注意：被屏蔽的关卡不在候选里"
        )

    # ------------------------------------------------------------------
    # 候选清单：与配置表求交集 + 屏蔽判断
    # ------------------------------------------------------------------
    def _build_candidates(
        self,
        section_states: list[SectionStateClass],
        section_index: dict[int, SpecialSection],
        area_id: int | None = None,
    ) -> tuple[list[SweepCandidate], list[BlockedSection]]:
        """把 ``11004`` 返回的关卡分成「可扫」与「被挡」两组。

        【为什么要跟配置表求交集，而不是直接信服务端】
        服务端只告诉你"这个区有哪些关"，不告诉你"这关该不该扫"：
        实测有 **99 个不消耗体力的关卡**在配置表里并没有禁止扫荡（``sweepOff=-1``），
        而它们重复通关毫无意义 —— 需求明确要求把这类挡掉。
        所以配置表侧的唯一权威判断是 :func:`models.game_config.sweep_block_reason`。

        【为什么要检查"已通关"（2026-09-21 加）】
        扫荡一个**没打过**的关卡等于花体力跳过首次通关体验，玩家通常不想要；
        而服务端**不会**替你挡（它只按 ``sectionID`` 结算）。
        判据是响应里的 ``sectionState``，取值实测见 :data:`PASSED_SECTION_STATE`。
        护栏默认开启（``only_passed=True``），要扫未通关关卡得显式
        ``--allow-unpassed`` —— 与"默认只读"同一套 fail-closed 哲学。

        :param section_states: ``11004`` 给出的关卡状态（保持服务端顺序）。
        :param section_index: ``{sectionID: SpecialSection}``。
        :param area_id: 区域 ID。若勾选「允许首次通关简单关卡」，将从配置表中补齐本区域的未解锁后续关卡。
        """
        candidates: list[SweepCandidate] = []
        blocked: list[BlockedSection] = []

        known_sections: list[tuple[int, int]] = []
        seen_ids: set[int] = set()
        for state in section_states:
            known_sections.append((state.sectionID, state.sectionState))
            seen_ids.add(state.sectionID)

        # 仅在只读预览发现模式（requested_times <= 0）且勾选了允许首通（only_passed=False）时：
        # 从本地配置表中补齐本区域尚未解锁的后续关卡（初始为未通关 sectionState=0），供网页端完整展现所有关卡候选
        if area_id is not None and not self.only_passed and self.requested_times <= 0:
            stamina_sections = game_config.area_stamina_sections(area_id, section_index)
            for sec in stamina_sections:
                if sec.section_id not in seen_ids:
                    known_sections.append((sec.section_id, 0))
                    seen_ids.add(sec.section_id)
            known_sections.sort(key=lambda item: item[0])

        for section_id, section_state in known_sections:
            section = section_index.get(section_id)
            if section is None:
                blocked.append(
                    BlockedSection(
                        section_id,
                        "本地配置表里没有这个关卡"
                        "（游戏可能刚更新过配置表，请重新解包 TextAsset）",
                    )
                )
                continue
            reason = game_config.sweep_block_reason(section)
            if reason is not None:
                blocked.append(
                    BlockedSection(section_id, reason, section.section_name or "")
                )
                continue
            if section_state != PASSED_SECTION_STATE:
                if self.only_passed:
                    blocked.append(
                        BlockedSection(
                            section_id,
                            f"未通关（sectionState={section_state}，"
                            f"已通关是 {PASSED_SECTION_STATE}）"
                            "—— 若需自动攻打请勾选「允许首次通关简单关卡」",
                            section.section_name or "",
                        )
                    )
                    continue
                # 用户开启了「允许首次通关简单关卡」（only_passed=False）
                if section.cheat_check != 0:
                    blocked.append(
                        BlockedSection(
                            section_id,
                            f"未通关且开启了反外挂校验（cheatCheck={section.cheat_check}），"
                            "严禁自动首通，请在真机手动通关",
                            section.section_name or "",
                        )
                    )
                    continue
                # 简单关卡（cheatCheck=0）：加入候选，标记需首通
                candidates.append(
                    SweepCandidate(
                        index=len(candidates) + 1,
                        section_id=section_id,
                        section_name=section.section_name or "<无名>",
                        energy_cost=section.energy_cost,
                        section_state=section_state,
                        needs_first_pass=True,
                    )
                )
                continue
            candidates.append(
                SweepCandidate(
                    index=len(candidates) + 1,
                    section_id=section_id,
                    section_name=section.section_name or "<无名>",
                    energy_cost=section.energy_cost,
                    section_state=section_state,
                    needs_first_pass=False,
                )
            )
        return candidates, blocked

    # ------------------------------------------------------------------
    # 次数：三道闸门取最小
    # ------------------------------------------------------------------
    def _plan_times(self, candidate: SweepCandidate) -> tuple[int, str]:
        """算出这个关卡**实际要扫几次**，以及被削减的原因说明。

        五道闸门（依次收窄，取最小）：

        1. **档位归一**：客户端只有 1 / 5 / 10 三档（``StageModule.eSweepType``），
           用户给的 7 会被向下归到 5 —— 见
           :func:`models.activity_stage.normalize_sweep_times`；
        2. ``config.SWEEP_MAX_TIMES`` 硬上限（默认 10 = 客户端最高档，防手误）；
        3. **体力** // 该关每次体力消耗 —— 实测客户端就是这么把 5 削成 4 的。
           取值见 :meth:`current_energy`：优先用最近一次响应回传的余额，
           没有时才退回扫荡前从背包读到的**起始快照**（★ 2026-10-05：正是它让
           **第一个关卡、第一次扫荡**也能削峰，而不是先发一个注定被拒的
           ``times=5``）；
        4. ★ 2026-10-02 新增：**扫荡券张数**（``self.ticket_left``，扫之前用
           2007 读出来）。券不够时**不是随便取个更小的数字**，而是再次向下归到
           官方档位：只有 3 张券时 → 只扫 1 次（不是 3 次）；0 张 → 一次都不扫。
        5. 结果为 0 → 这一关直接跳过。

        :return: ``(次数, 说明)``；次数为 ``0`` 表示"这一关别扫了"。
        """
        limit, choice_note = normalize_sweep_times(self.requested_times)
        notes: list[str] = [choice_note] if choice_note else []
        if limit > config.SWEEP_MAX_TIMES:
            notes.append(
                f"被硬上限 {config.SWEEP_MAX_TIMES} 削（你要求 {limit}；"
                f"改环境变量 {config.SWEEP_MAX_TIMES_ENV_NAME} 可调整）"
            )
            limit = config.SWEEP_MAX_TIMES
        energy = self.current_energy()
        if energy is not None:
            if candidate.energy_cost <= 0:  # 理论上进不了候选，纯防御
                affordable = 0
            else:
                affordable = energy // candidate.energy_cost
            if affordable < limit:
                notes.append(
                    f"体力剩 {energy}、每关 {candidate.energy_cost} "
                    f"→ 最多 {affordable} 次"
                )
                limit = affordable
        limit = self._cap_by_tickets(limit, notes)
        return max(limit, 0), "；".join(notes)

    def _cap_by_tickets(self, limit: int, notes: list[str]) -> int:
        """用**扫荡券张数**再削一次次数（★ 2026-10-02 新增的第四道闸门）。

        :param limit: 上一道闸门算出的次数。
        :param notes: 说明文字列表（本方法会往里追加一句"为什么被削"）。
        :return: 削完之后的次数。

        【为什么要"再归一次档位"，而不是直接 ``min(limit, 券数)``】
        客户端面板只有 1 / 5 / 10 三个按钮，用户点不出"3 次"。
        既然本项目的基线是"镜像真实客户端"，那么券只够 3 张时该做的是
        **能选的最大档位** = 1（而不是自己发明一个 3）。
        :func:`normalize_sweep_times` 正是那个"向下归档"的唯一实现，
        这里直接复用它，避免第二份档位逻辑。

        【``ticket_left is None`` 时为什么不拦】
        只有在"用户要求真扫"之后才会去读券（见 :meth:`execute`），
        所以真扫路径上它一定不是 ``None``；``None`` 只会出现在
        单测直接调用 :meth:`_plan_times` 的场景里 —— 那时按"未知"处理、不额外削，
        与改造前的行为保持一致。
        """
        if self.ticket_left is None:
            return limit
        if self.ticket_left < limit:
            capped, cap_note = normalize_sweep_times(self.ticket_left)
            notes.append(
                f"扫荡券只有 {self.ticket_left} 张 → 最多 {capped} 次"
                + (f"（{cap_note}）" if cap_note else "")
            )
            limit = capped
        return limit

    # ------------------------------------------------------------------
    # 扫荡一个关卡（**有副作用**）
    # ------------------------------------------------------------------
    def _sweep_once(
        self, game: GameClient, candidate: SweepCandidate, times: int
    ) -> SweepSectionRes:
        """对一个关卡发出扫荡，并把响应里的剩余体力记下来（滚动削次数）。

        【为什么绝不重试】
        这是一条**有副作用**的请求（扣体力 + 扣扫荡券）。超时后重发等于
        "可能扫了两遍、扣两次资源"，所以本方法只发一次，异常直接抛给上层
        （基类会翻译成失败结果，不会偷偷重发）。
        """
        view = self._guarded_send(
            game,
            SweepSectionPacket(
                sectionID=candidate.section_id,
                times=times,
                useSweepTicket=config.SWEEP_USE_TICKET_VALUE,
            ),
            SweepSectionRes,
        )
        res = view.decode_sub_packet(SweepSectionRes)
        if res.energy_left is not None:
            self.energy_left = res.energy_left
        # ★ 2026-10-02：券也要**滚动更新**，否则第二个关卡会拿"扫第一关之前的券数"
        # 去算，等于多扫（服务端会拒，但那就又变成"发了一次注定失败的包"）。
        # 优先用响应回传的真实剩余量；服务端没给时按"每扫 1 次扣 1 张"自己减。
        reported_ticket = res.sweep_ticket_left
        if reported_ticket is not None:
            self.ticket_left = max(int(reported_ticket), 0)
        elif self.ticket_left is not None:
            self.ticket_left = max(self.ticket_left - int(times), 0)
        return res

    def _first_pass_once(
        self, game: GameClient, candidate: SweepCandidate
    ) -> tuple[bool, FightingResultRes | None, str]:
        """对简单未通关关卡（cheatCheck=0）执行一次战斗模拟并上报三星首通。

        【安全隔离说明】
        本方法仅在用户显式勾选「允许首次通关简单关卡」且关卡未通关时触发；
        对于任何已通关关卡的正常扫荡，绝不调用此方法、绝不上报战斗存证。
        """
        req = FightingRequestPacket(sectionID=candidate.section_id, teamID=TEAM_TYPE_PVE1)
        req_view = self._guarded_send(game, req, FightingResponseRes)
        req_res = req_view.decode_sub_packet(FightingResponseRes)
        if not req_res.is_success() or req_res.fightToken == 0:
            return False, None, f"开战请求失败：errorCode={req_res.errorCode}"

        fight_token = req_res.fightToken
        time_cost_sec = 0 if self.dry_run else random.randint(15, 22)
        if time_cost_sec > 0:
            self.logger.info("关卡 %s 首次通关战斗中，模拟真实耗时 %d 秒...", candidate.section_name, time_cost_sec)
            cancellation.sleep_interruptible(time_cost_sec)

        verify_token = make_fight_verify_token(fight_token, time_cost_sec, win_lose=1)
        server_verify = make_battle_server_verify(candidate.section_id)

        result_packet = FightingResultPacket(
            fightData=FightDataClass(
                token=fight_token,
                sectionId=candidate.section_id,
            ),
            winLose=1,
            starCount=[1, 1, 1],
            timeCostSec=time_cost_sec,
            ComboRecord=BattleAchiComboClass(totalComboCount=0, killBossCount=0),
            InterupRecord=BattleAchiInterupClass(totalInterupCount=1),
            AwakeRecord=BattleAchiAwakeClass(totalInterupCount=0),
            battleResult=BattleResultClass(
                teamId=TEAM_TYPE_PVE1,
                fightingPower=0,
                damageContent="",
                damage=0,
                yimoLv=-1,
                myTeamRecord=[],
                enemyTeamRecord=[],
                battleServerVerify=server_verify,
            ),
            token=verify_token,
        )
        res_view = self._guarded_send(game, result_packet, FightingResultRes)
        result_res = res_view.decode_sub_packet(FightingResultRes)
        if not result_res.is_success():
            return False, result_res, f"首通结算失败：errorCode={result_res.errorCode}"

        if result_res.energy_left is not None:
            self.energy_left = result_res.energy_left

        return True, result_res, "首通三星成功"

    # ------------------------------------------------------------------
    # 「扫荡到清空体力」：反复发「扫荡 5 次」，直到服务端拒绝
    # ------------------------------------------------------------------
    def _affordable_times(self, candidate: SweepCandidate) -> int:
        """当前体力**还能扫几次**（砍到整数次）。

        :return: 体力已知时返回 ``current_energy // energy_cost``；
            **体力未知时返回一个"不设限"的大数**（调用方再与档位 5 取最小，
            所以等价于"按档位发"）。未知时返回大数而不是 0 —— 否则会在
            "读不到体力"的情况下把整个循环卡死（失败方向错成"一次都不扫"）。
        """
        energy = self.current_energy()
        if energy is None:
            return UNTIL_EMPTY_ROUND_TIMES
        if candidate.energy_cost <= 0:  # 理论上进不了候选，纯防御
            return 0
        return energy // candidate.energy_cost

    def _empty_reason(self, candidate: SweepCandidate) -> str:
        """本地算出"一次都扫不起"时，给循环的收尾原因（服务端还没说话）。"""
        energy = self.current_energy()
        if energy is None:
            return "无需继续（本轮可扫次数为 0）"
        return (
            f"体力不足（当前 {energy}，每关 {candidate.energy_cost}，"
            "不足一次），已停止：未发送注定被拒的包"
        )

    def _sweep_until_empty(
        self, game: GameClient, candidate: SweepCandidate
    ) -> TaskResult:
        """对**指定关卡**反复发扫荡，直到体力清空 / 券用尽。

        【执行方式（2026-10-05 修订）】
        每一轮发**一个** 11009，然后按 ``config.sweep_interval()``（随机 1~2 秒）
        间隔后再发下一轮。**每一轮的次数都按当前体力削**：

        ::

            首轮：running_energy = 背包快照（2007 读到的体力）
            每轮：round_times = min(5, running_energy // energyCost, 安全余额)
                  发 11009(times=round_times)
                  成功后 running_energy = 响应回传的剩余体力（滚动覆盖）

        【★ 为什么"每轮都要削"而不是"固定发 5"】
        这是**实测客户端的真实行为**：它每次点按钮都现算
        ``times = min(档位, 当前体力 // energyCost)``。抓包铁证 —— 同一关，
        第一次点 1 次（体力 200）、第二次点 5 次（体力已 160 → ``160//40=4``）
        各发各的包（``notes/sweep_capture.md`` §五 / raw/08 与 raw/10）。
        旧实现固定发 ``times=5``，滚到"只剩 120 点（每关 40 → 只够 3 次）"时
        仍会发 5 → 服务端回 ``NoEnergy = -2`` → 白扫一轮。现在改为发 3。

        【停止条件】
        仍以服务端为权威：收到非 0 错误码（``-2`` 体力不足 / ``-3`` 券不足 /
        ``-7`` 次数已满 …）就停、如实打印。区别只在于**本地不再发"明知会被拒"
        的包** —— 算得 ``round_times <= 0`` 时直接收尾（与"零券时一个 11009
        都不发"同一原则，见 :meth:`execute`）。第一轮若连 1 次都扫不起，
        就不会发出任何 11009，收尾原因直接写明"体力不足"。

        【安全上限】``config.SWEEP_UNTIL_EMPTY_MAX_TIMES``（默认 200 = 2000 / 10）
        兜住"服务端异常、永远回成功"的极端情况。

        .. warning::
           每一轮都是**有副作用**的请求（扣体力 + 扣券），所以绝不重试、绝不并发。
        """
        # 真扫前先读券（只读 2007）—— 与普通模式同一道闸门；0 张则一个 11009 都不发。
        # ★ 2026-10-05：这一次同时把体力读进 self.stamina_snapshot（零新增发包）。
        ticket_left = self._fetch_ticket_count(game)
        if ticket_left <= 0:
            msg = (
                "扫荡券不足（当前持有 0 张），拒绝执行扫荡："
                "本次不会发送任何 11009（每扫 1 次扣 1 张券，先领券再来）"
            )
            self.logger.warning("%s", msg)
            return TaskResult(
                task=self.name,
                ok=True,
                message=msg,
                data={
                    "mode": "no_ticket",
                    "section_id": candidate.section_id,
                    "ticket_left": 0,
                    "sent_packets": tuple(self.sent_packet_ids),
                },
            )

        self.logger.info(
            "扫荡到清空体力：对关卡「%s」反复执行「扫荡 %d 次」，"
            "**每轮按当前体力削峰**（安全上限 %d 次、轮间隔随机 %.1f~%.1fs）",
            candidate.section_name or "该关卡",
            UNTIL_EMPTY_ROUND_TIMES,
            config.SWEEP_UNTIL_EMPTY_MAX_TIMES,
            config.SWEEP_INTERVAL_MIN_SECONDS,
            config.SWEEP_INTERVAL_MAX_SECONDS,
        )

        rounds: list[tuple[int, SweepSectionRes]] = []
        total = 0
        #: 循环因"撞安全上限"而结束时用这句；被服务端拒绝时会被覆盖成服务端原话。
        reason = f"已达到安全上限 {config.SWEEP_UNTIL_EMPTY_MAX_TIMES} 次，已停止"
        while True:
            # 每一轮都是安全的中止点（用户点「停止执行」后最多再扫完手上这一轮）。
            cancellation.raise_if_cancelled()
            if total >= config.SWEEP_UNTIL_EMPTY_MAX_TIMES:
                break
            # ★ 每轮削峰：固定档位 5、当前体力可支撑的次数、安全余额，三者取最小。
            round_times = min(
                UNTIL_EMPTY_ROUND_TIMES,
                self._affordable_times(candidate),
                config.SWEEP_UNTIL_EMPTY_MAX_TIMES - total,
            )
            if round_times <= 0:
                # 本地就能算出"一次都扫不起" → 不再发注定被拒的包，直接收尾。
                reason = self._empty_reason(candidate)
                break
            res = self._sweep_once(game, candidate, round_times)
            rounds.append((round_times, res))
            self.logger.info(
                "第 %d 轮：扫 %d 次 → %s",
                len(rounds),
                round_times,
                res.cost_summary(),
            )
            if not res.is_success:
                reason = (
                    f"服务端返回错误码 {res.errorCode}"
                    f"（{describe_error_code(res.errorCode)}），已停止（不重试）"
                )
                break
            total += round_times
            cancellation.sleep_interruptible(config.sweep_interval())

        last = rounds[-1][1] if rounds else None
        # 三种收尾都算成功：
        #   ① 本地判定"体力不足一次"而主动收尾（``last`` 要么是上一轮的成功、要么不存在）；
        #   ② 最后一轮**业务成功**（例如撞了安全上限，正常停下）；
        #   ③ 命中「体力不足 / 券不足 / 次数已满」等**预期内**的停止码
        #      （见 ``UNTIL_EMPTY_NATURAL_STOP``）。
        # 只有"最后一轮回了非预期错误码"才算失败。
        ok = (
            last is None
            or last.is_success
            or last.errorCode in UNTIL_EMPTY_NATURAL_STOP
        )
        message = (
            f"清空体力完成：对「{candidate.section_name or '该关卡'}」共扫 {total} 次"
            f"（{len(rounds)} 轮，每轮按体力削峰、上限 {UNTIL_EMPTY_ROUND_TIMES} 次）："
            f"{reason}"
            + (
                f"（剩余体力 {self.energy_left}）"
                if self.energy_left is not None
                else ""
            )
        )
        self.logger.info("%s", message)
        return TaskResult(
            task=self.name,
            ok=ok,
            message=message,
            data={
                "mode": "sweep_until_empty",
                "section_id": candidate.section_id,
                "rounds": len(rounds),
                "times_total": total,
                "energy_left": self.energy_left,
                "ticket_left": self.ticket_left,
                "stop_reason": reason,
                "last_error_code": (last.errorCode if last is not None else None),
                "packets": [
                    {"round": index + 1, "times": times, "error_code": res.errorCode}
                    for index, (times, res) in enumerate(rounds)
                ],
                "sent_packets": tuple(self.sent_packet_ids),
            },
        )

    # ------------------------------------------------------------------
    # 演练（--dry-run）
    # ------------------------------------------------------------------
    def _dry_run(self) -> TaskResult:
        """演练：**不联网、不发包**，只把"真跑会发生什么"讲清楚。"""
        if self.until_empty:
            # 清空模式没有"每关次数"这个概念，改报循环参数。
            return self._dry_run_until_empty()
        planned, choice_note = normalize_sweep_times(self.requested_times)
        planned = min(planned, config.SWEEP_MAX_TIMES)
        self.logger.info("演练模式：不会联网，也不会发送任何报文")
        self.logger.info(
            "白名单：%s（2007 数扫荡券 / 11025 拉区域 / 11003 拉关卡 / 11009 扫荡）",
            sorted(ALLOWED_PACKET_IDS.values()),
        )
        self.logger.info(
            "参数：area=%s、times=%d、section=%s",
            self.area_selector or "<未指定>",
            self.requested_times,
            self.section_selector or "<未指定>",
        )
        self.logger.info(
            "闸门：config.SWEEP_MAX_TIMES=%d；useSweepTicket=%d；"
            "屏蔽规则 = energyCost<=0 或 sweepOff!=-1 或 cheatCheck!=0",
            config.SWEEP_MAX_TIMES,
            config.SWEEP_USE_TICKET_VALUE,
        )
        self.logger.info(
            "扫荡券闸门（★ 2026-10-02）：真跑前先用 2007 读背包，"
            "0 张则一个 11009 都不发；不足时向下归到官方档位 %s",
            list(SWEEP_TIME_CHOICES),
        )
        self.logger.info(
            "通关护栏：%s（已通关判据 sectionState=%d）",
            "只扫已通关关卡"
            if self.only_passed
            else "允许扫未通关关卡（--allow-unpassed）",
            PASSED_SECTION_STATE,
        )
        if choice_note:
            self.logger.info("档位归一：%s", choice_note)
        if self.requested_times <= 0:
            self.logger.info("times=0 → 真跑时同样只读（只列清单，不发 11009）")
        else:
            self.logger.info(
                "真跑顺序：11025 → 11003(--area) → 逐个候选关卡发 11009"
                "（每关最多 %d 次，且会按响应回传的剩余体力再削一次）",
                planned,
            )
        return TaskResult(
            task=self.name,
            ok=True,
            message="演练完成（未联网、未发包）",
            data={
                "mode": "dry_run",
                "area": self.area_selector,
                "times": self.requested_times,
                "planned_times_per_section": planned,
                "section": self.section_selector,
                "allowed_packets": sorted(ALLOWED_PACKET_IDS.values()),
            },
        )

    def _dry_run_until_empty(self) -> TaskResult:
        """「扫荡到清空体力」的演练（不联网、不发包）。"""
        self.logger.info("演练模式（扫荡到清空体力）：不会联网，也不会发送任何报文")
        self.logger.info(
            "白名单：%s（2007 数扫荡券 / 11025 拉区域 / 11003 拉关卡 / 11009 扫荡）",
            sorted(ALLOWED_PACKET_IDS.values()),
        )
        self.logger.info(
            "参数：area=%s、times=扫荡到清空体力、section=%s",
            self.area_selector or "<未指定>",
            self.section_selector or "<未指定>",
        )
        if not self.section_selector:
            self.logger.warning(
                "清空体力模式**必须**用 --section 指定一个关卡；"
                "否则真跑时会直接失败（不会误扫整个区域）"
            )
        self.logger.info(
            "执行方式：反复发 11009（每轮一个包，**每轮按当前体力削峰**，"
            "单轮最多 times=%d），直到服务端返回体力不足 / 券不足；"
            "轮间隔随机 %.1f~%.1fs；安全上限 %d 次",
            UNTIL_EMPTY_ROUND_TIMES,
            config.SWEEP_INTERVAL_MIN_SECONDS,
            config.SWEEP_INTERVAL_MAX_SECONDS,
            config.SWEEP_UNTIL_EMPTY_MAX_TIMES,
        )
        return TaskResult(
            task=self.name,
            ok=True,
            message="演练完成（未联网、未发包）",
            data={
                "mode": "dry_run_until_empty",
                "area": self.area_selector,
                "times": SWEEP_UNTIL_EMPTY,
                "section": self.section_selector,
                "round_times": UNTIL_EMPTY_ROUND_TIMES,
                "max_times": config.SWEEP_UNTIL_EMPTY_MAX_TIMES,
                "allowed_packets": sorted(ALLOWED_PACKET_IDS.values()),
            },
        )

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def execute(self) -> TaskResult:
        """列清单 →（按需）逐关扫荡。

        :return: 统一任务结果。**默认只读（``--times`` 未给）时不会发出任何 ``11009``。**
        """
        if self.dry_run:
            return self._dry_run()

        # 配置表：唯一权威的"能不能扫"来源（读本地文件，不联网）
        sections = game_config.load_special_sections()
        section_index = game_config.special_section_index(sections)
        areas = game_config.load_special_areas()
        area_names = {area.area_id: (area.area_name or "") for area in areas}
        area_index = game_config.special_area_index(areas)

        with self.open_game_client() as game:
            # ① 11025（空包，只读）→ 11026：当期开放区域
            areas_res = self._fetch_areas(game)
            # 过滤掉不消耗体力的区域（如角色体验、特别挑战、纯 0 体力 EX 关等）
            opened = [
                area
                for area in areas_res.areaStateList
                if game_config.area_has_stamina_stages(area.areaID, section_index)
            ]
            self.logger.info(
                "当期开放体力活动区域 %d 个（原下发 %d 个，另有回忆区 %d 个、常驻区 %d 个）",
                len(opened),
                len(areas_res.areaStateList),
                len(areas_res.memoryAreaStateList),
                len(areas_res.permanentAreaStateList),
            )
            for position, area in enumerate(opened, start=1):
                self.logger.info(
                    "  %d) %s", position, area.describe(area_names.get(area.areaID, ""))
                )

            if not self.area_selector:
                all_sections: dict[str, dict[str, Any]] = {}
                if self.prefetch_sections:
                    for area in opened:
                        try:
                            sections_res = self._fetch_sections(game, area.areaID)
                            candidates, blocked = self._build_candidates(
                                sections_res.sectionStateList,
                                section_index,
                                area_id=area.areaID,
                            )
                            all_sections[str(area.areaID)] = {
                                "sweepable": [c.as_view() for c in candidates],
                                "blocked": [b.as_view() for b in blocked],
                            }
                        except Exception as exc:
                            self.logger.warning("预拉取区域 %d 关卡失败: %s", area.areaID, exc)

                return TaskResult(
                    task=self.name,
                    ok=True,
                    message=(
                        f"只读完成：当期开放 {len(opened)} 个区域（关卡已一并加载）。"
                        if all_sections
                        else f"只读完成：当期开放 {len(opened)} 个区域（清单见日志）。"
                        "要扫荡请指定区域，例如："
                        "python main.py run sweep_activity --area 2 --times 1"
                    ),
                    data={
                        "mode": "read_only",
                        "open_areas": len(opened),
                        # ★ 结构化清单（2026-09-22 新增）：网页的「活动区域」下拉就靠它。
                        # 之前这里只有 ``open_areas`` 这个**数量**，完整清单只出现在日志里，
                        # 于是网页端除了"自己翻日志抄 ID"之外无路可走。
                        # 计数键保留原样，旧调用方与测试不受影响。
                        "areas": [
                            {
                                "index": position,
                                **area_view(area, area_names.get(area.areaID, "")),
                            }
                            for position, area in enumerate(opened, start=1)
                        ],
                        "all_sections": all_sections,
                        "sent_packets": tuple(self.sent_packet_ids),
                    },
                )

            # ② 选定区域（三段规则：areaID → 清单编号 → 名称关键字）
            area_id, note = self._resolve_area(self.area_selector, opened, area_names)
            if area_id is None:
                return TaskResult(
                    task=self.name,
                    ok=False,
                    message=f"选择区域失败：{note}",
                    data={"sent_packets": tuple(self.sent_packet_ids)},
                )
            self.logger.info("目标区域：%s", note)

            area_cfg = area_index.get(area_id)
            if area_cfg is not None:
                area_block = game_config.area_block_reason(area_cfg)
                if area_block is not None:
                    return TaskResult(
                        task=self.name,
                        ok=False,
                        message=(
                            f"区域「{area_cfg.area_name or area_id}」不可扫荡：{area_block}"
                        ),
                        data={
                            "area_id": area_id,
                            "sent_packets": tuple(self.sent_packet_ids),
                        },
                    )

            # ③ 11003（只读）→ 11004：该区关卡，与配置表求交集（屏蔽规则见模块文档）
            sections_res = self._fetch_sections(game, area_id)
            candidates, blocked = self._build_candidates(
                sections_res.sectionStateList, section_index, area_id=area_id
            )
            self.logger.info(
                "关卡共 %d 个：可扫 %d 个、被挡 %d 个",
                len(sections_res.sectionStateList),
                len(candidates),
                len(blocked),
            )
            for candidate in candidates:
                self.logger.info("  ✔ %s", candidate.describe())
            for item in blocked:
                self.logger.info("  ✘ %s", item.describe())

            # 该区域的 ``11026`` 状态（只读清单要用它给界面填进度 / 起止时间）。
            selected_state = next(
                (item for item in opened if item.areaID == area_id), None
            )

            # ★ 2026-10-04：清空模式（``times=-1``）下 ``requested_times`` 恒为 0，
            #   但**不是**只读 —— 必须放它继续走到下面的清空循环，不能被这里拦掉。
            if self.requested_times <= 0 and not self.until_empty:
                return TaskResult(
                    task=self.name,
                    ok=True,
                    message=(
                        f"只读完成（times=0，未发任何扫荡请求）："
                        f"「{area_names.get(area_id, '') or area_id}」"
                        f"可扫 {len(candidates)} 关、被挡 {len(blocked)} 关"
                    ),
                    data={
                        "mode": "read_only",
                        "area_id": area_id,
                        # ``sweepable`` / ``blocked`` 两个**计数**保留原样（旧调用方在用），
                        # 详情另放 ``sections``，两者互不干扰。
                        "sweepable": len(candidates),
                        "blocked": len(blocked),
                        "area": (
                            area_view(selected_state, area_names.get(area_id, ""))
                            if selected_state is not None
                            else {
                                "area_id": area_id,
                                "name": area_names.get(area_id, ""),
                            }
                        ),
                        # ★ 结构化清单（2026-09-22 新增）：网页的「指定关卡」下拉靠它。
                        # ``blocked`` 也一并给出，界面才能解释"为什么这里少了 4 关"。
                        "sections": {
                            "sweepable": [item.as_view() for item in candidates],
                            "blocked": [item.as_view() for item in blocked],
                        },
                        "sent_packets": tuple(self.sent_packet_ids),
                    },
                )

            if not candidates:
                return TaskResult(
                    task=self.name,
                    ok=False,
                    message=(
                        f"区域「{area_names.get(area_id, '') or area_id}」"
                        f"没有可扫荡的关卡（{len(blocked)} 个全被屏蔽，"
                        "原因见日志：不消耗体力 / 配置表禁止 / 开了反外挂校验）"
                    ),
                    data={
                        "area_id": area_id,
                        "blocked": len(blocked),
                        "sent_packets": tuple(self.sent_packet_ids),
                    },
                )

            # ★ 2026-10-04：「扫荡到清空体力」**只作用于指定关卡**。
            #   未指定关卡就直接失败 —— 不允许"把整个区域所有关卡都扫到体力空"
            #   （那是完全不同的一种行为，用户明确只要单关）。
            if self.until_empty and not self.section_selector:
                return TaskResult(
                    task=self.name,
                    ok=False,
                    message=(
                        "「扫荡到清空体力」必须用 --section 指定一个关卡"
                        "（只对单个关卡反复扫；未指定关卡时不允许清空体力）"
                    ),
                    data={
                        "area_id": area_id,
                        "sent_packets": tuple(self.sent_packet_ids),
                    },
                )

            # ④ 真扫：默认扫全部候选，--section 可只扫其中一个
            targets = list(candidates)
            if self.section_selector:
                chosen, section_note = self._resolve_section(
                    self.section_selector, candidates
                )
                if chosen is None:
                    return TaskResult(
                        task=self.name,
                        ok=False,
                        message=f"选择关卡失败：{section_note}",
                        data={
                            "area_id": area_id,
                            "sent_packets": tuple(self.sent_packet_ids),
                        },
                    )
                self.logger.info("指定目标关卡：%s", section_note)
                # 若目标关卡尚未通关，且前面有未通关的前置关卡，必须先按顺序首通前置关卡
                if chosen.needs_first_pass:
                    preceding = [c for c in candidates if c.index < chosen.index and c.needs_first_pass]
                    if preceding:
                        self.logger.info("目标关卡前有 %d 个未通关前置关卡，将按顺序先行攻打", len(preceding))
                        targets = preceding + [chosen]
                    else:
                        targets = [chosen]
                else:
                    targets = [chosen]

            # ★ 2026-10-04：「扫荡到清空体力」走**独立的循环**（不进入下面逐关的
            #   普通流程）。前面已保证 ``section_selector`` 非空 → ``targets`` 就是
            #   用户选中的那一关（首通前置关卡的情形除外，这里不做首通）。
            if self.until_empty:
                return self._sweep_until_empty(game, targets[-1])

            done: list[tuple[SweepCandidate, int, SweepSectionRes]] = []
            first_passed: list[SweepCandidate] = []
            # ★ 2026-10-02：**真扫之前先数扫荡券**（只读 2007 GetAllItemPacket）。
            # 每扫 1 次固定扣 1 张券；零券时**一个 11009 都不许发** —— 否则就是
            # "发一个注定被拒（NoTicket = -3）的包"，白扣一次风控暴露。
            # 顺序刻意放在这里：只读模式（times=0）早就 return 了，
            # 所以"只想看看清单"的人**不会**被多问一次背包。
            ticket_left = self._fetch_ticket_count(game)
            if ticket_left <= 0:
                msg = (
                    "扫荡券不足（当前持有 0 张），拒绝执行扫荡："
                    "本次不会发送任何 11009（每扫 1 次扣 1 张券，先领券再来）"
                )
                self.logger.warning("%s", msg)
                return TaskResult(
                    task=self.name,
                    ok=True,
                    message=msg,
                    data={
                        "mode": "no_ticket",
                        "area_id": area_id,
                        "ticket_left": 0,
                        "sections_planned": len(targets),
                        "sections_done": 0,
                        "sent_packets": tuple(self.sent_packet_ids),
                    },
                )
            reason = f"已完成 {len(targets)} 个关卡的扫荡"
            for index, candidate in enumerate(targets, start=1):
                # ★ 2026-10-04：每一关之间都是安全的中止点（用户点「停止执行」后
                #   最多再扫完手上的这一关；扫荡批次长，这个检查点尤其重要）。
                cancellation.raise_if_cancelled()

                # ★ 正常已通关关卡：100% 保持原有扫荡逻辑不变，绝不上报战斗存证 ★
                if not candidate.needs_first_pass:
                    times, plan_note = self._plan_times(candidate)
                    if times <= 0:
                        reason = (
                            f"「{candidate.section_name or '该关卡'}」的可扫次数为 0"
                            f"（{plan_note or '体力不足以扫一次'}），已停止"
                        )
                        break
                    if plan_note:
                        self.logger.info(
                            "%s：%s", candidate.section_name or "该关卡", plan_note
                        )

                    res = self._sweep_once(game, candidate, times)
                    done.append((candidate, times, res))
                    # ★ 2026-10-04 口径修正：11010 的 rewardList/costList 里的
                    # itemAmount 是**扫荡后的持有量**（Q-021 真机结案），
                    # 所以运行视图只能说"体力剩 X、券剩 Y"，**不能**把它当本次掉落念。
                    # 逐件掉落明细（裸 itemID）属于开发者日志 → debug。
                    self.logger.info(
                        "第 %d/%d 关 %s：扫 %d 次 → %s",
                        index,
                        len(targets),
                        candidate.section_name or "该关卡",
                        times,
                        res.cost_summary(),
                    )
                    self.logger.debug(
                        "关卡 %s 本次结算明细（数量口径=操作后持有）：%s",
                        candidate.section_id,
                        res.reward_summary(),
                    )
                    if not res.is_success:
                        reason = (
                            f"服务端返回错误码 {res.errorCode}"
                            f"（{describe_error_code(res.errorCode)}），已停止（不重试）"
                        )
                        break
                    if index < len(targets):
                        cancellation.sleep_interruptible(config.sweep_interval())
                    continue

                # ★ 需要首次通关的简单关卡（勾选了「允许首次通关简单关卡」且 cheatCheck=0） ★
                times, plan_note = self._plan_times(candidate)
                if times <= 0:
                    reason = (
                        f"「{candidate.section_name or '该关卡'}」的可打次数为 0"
                        f"（{plan_note or '体力不足以打一次'}），已停止"
                    )
                    break
                if plan_note:
                    self.logger.info(
                        "%s：%s", candidate.section_name or "该关卡", plan_note
                    )

                # 执行首通
                ok, fp_res, fp_msg = self._first_pass_once(game, candidate)
                if not ok:
                    reason = (
                        f"第 {index}/{len(targets)} 关"
                        f"「{candidate.section_name or '该关卡'}」{fp_msg}，已停止"
                    )
                    break
                first_passed.append(candidate)
                self.logger.info(
                    "第 %d/%d 关 %s：首次通关成功！已获三星状态",
                    index,
                    len(targets),
                    candidate.section_name or "该关卡",
                )

                # 首通已计入 1 次推进。若本关为序列最终目标关卡且要求次数 > 1，则剩余次数继续走常规 11009 扫荡；
                # 中间前置关卡仅首通 1 次以推进解锁，不浪费扫荡次数在低收益关卡上。
                is_final_target = (index == len(targets))
                remaining_times = (times - 1) if is_final_target else 0
                if remaining_times > 0:
                    res = self._sweep_once(game, candidate, remaining_times)
                    done.append((candidate, remaining_times, res))
                    self.logger.info(
                        "第 %d/%d 关 %s：首通后扫 %d 次 → %s",
                        index,
                        len(targets),
                        candidate.section_name or "该关卡",
                        remaining_times,
                        res.cost_summary(),
                    )
                    self.logger.debug(
                        "关卡 %s 首通后结算明细（数量口径=操作后持有）：%s",
                        candidate.section_id,
                        res.reward_summary(),
                    )
                    if not res.is_success:
                        reason = (
                            f"服务端返回错误码 {res.errorCode}"
                            f"（{describe_error_code(res.errorCode)}），已停止（不重试）"
                        )
                        break
                if index < len(targets):
                    cancellation.sleep_interruptible(config.sweep_interval())

            # 若用户未指定特定关卡，且允许首次通关：每通关一关后，检查服务端是否解锁了下一关并继续推进
            while not self.section_selector and not self.only_passed and first_passed:
                # 重新拉取服务端最新关卡状态
                latest_res = self._fetch_sections(game, area_id)
                next_candidates, _ = self._build_candidates(
                    latest_res.sectionStateList, section_index, area_id=None
                )
                passed_ids = {c.section_id for c in first_passed} | {c.section_id for c, _, _ in done}
                unpassed = [
                    c for c in next_candidates
                    if c.needs_first_pass and c.section_id not in passed_ids
                ]
                if not unpassed:
                    break

                next_candidate = unpassed[0]
                times, plan_note = self._plan_times(next_candidate)
                if times <= 0:
                    reason = (
                        f"「{next_candidate.section_name or '该关卡'}」的可打次数为 0"
                        f"（{plan_note or '体力不足以打一次'}），已停止推进"
                    )
                    break

                self.logger.info(
                    "继续顺序推进下一关：%s",
                    next_candidate.section_name or "该关卡",
                )
                ok, fp_res, fp_msg = self._first_pass_once(game, next_candidate)
                if not ok:
                    reason = (
                        f"关卡「{next_candidate.section_name or '该关卡'}」{fp_msg}，已停止"
                    )
                    break
                first_passed.append(next_candidate)
                self.logger.info(
                    "关卡 %s：首次通关成功！已获三星状态",
                    next_candidate.section_name or "该关卡",
                )
                cancellation.sleep_interruptible(config.sweep_interval())

            # ⑤ 汇总
            total_times = sum(item[1] for item in done) + len(first_passed)
            ok = (bool(done) or bool(first_passed)) and all(res.is_success for _, _, res in done)
            return TaskResult(
                task=self.name,
                ok=ok,
                message=(
                    f"完成 {len(done) + len(first_passed)}/{len(targets)} 个关卡、共推进 {total_times} 次：{reason}"
                    + (
                        f"（剩余体力 {self.energy_left}）"
                        if self.energy_left is not None
                        else ""
                    )
                ),
                data={
                    "mode": "sweep",
                    "area_id": area_id,
                    "sections_done": len(done) + len(first_passed),
                    "first_passed": [c.section_id for c in first_passed],
                    "sections_planned": len(targets),
                    #: 被挡下的关卡数（不消耗体力的 / 配置表禁止的 / 未通关的 …）
                    #: —— 真扫时也要暴露，否则"为什么这关没扫"只能去翻日志
                    "blocked": len(blocked),
                    "times_total": total_times,
                    "energy_left": self.energy_left,
                    "packets": [
                        {
                            "section_id": candidate.section_id,
                            "times": times,
                            "error_code": res.errorCode,
                        }
                        for candidate, times, res in done
                    ],
                    "stop_reason": reason,
                    "sent_packets": tuple(self.sent_packet_ids),
                },
            )
