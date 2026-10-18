"""天命对决（WuDou PvP）自动选人与云端战斗任务。

【架构与机制（逆向闭环实证）】
1. **纯云端推演**：官方底层开启 ``get_is_WuDouPVP_Rp() == true``，服务端在收到
   ``26051 WuDouReplayPacket`` 后由云端战斗引擎权威推演完整过程，并将战斗结果、
   逐帧回放与翻牌到账奖励（``winRewardList``）直接在 ``26052 WuDouReplayRes`` 中一并下发；
2. **死代码排除**：客户端旧版 ``26037 WuDouChallengeReady`` 与 ``26039 WuDouChallenge``
   已被官方标记为 [Obsolete] 废弃，在现代客户端中 100% 不会被调用，本项目严格不碰；
3. **翻牌奖励**：胜利后的三选一翻牌为客户端本地纯伪随机演出动画，奖励早在 26052 下发时
   即已入库到账，无需任何后续选牌发包交互；
4. **并发锁核验**：真实客户端在选定对手后先发 ``26053 WuDouRankPacket`` 校验目标排名未过期，
   随后发 ``26051`` 触发开战，本项目完整镜像该真实发包链路。

【安全防护（三道红线）】
- **默认只读（battles=0）**：未显式指定场数时仅拉取主页与挑战页展示对手，零消耗退出；
- **严格禁止花钻石买次数**：``26047 WuDouBuyChallenge`` 列入 ``FORBIDDEN_PACKET_IDS``，
  若收到服务端返回错误码 ``-1``（挑战次数不足），立即熔断停止，绝不花钻；
- **拟真随机冷却**：默认每场对决结算后随机等待 5~10 秒（由 ``config.WUDOU_PVP_INTERVAL_MIN``
  与 ``config.WUDOU_PVP_INTERVAL_MAX`` 控制，可通过 ``cancellation.sleep_interruptible`` 随时中断）。

【选人策略】
- ``lowest_power``（默认）：挑选候选列表中防守战力最低的对手；
- ``npc_first``：优先挑选 NPC 训练家（playerID < 0），若有多个则取战力最低；无 NPC 则回退为低战优先；
- ``rank_first``：冲榜优先，挑选名次最靠前的对手（ranking > 0 且数值最小）。
"""

from __future__ import annotations

import logging
import random
from collections.abc import Sequence
from typing import Final

import config
from client.game_client import GameClient
from client.session import GameSession
from client.spending import SpendPolicy
from models.envelope import EnvelopeView
from models.game_packet import ProtoMessage
from models.wudou import (
    ALLOWED_PACKET_IDS,
    FORBIDDEN_PACKET_IDS,
    PACKET_PURPOSE,
    TEAM_TYPE_WUDOU_ATK,
    WUDOU_ERROR_NO_CHALLENGE_TIMES,
    WUDOU_SUCCESS,
    PVPListClass,
    WuDouChallengeHome,
    WuDouChallengeHomeRes,
    WuDouHome,
    WuDouHomeRes,
    WuDouRankPacket,
    WuDouRankRes,
    WuDouReplayPacket,
    WuDouReplayRes,
    describe_error_code,
    is_no_times_error,
)
from services import cancellation
from tasks.base import BaseTask, RunOptions, TaskResult, register_task
from tasks.packet_guard import guarded_send

_LOGGER: Final[logging.Logger] = logging.getLogger("tasks.wudou_battle")


def select_opponent(
    opponents: Sequence[PVPListClass],
    strategy: str = "lowest_power",
) -> PVPListClass | None:
    """根据选人策略从对手列表中挑选一名挑战目标。

    :param opponents: 挑战页候选对手列表（通常 3~4 名）。
    :param strategy: 选人策略：
        - ``"lowest_power"``：优先挑选防守战力（fighting）最低的对手；
        - ``"npc_first"``：优先挑选系统 NPC 训练家；若多个 NPC 取战力最低；无 NPC 则按 lowest_power 挑选；
        - ``"rank_first"``：冲榜优先，挑选名次最靠前的对手（ranking > 0 且数值最小）；若名次相同取战力最低。
    :return: 挑选出的对手条目，候选列表为空时返回 None。
    """
    if not opponents:
        return None

    if strategy == "npc_first":
        npcs = [o for o in opponents if o.is_npc]
        if npcs:
            return min(npcs, key=lambda o: (o.fighting, o.playerID))
        return min(opponents, key=lambda o: (o.fighting, o.playerID))

    if strategy == "rank_first":
        ranked = [o for o in opponents if o.ranking > 0]
        if ranked:
            return min(ranked, key=lambda o: (o.ranking, o.fighting, o.playerID))
        return min(opponents, key=lambda o: (o.fighting, o.playerID))

    # 默认 lowest_power
    return min(opponents, key=lambda o: (o.fighting, o.playerID))


@register_task
class WuDouBattleTask(BaseTask):
    """天命对决（WuDou）：自动选人并完成挑战，消耗挑战次数。"""

    name = "wudou_pvp_battle"
    description = "天命对决（WuDou）：自动选人并完成挑战，消耗挑战次数"

    #: 需要登录态
    requires_auth = True

    #: 绝不花钻石：只能发送白名单内的查询、核验与挑战包，禁止 26047 购次
    spends_diamond = False

    #: 接受运行参数（打几场 / 间隔多久）
    accepts_run_options = True

    def __init__(
        self,
        session: GameSession,
        *,
        logger: logging.Logger | None = None,
        dry_run: bool = False,
        spend_policy: SpendPolicy | None = None,
        run_options: RunOptions | None = None,
        strategy: str = "lowest_power",
        team_id: int = TEAM_TYPE_WUDOU_ATK,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            session,
            logger=logger,
            dry_run=dry_run,
            spend_policy=spend_policy,
            run_options=run_options,
        )
        self.strategy = strategy or config.WUDOU_PVP_STRATEGY
        self.team_id = team_id
        self.sent_packet_ids: list[int] = []

    # ------------------------------------------------------------------
    # 发包：一律走公共守卫（`tasks/packet_guard.py`）
    # ------------------------------------------------------------------
    def _guarded_send(
        self,
        game: GameClient,
        packet: ProtoMessage,
        expect: type[ProtoMessage] | None,
        *,
        include_defaults: bool = False,
    ) -> EnvelopeView:
        """发送白名单内的包并记录发包轨迹；越界或禁发直接抛错。"""
        return guarded_send(
            game,
            packet,
            expect,
            allowed=ALLOWED_PACKET_IDS,
            forbidden=FORBIDDEN_PACKET_IDS,
            record=self.sent_packet_ids,
            purpose=PACKET_PURPOSE,
            logger=self.logger,
            include_defaults=include_defaults,
        )

    # ------------------------------------------------------------------
    # 业务请求步骤
    # ------------------------------------------------------------------
    def _fetch_home(self, game: GameClient) -> WuDouHomeRes:
        """读主页：出战阵容、周榜、分组（消息号 26031）。"""
        view = self._guarded_send(game, WuDouHome.for_default_group(), WuDouHomeRes)
        return view.decode_sub_packet(WuDouHomeRes)

    def _fetch_challenge_home(self, game: GameClient) -> WuDouChallengeHomeRes:
        """读挑战页：对手列表、我的名次、剩余挑战次数（消息号 26033）。"""
        view = self._guarded_send(game, WuDouChallengeHome(), WuDouChallengeHomeRes)
        return view.decode_sub_packet(WuDouChallengeHomeRes)

    def _verify_rank(
        self,
        game: GameClient,
        target_pid: int,
        my_ranking: int,
        target_ranking: int,
    ) -> WuDouRankRes:
        """开战前核验对手排名乐观并发锁（消息号 26053）。"""
        packet = WuDouRankPacket(
            targetPid=target_pid,
            myRanking=my_ranking,
            targetRanking=target_ranking,
            isBlock=False,
        )
        view = self._guarded_send(
            game, packet, WuDouRankRes, include_defaults=True
        )
        return view.decode_sub_packet(WuDouRankRes)

    def _challenge_once(
        self,
        game: GameClient,
        target_pid: int,
        team_id: int,
        my_ranking: int,
        target_ranking: int,
    ) -> WuDouReplayRes:
        """纯云端发起挑战（消息号 26051）。服务器权威推演完整战局后回传 26052。"""
        packet = WuDouReplayPacket(
            targetPid=target_pid,
            teamID=team_id,
            myRanking=my_ranking,
            targetRanking=target_ranking,
        )
        view = self._guarded_send(
            game, packet, WuDouReplayRes, include_defaults=True
        )
        return view.decode_sub_packet(WuDouReplayRes)

    # ------------------------------------------------------------------
    # 演练模式
    # ------------------------------------------------------------------
    def _dry_run(self, game: GameClient, options: RunOptions) -> TaskResult:
        """演练模式：零网络发包，仅输出模拟发包计划与策略配置。"""
        battles = (
            "全部剩余次数（0=打完所有剩余次数）"
            if options.battles <= 0
            else f"{options.battles}"
        )
        msg = (
            f"演练模式（零网络请求）：将模拟天命对决流程。"
            f"计划挑战 {battles} 场，选人策略={self.strategy}。"
            f"发包序列：26031(主页) -> 26033(挑战页) -> "
            f"循环[26053(核验) -> 26051(云端挑战) -> 26033(刷新列表)]；严格禁止 26047。"
            f"（一旦无实际挑战次数立即中断发包停止）"
        )
        self.logger.info("%s", msg)
        return TaskResult(
            task=self.name,
            ok=True,
            message=msg,
            data={
                "mode": "dry_run",
                "planned_battles": battles,
                "strategy": self.strategy,
                "sent_packets": tuple(self.sent_packet_ids),
            },
        )

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def execute(self) -> TaskResult:
        """读取状态 → 选人与云端推演对决。

        :return: 统一任务结果。
        """
        options = self.run_options
        battles_plan = (
            "打完所有剩余次数"
            if options.battles <= 0
            else f"计划打 {options.battles} 场"
        )
        self.logger.info(
            "天命对决：%s（间隔 %gs）；选人策略=%s",
            battles_plan,
            options.interval_seconds,
            self.strategy,
        )

        with self.open_game_client() as game:
            if self.dry_run:
                return self._dry_run(game, options)

            # ① 拉主页（获取出战队伍 ID 与分组）
            home = self._fetch_home(game)
            self.logger.info("天命对决主页：%s", home.summary())
            # 攻防队伍解耦：26032 下发的 home.team 为防守编队（WuDouPVP_Def=14）；
            # 进攻发起（26051）必须使用天命对决进攻编队（Wudou_PVP1=70）
            team_id = self.team_id

            # ② 拉挑战页（获取对手列表、当前名次、剩余挑战次数）
            challenge_home = self._fetch_challenge_home(game)
            self.logger.info("天命对决挑战页：%s", challenge_home.summary())

            # ③ 剩余次数预检（零次数直接中断发包并退出，绝不发 26047 买次数）
            actual_available = challenge_home.challenge_times
            if actual_available <= 0:
                self.logger.info(
                    "天命对决无实际挑战次数（剩余 %d 次），立即中断发包停止", actual_available
                )
                return TaskResult(
                    task=self.name,
                    ok=True,
                    message=(
                        f"天命对决挑战次数不足（当前剩余 {actual_available} 次），"
                        "已中断发包并按设计停止，绝不发送 26047 购买次数。"
                    ),
                    data={
                        "mode": "no_times",
                        "challenge_times": actual_available,
                        "my_ranking": challenge_home.myRanking,
                        "npc_times": challenge_home.npc_times,
                        "buyed_count": challenge_home.buyedCount,
                        "strategy": self.strategy,
                        "sent_packets": tuple(self.sent_packet_ids),
                    },
                )

            # ④ 计算实际挑战场数：0 或负数表示打完所有剩余次数；大于 0 为指定场数（自动对齐可用次数）
            requested_battles = options.battles
            if requested_battles <= 0:
                battles_to_run = actual_available
                self.logger.info(
                    "天命对决：打完所有剩余次数（battles=%d），当前实际剩余 %d 次，计划挑战 %d 场",
                    requested_battles,
                    actual_available,
                    battles_to_run,
                )
            else:
                if requested_battles > actual_available:
                    self.logger.info(
                        "天命对决：计划挑战 %d 场，但服务端当前实际剩余 %d 次，"
                        "自动对齐为 %d 场，无挑战次数时立即中断发包。",
                        requested_battles,
                        actual_available,
                        actual_available,
                    )
                    battles_to_run = actual_available
                else:
                    battles_to_run = requested_battles

            results: list[WuDouReplayRes] = []
            reason = f"已完成计划的 {battles_to_run} 场"
            current_my_ranking = challenge_home.myRanking
            current_challenge_times = actual_available

            for index in range(1, battles_to_run + 1):
                cancellation.raise_if_cancelled()

                # 每轮开始前再次核验实际次数，一旦无可用次数立即中断发包
                if current_challenge_times <= 0:
                    reason = "挑战次数已用完（当前剩余 0 次）—— 立即中断发包，正常停止"
                    self.logger.info("%s", reason)
                    break

                # 选人
                opponent = select_opponent(challenge_home.challengeList, self.strategy)
                if opponent is None:
                    reason = "未在列表中找到有效对手，已停止"
                    self.logger.warning("%s", reason)
                    break

                target_pid = opponent.playerID
                target_ranking = opponent.ranking
                self.logger.info(
                    "第 %d/%d 场：匹配对手 %s (PID=%d, 排名=%d, 战力=%d, NPC=%s)",
                    index,
                    battles_to_run,
                    opponent.name,
                    target_pid,
                    target_ranking,
                    opponent.fighting,
                    opponent.is_npc,
                )

                # 排名并发锁核验 26053
                rank_res = self._verify_rank(
                    game,
                    target_pid=target_pid,
                    my_ranking=current_my_ranking,
                    target_ranking=target_ranking,
                )
                if is_no_times_error(rank_res.errorCode):
                    reason = (
                        f"挑战次数已用完（26053 返回 {rank_res.errorCode}）"
                        "—— 立即中断发包，正常停止，绝不购买次数"
                    )
                    self.logger.info("%s", reason)
                    break
                if not rank_res.is_success():
                    # 若对手名次变更（-3 冲突），刷新一次候选列表重选
                    self.logger.warning(
                        "对手排名核验未通过: errorCode=%d (%s)，正在刷新挑战页重选",
                        rank_res.errorCode,
                        describe_error_code(rank_res.errorCode),
                    )
                    challenge_home = self._fetch_challenge_home(game)
                    current_my_ranking = challenge_home.myRanking
                    current_challenge_times = challenge_home.challenge_times
                    if current_challenge_times <= 0:
                        reason = "刷新后挑战次数已用完（剩余 0 次）—— 立即中断发包，正常停止"
                        break
                    opponent = select_opponent(challenge_home.challengeList, self.strategy)
                    if opponent is None:
                        reason = "刷新后未找到有效对手，已停止"
                        break
                    target_pid = opponent.playerID
                    target_ranking = opponent.ranking
                    rank_res = self._verify_rank(
                        game,
                        target_pid=target_pid,
                        my_ranking=current_my_ranking,
                        target_ranking=target_ranking,
                    )
                    if is_no_times_error(rank_res.errorCode):
                        reason = (
                            f"挑战次数已用完（重试 26053 返回 {rank_res.errorCode}）"
                            "—— 立即中断发包，正常停止"
                        )
                        break
                    if not rank_res.is_success():
                        reason = (
                            f"对手排名核验失败: errorCode={rank_res.errorCode}"
                            f"（{describe_error_code(rank_res.errorCode)}）"
                        )
                        break

                # 纯云端推演开战 26051
                replay_res = self._challenge_once(
                    game,
                    target_pid=target_pid,
                    team_id=team_id,
                    my_ranking=current_my_ranking,
                    target_ranking=target_ranking,
                )
                results.append(replay_res)
                # ★ 2026-10-18：逐场结果走 progress（网页「运行结果」实时显示）。
                self.progress(
                    "第 %d/%d 场：%s", index, battles_to_run, replay_res.summary()
                )

                if is_no_times_error(replay_res.errorCode):
                    reason = (
                        f"挑战次数已用完（26052 返回 {replay_res.errorCode}）"
                        "—— 立即中断发包，正常停止，绝不购买次数"
                    )
                    break
                if not replay_res.is_success():
                    reason = (
                        f"服务端返回错误码 {replay_res.errorCode}"
                        f"（{describe_error_code(replay_res.errorCode)}），已停止"
                    )
                    break

                # 更新当前名次与剩余次数
                if replay_res.myRanking > 0:
                    current_my_ranking = replay_res.myRanking

                # 同步本地记录的挑战次数
                current_challenge_times = (
                    replay_res.challenge_times
                    if replay_res.challenge_times > 0
                    else max(current_challenge_times - 1, 0)
                )

                if current_challenge_times <= 0:
                    reason = "挑战次数已用完（战后剩余 0 次）—— 立即中断发包，正常停止，绝不购买次数"
                    break

                # 若还有下一场，拟真随机等待 5~10 秒，并刷新对手列表
                if index < battles_to_run:
                    if options.interval_seconds > 0:
                        wait_sec = options.interval_seconds
                    else:
                        wait_sec = random.uniform(
                            config.WUDOU_PVP_INTERVAL_MIN,
                            config.WUDOU_PVP_INTERVAL_MAX,
                        )
                    self.logger.debug("等待 %.1f 秒拟真间隔后进入下一场...", wait_sec)
                    cancellation.sleep_interruptible(wait_sec)

                    # 刷新对手列表
                    challenge_home = self._fetch_challenge_home(game)
                    current_my_ranking = challenge_home.myRanking
                    current_challenge_times = challenge_home.challenge_times
                    if current_challenge_times <= 0:
                        reason = "挑战次数已用完（刷新后剩余 0 次）—— 立即中断发包，正常停止"
                        break

            # ⑥ 结算汇总
            wins = sum(1 for r in results if r.battle_summary().get("winOrFail") is True)
            losses = sum(1 for r in results if r.battle_summary().get("winOrFail") is False)
            unknown = len(results) - wins - losses

            last_code = results[-1].errorCode if results else 0
            abnormal_stop = last_code not in (0, WUDOU_ERROR_NO_CHALLENGE_TIMES, 5)

            return TaskResult(
                task=self.name,
                ok=not abnormal_stop,
                message=(
                    f"完成 {len(results)}/{battles_to_run} 场"
                    f"（胜 {wins}、负 {losses}"
                    + (f"、结果未知 {unknown}" if unknown else "")
                    + f"；最新名次 {current_my_ranking}）：{reason}"
                ),
                data={
                    "mode": "battle",
                    "planned_battles": battles_to_run,
                    "battles_done": len(results),
                    "wins": wins,
                    "losses": losses,
                    "unknown": unknown,
                    "strategy": self.strategy,
                    "final_ranking": current_my_ranking,
                    "sent_packets": tuple(self.sent_packet_ids),
                    "reason": reason,
                },
            )
