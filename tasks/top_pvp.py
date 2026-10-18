"""荣耀之巅（TopPvp）自动约战 —— 竞技板块里**唯一"纯协议可打"**的一个。

【为什么只有它能打（一句话）】
``39007 TopPvpChallengePacket`` 的**请求体是 0 字节**，服务器算完整场战斗后把战报
放进 ``39008`` 的 ``replayResult``。对照另外三个板块（竞技场 / 天命对决 / 失控炼成阵）：
它们都要**客户端自己算出结果并上报**（``26019`` / ``26039`` / ``22109``，且带逐施法存证
与客户端签名 token），没有复刻战斗引擎就无法通过服务端比对。
实测证据与字节级解析见 ``notes/glory_summit_capture.md``。

【场数语义（★ 2026-10-18 与其它战斗任务对齐）】
``--battles 0``（或不填）现在表示**打到服务端说没次数为止**，不再是"只读"——
用户要求"荣耀之巅中打几场的 0 和其他战斗同步"。
"只看不打"由独立的只读任务 ``top_pvp_status``（39001 only）承担，
``/api/player`` 的信息面板走的就是它；本任务因此**没有任何只读分支**，
也就不存在"以为在浏览、实际在打"的歧义。

【三道闸门（都要过才会真的打）】
1. ``--battles N``（或 ``CHERRYTALE_TOP_PVP_BATTLES``）显式给场数时按 N 打，
   并受 ``config.TOP_PVP_MAX_BATTLES`` 硬上限裁剪（防手误多打一个 0）；
2. ``battles <= 0`` → 打到次数用完为止，**只信服务端** 的 ``-1``：
   不自己推算剩余次数（39002 里没有"剩余次数"字段，见 ``models/top_pvp.py``），
   只留一个 1000 场的"防死循环"绝对兜底（正常玩家一天远到不了）；
3. 服务器错误码 ``-1``（次数不足）→ 立刻收工。

【流程】
::

    39001（空包，只读）→ 39002：段位/排名、已购次数、跳过战斗演出开关状态
    └─ 需要打 且 开关为关闭 → 39019{isSkipBattle=True}（「跳过战斗演出」默认开启）
    └─ 需要打 → 39011{sectionID, actionType}（镜像真实客户端的"进入模式"步骤）
    └─ 需要打 → 循环：39007（空包）→ 39008
         · errorCode == 0  → 记录胜负/积分变化/奖励，**胜负都继续**，不重试
         · errorCode == -1 → 次数不足，正常收工
         · 其它错误码      → 记录原因后收工（不猜、不重试）

【严格禁止"花钻石买次数"】
本任务**只**能发出白名单里的三个包（``models.top_pvp.ALLOWED_PACKET_IDS``），
每次发包前都会核对；购买次数的 ``39005`` 连模型都没有建。
回归测试 ``tests/test_tasks_top_pvp.py`` 会从字节层面断言"实际发出的消息号 ⊆ 白名单"。
"""

from __future__ import annotations

import logging
import random
import time
from typing import Final

import config
from client.game_client import GameClient
from client.session import GameSession
from client.spending import SpendPolicy
from models.envelope import EnvelopeView
from models.game_packet import ProtoMessage
from models.packet_ids import packet_id
from models.top_pvp import (
    ALLOWED_PACKET_IDS,
    FORBIDDEN_PACKET_IDS,
    PACKET_PURPOSE,
    TOP_PVP_ERROR_NO_CHALLENGE_TIMES,
    GetTopPvpHomePacket,
    GetTopPvpHomeRes,
    TopPvpChallengePacket,
    TopPvpChallengeRes,
    TopPvpSetSkipBattleTogglePacket,
    TopPvpSetSkipBattleToggleRes,
    TopTeamEditInfoPacket,
    TopTeamEditInfoRes,
    describe_error_code,
)
from services import cancellation
from tasks.base import BaseTask, RunOptions, TaskResult, register_task
from tasks.packet_guard import guarded_send


#: "打到次数用完"模式下的**防死循环绝对兜底**（不是业务上限）。
#:
#: 【为什么要有它（★ 2026-10-18）】
#: 用户要求"0 = 打到没次数为止，只信服务端"。服务端正常会返回 ``-1`` 收工，
#: 但协议异常时（返回 success 却不扣次数）就会没完没了地发包 —— 这类循环
#: 在挂机场景里代价极大（风控可见的真实流量）。所以留一个**远高于正常值**的
#: 绝对兜底：``eOtherSettings.TopPvp_FreeChallengeTimes`` 实测登记的是 395，
#: 1000 场在正常玩法下不可能触达，只在协议异常时保命。
SAFETY_MAX_BATTLES: Final[int] = 1000


@register_task
class TopPvpBattleTask(BaseTask):
    """荣耀之巅（TopPvp）：读取状态，并按用户指定的场数约战（战斗由服务器结算）。

    战斗演出**固定跳过**（2026-09-25 用户要求删掉「保留演出」开关）：真打之前会先
    确认服务端「跳过战斗演出」开关为开启（写包 39019）。
    唯一的例外是环境变量 ``CHERRYTALE_TOP_PVP_SKIP_BATTLE=0``（逃生舱，
    见 ``config.py``）—— 那时工具连这个开关都不碰。

    .. note::
       "只读浏览"搬去了独立任务 ``top_pvp_status``（本次 39001，零写操作）。
       本任务只要被叫到就是**真打**，所以不再有"只读模式不许写开关"那条分支。
    """

    name = "top_pvp_battle"

    #: 需要登录态（39001 之后所有请求都要带会话令牌）
    requires_auth = True

    #: 本任务**不存在**花钻石的路径：它只能发白名单里的三个包（挂机也不碰 39005）。
    #: 显式声明 ``False`` 是为了让 ``main.py tasks`` 的输出如实反映"这个任务不花钱"。
    spends_diamond = False

    #: 接受运行参数（打几场 / 间隔多久）—— 见 :class:`tasks.base.RunOptions`。
    accepts_run_options = True

    def __init__(
        self,
        session: GameSession,
        *,
        logger: logging.Logger | None = None,
        dry_run: bool = False,
        spend_policy: SpendPolicy | None = None,
        run_options: RunOptions | None = None,
    ) -> None:
        super().__init__(
            session,
            logger=logger,
            dry_run=dry_run,
            spend_policy=spend_policy,
            run_options=run_options,
        )
        #: 本次**实际发出**的消息号（按顺序）。测试用它断言"没有越界发包"。
        self.sent_packet_ids: list[int] = []

    # ------------------------------------------------------------------
    # 发包：一律走公共守卫（`tasks/packet_guard.py`）
    # ------------------------------------------------------------------
    def _guarded_send(
        self,
        game: GameClient,
        packet: ProtoMessage,
        expect: type[ProtoMessage],
    ) -> EnvelopeView:
        """发送一个**白名单内**的包并返回信封视图。

        【为什么守卫逻辑抽到 ``tasks/packet_guard.py``】
        竞技板块有四个模式（本任务 + 三个只读任务），各自抄一遍"能不能发这个包"，
        早晚会出现四份**各不相同**的白名单 —— 而漏一处的后果是"把次数买了/把战斗打了"。
        收敛到一处后，白名单、禁发清单、消息号核对、``include_defaults=True``
        以及"响应不是 protobuf 时的可读提示"都只有唯一实现。

        :raises ValueError: 命中禁发清单、不在白名单、或消息号与生成表不一致时。
        :raises client.exceptions.ProtocolError: 响应不是合法 protobuf 时。
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
            # ⚠️ 荣耀之巅**必须**带默认值：实测客户端的 ``39011`` 会显式发
            # ``actionType=0``（8 字节子包），省略它时服务端会把战斗类请求挡下
            # （回 22 字节 ``{"response_code":"OK"}``）。对照实验见
            # notes/glory_summit_capture.md 第九节。
            include_defaults=True,
        )


    # ------------------------------------------------------------------
    # ① 只读：拉主页（零消耗）
    # ------------------------------------------------------------------
    def _fetch_home(self, game: GameClient) -> GetTopPvpHomeRes:
        """读主页：段位/排名、已购次数、以及「跳过战斗演出」的服务端状态。"""
        view = self._guarded_send(game, GetTopPvpHomePacket(), GetTopPvpHomeRes)
        return view.decode_sub_packet(GetTopPvpHomeRes)

    # ------------------------------------------------------------------
    # ② 「跳过战斗演出」开关（默认开启）
    # ------------------------------------------------------------------
    def _ensure_skip_battle_enabled(
        self, game: GameClient, home: GetTopPvpHomeRes
    ) -> str:
        """按需求把「跳过战斗演出」置为开启；返回一行说明（进日志与结果）。

        【★ 2026-10-18：这里不再有"只读分支"】
        以前传 ``options`` 进来、发现是只读模式就不碰开关。现在"只看不打"
        搬去了 ``top_pvp_status``（它连 39019 的模型都不 import），
        本任务**只要在跑就是真打** —— 所以这条判断连同它的参数一起删掉，
        少一个"参数说只读、实际却要打"的错配可能。

        【2026-09-25：不再有"保留演出"参数】
        行为固定为跳过，用户没有开关可点（见 ``services/task_spec.py`` 的说明）。
        只有一个环境变量逃生舱会让它什么都不做，即
        ``CHERRYTALE_TOP_PVP_SKIP_BATTLE=0``。
        """
        want_skip = config.TOP_PVP_SKIP_BATTLE_DEFAULT
        if not want_skip:
            return (
                "按配置未改动服务端「跳过战斗演出」开关"
                f"（{config.TOP_PVP_SKIP_BATTLE_ENV_NAME}=0）"
            )
        if home.isSkipBattle:
            return "「跳过战斗演出」已是开启状态（服务端），无需改动"

        view = self._guarded_send(
            game,
            TopPvpSetSkipBattleTogglePacket.enable(),
            TopPvpSetSkipBattleToggleRes,
        )
        res = view.decode_sub_packet(TopPvpSetSkipBattleToggleRes)
        if not res.is_success():
            return f"开启「跳过战斗演出」失败：{describe_error_code(res.errorCode)}"
        return "已把「跳过战斗演出」置为开启（服务端已保存）"

    # ------------------------------------------------------------------
    # ②.5 进入模式：队伍编辑信息（镜像真实客户端的 39011）
    # ------------------------------------------------------------------
    def _sync_team_info(self, game: GameClient) -> str:
        """发送 ``39011``（挑战前必发），返回一行说明。

        【为什么必须有这一步】
        实测抓包（``captures/荣耀之巅.saz``）里真实客户端的顺序是 **39011 → 39007**。
        只发 39007 时，服务端回的**不是游戏协议**：22 字节 ``{"response_code":"OK"}``、
        ``content-type: text/html`` + ``Connection: close``（实测三次全一样），
        现象正是"读得到状态（39001/39019 都正常）、就是打不了架"。
        """
        request = TopTeamEditInfoPacket.for_section(config.TOP_PVP_SECTION_ID)
        view = self._guarded_send(game, request, TopTeamEditInfoRes)
        return view.decode_sub_packet(TopTeamEditInfoRes).summary()

    # ------------------------------------------------------------------
    # ③ 一场挑战（**会消耗一次次数**）
    # ------------------------------------------------------------------
    def _challenge_once(self, game: GameClient) -> TopPvpChallengeRes:
        """发起一场挑战并解出响应（服务器算完整场，结果就在响应里）。"""
        view = self._guarded_send(game, TopPvpChallengePacket(), TopPvpChallengeRes)
        return view.decode_sub_packet(TopPvpChallengeRes)

    def _run_battles(
        self, game: GameClient, planned: int | None
    ) -> tuple[list[TopPvpChallengeRes], str, int]:
        """循环挑战，直到"计划打完"或"服务端说没次数"。

        :param planned: 计划场数；``None`` = **打到次数用完为止**（0 / 不填）。
        :return: ``(成功开打的每一场响应, 结束原因, 最后一次业务错误码)``。

        结束条件（都**不重试** —— 这类请求有副作用，超时重发可能重复计分）:

        - ``errorCode == 0``：记下来继续下一场（**输了也继续**：需求是"按计算结果执行"）；
        - ``errorCode == -1``：次数不足，正常收工；
        - 其它错误码：记录原因后收工，把判断交给用户（不猜、不重试）；
        - ``planned`` 给定时打满即停。

        【★ 2026-10-18：为什么从 ``for range(battles)`` 改成 ``while``】
        场数语义变成"0 = 打到没次数为止"之后，"要打几场"这个数字**在开打前
        根本不存在** —— 39002 里没有"剩余次数"字段（``models/top_pvp.py`` 已
        标注该未知项），只能由服务端的 ``-1`` 告诉我们结束。于是循环条件从
        "数到 N"改成"服务端说停"。``SAFETY_MAX_BATTLES`` 只是防死循环的绝对兜底。

        【为什么 ``-1`` 那次响应不进 ``results``】
        第 N 次请求被服务端以"次数不足"拒绝时，**并没有真的打这一场**。
        把它算进结果会让汇总写成"共打 4 场（胜 3、结果未知 1）"——
        用户会以为打了一场还没出结果的架。所以只有 ``errorCode == 0``
        的响应才算"打过的场次"，收工码单独作为第三个返回值交给调用方判断。

        【逐场输出】
        每一场的结果走 :meth:`tasks.base.BaseTask.progress`（``PROGRESS`` 级别），
        网页「运行结果」会**打完一场就显示一场**；任务结束时由 runner 那行
        ✔/✘ 汇总收尾 —— 用户要的"最后再总结"就是它。
        """
        results: list[TopPvpChallengeRes] = []
        reason = "挑战次数已用完（服务端返回 -1）—— 按设计停止，绝不购买次数"
        interval = max(self.run_options.interval_seconds, 0.0)
        index = 0
        stop_code = 0

        while True:
            # ★ 2026-10-04：每一场之间都是安全的中止点。
            # 连打十几场时"点了停止却还要打完"是最难受的，所以每轮都问一次。
            cancellation.raise_if_cancelled()
            index += 1
            res = self._challenge_once(game)
            stop_code = res.errorCode

            if res.errorCode == TOP_PVP_ERROR_NO_CHALLENGE_TIMES:
                break
            if not res.is_success():
                self.logger.info(
                    "第 %d 次请求被服务端拒绝：%s",
                    index,
                    describe_error_code(res.errorCode),
                )
                reason = (
                    f"服务端返回错误码 {res.errorCode}"
                    f"（{describe_error_code(res.errorCode)}），已停止"
                )
                break

            results.append(res)
            if planned is None:
                self.progress("第 %d 场：%s", len(results), res.summary())
            else:
                self.progress("第 %d/%d 场：%s", len(results), planned, res.summary())

            if planned is not None and len(results) >= planned:
                reason = f"已完成计划的 {planned} 场"
                break
            if index >= SAFETY_MAX_BATTLES:
                reason = (
                    f"已连续打满 {SAFETY_MAX_BATTLES} 场仍未收到「次数不足」，"
                    "按防死循环兜底停止（协议可能异常）"
                )
                self.logger.warning(reason)
                break

            if interval > 0:
                wait_sec = interval
            else:
                wait_sec = random.uniform(
                    config.TOP_PVP_INTERVAL_MIN,
                    config.TOP_PVP_INTERVAL_MAX,
                )
            self.logger.debug("等待 %.1f 秒拟真间隔后进入下一场...", wait_sec)
            cancellation.sleep_interruptible(wait_sec)

        return results, reason, stop_code

    # ------------------------------------------------------------------
    # ④ 主流程
    # ------------------------------------------------------------------
    def _planned_battles(self) -> int | None:
        """本次计划打几场：``None`` = 打到次数用完为止。

        显式给的正数仍受 ``RunOptions.effective_battles`` 的硬上限裁剪
        （防手误多打一个 0）；0 / 负数一律当成"打完为止"。
        """
        requested = self.run_options.battles
        if requested <= 0:
            return None
        return self.run_options.effective_battles

    def execute(self) -> TaskResult:
        """读状态 → （按需）开启跳过演出 → 按场数约战。

        :return: 统一任务结果。``battles <= 0`` 时打到服务端返回 ``-1`` 为止。
        """
        options = self.run_options
        planned = self._planned_battles()
        plan_text = (
            "打到挑战次数用完为止（只信服务端 -1）"
            if planned is None
            else f"计划打 {planned} 场"
        )
        self.logger.info(
            "荣耀之巅：%s（battles=%d）；战斗演出=跳过（真打前确保服务端 39019 为开启）",
            plan_text,
            options.battles,
        )

        with self.open_game_client() as game:
            if self.dry_run:
                return self._dry_run(game, planned)

            # ① 只读：拉主页（零消耗）
            home = self._fetch_home(game)
            self.logger.info("荣耀之巅状态：%s", home.summary())
            if not home.is_success():
                return TaskResult(
                    task=self.name,
                    ok=False,
                    message=f"拉取荣耀之巅主页失败：{describe_error_code(home.errorCode)}",
                    data={
                        "error_code": home.errorCode,
                        "sent_packets": tuple(self.sent_packet_ids),
                    },
                )


            # ② 「跳过战斗演出」：真打前确保为开启
            #    （★ 2026-10-18：本任务已无只读分支 —— "只看不打"归 top_pvp_status）
            skip_note = self._ensure_skip_battle_enabled(game, home)
            self.logger.info("%s", skip_note)

            # ④ 进入模式（镜像真实客户端：挑战前先发 39011）
            team_note = self._sync_team_info(game)
            self.logger.info("队伍信息：%s", team_note)

            # ⑤ 真打：按场数循环（planned=None 表示打到次数用完）
            results, reason, stop_code = self._run_battles(game, planned)

            wins = sum(1 for r in results if r.battle_summary().get("winOrFail") is True)
            losses = sum(1 for r in results if r.battle_summary().get("winOrFail") is False)
            unknown = len(results) - wins - losses
            point_total = sum(r.point_change() for r in results)
            # 只有"被非正常错误码打断"才算失败；次数用尽是正常的日程结束。
            abnormal_stop = stop_code not in (0, TOP_PVP_ERROR_NO_CHALLENGE_TIMES)
            done_text = (
                f"共打 {len(results)} 场"
                if planned is None
                else f"完成 {len(results)}/{planned} 场"
            )

            return TaskResult(
                task=self.name,
                ok=not abnormal_stop,
                message=(
                    done_text
                    + f"（胜 {wins}、负 {losses}"
                    + (f"、结果未知 {unknown}" if unknown else "")
                    + f"；积分变化合计 {point_total:+d}）：{reason}"
                ),
                data={
                    "mode": "battle",
                    "planned_battles": -1 if planned is None else planned,
                    "battles_done": len(results),
                    "wins": wins,
                    "losses": losses,
                    "result_unknown": unknown,
                    "point_change_total": point_total,
                    "stop_reason": reason,
                    "skip_battle_note": skip_note,
                    "team_info": team_note,
                    "sent_packets": tuple(self.sent_packet_ids),
                },
            )

    # ------------------------------------------------------------------
    # ⑤ 演练（--dry-run）：只组装、打印，**一个字节都不发**
    # ------------------------------------------------------------------
    def _dry_run(self, game: GameClient, planned_battles: int | None) -> TaskResult:
        """打印"将要做什么、将要发什么字节"，不联网。

        :param planned_battles: 计划场数；``None`` = 打到次数用完（包数未知）。

        【演练为什么值得单独写好】
        这是唯一能在**不消耗任何次数**的前提下检查"包长得对不对"的机会 ——
        真打之后再发现问题，代价是一次真实消耗（甚至一次风控暴露）。
        """
        skip_policy = (
            "默认跳过：若发现服务端开关是关闭的，会先写 39019 置为开启"
            if config.TOP_PVP_SKIP_BATTLE_DEFAULT
            else (
                "按配置不动服务端开关"
                f"（{config.TOP_PVP_SKIP_BATTLE_ENV_NAME}=0）：不会去写 39019"
            )
        )
        plan_text = (
            "打到挑战次数用完为止（只信服务端 -1，包数未知）"
            if planned_battles is None
            else f"计划打 {planned_battles} 场"
        )
        planned: list[ProtoMessage] = [GetTopPvpHomePacket()]
        # ★ 2026-09-25：不再看"保留演出"参数（它已删），只看 config 逃生舱
        if config.TOP_PVP_SKIP_BATTLE_DEFAULT:
            planned.append(TopPvpSetSkipBattleTogglePacket.enable())
        # 镜像真实客户端：挑战前先发 39011（拉取队伍编辑信息）
        planned.append(
            TopTeamEditInfoPacket.for_section(config.TOP_PVP_SECTION_ID)
        )
        # 场数未知时按 1 个挑战包示意（不可枚举，写死 N 个会误导）
        planned.extend(
            TopPvpChallengePacket() for _ in range(planned_battles or 1)
        )

        challenge_count = planned_battles

        print("【荣耀之巅 · 演练】以下全部为本地组装，未发送任何请求")
        print()
        print(f"运行参数：{plan_text}")
        print(f"战斗演出策略：{skip_policy}")
        print()
        print(f"将要发送的报文（共 {len(planned)} 个）：")
        for packet in planned:
            name = type(packet).__name__
            number = packet_id(name)
            print(f"  · {number:>5} {name} —— {PACKET_PURPOSE.get(number, '')}")
        print()
        print("首个包的实际字节（用于与抓包逐字节比对）：")
        print(game.build(planned[0], include_defaults=True).describe())
        print()
        print("【安全边界】")
        print(f"  · 允许发出的消息号：{sorted(ALLOWED_PACKET_IDS.values())}")
        print(
            f"  · 明确禁发（含花钻石买次数的 39005）：{sorted(FORBIDDEN_PACKET_IDS.values())}"
        )
        print("  · 服务器返回 -1（次数不足）→ 立即停止，绝不进入购买分支")
        print("  · 无论胜负都不重试（POST 有副作用，重发可能重复计分）")
        print("  · 场数 0 / 不填 = 打到次数用完（只信服务端 -1，不自行推算剩余次数）")
        print()
        print("—— 以上为演练输出，未发送任何请求 ——")

        return TaskResult(
            task=self.name,
            ok=True,
            message=(
                f"演练完成：计划发出 {len(planned)} 个包"
                f"（{plan_text}），未发送"
            ),
            data={
                "planned_packets": tuple(packet_id(type(p).__name__) for p in planned),
                "planned_challenges": challenge_count,
                "read_only": False,
                "sent_packets": (),
            },
        )