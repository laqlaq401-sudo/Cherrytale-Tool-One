"""天命对决（WuDou）挑战次数购买任务（消息号 26033/26034 挑战页查询 与 26047/26048 购买次数）。

【流程与安全防线】
1. 若 ``times < 1``：**直接拒绝**（★ 2026-10-18：``0`` 不再表示只读查询），一个包都不发；
2. 发 ``26033 WuDouChallengeHome``（空包）拉取今日已购次数 ``buyedCount``；
3. 依据玩家 VIP 等级（或安全基准 VIP0）获取今日最大可购买上限（如 VIP0 为 10 次）；
4. 计算实际剩余可购买次数 ``remaining = max(0, max_limit - buyedCount)``；
5. 若 ``remaining <= 0``：**杜绝超发**，提示已达今日购买上限，安全退出且不发任何购买请求；
6. 若 ``times > remaining``：自动截断为 ``remaining``，防止向服务端发送超量请求；
7. 按 StepTollSystemData 阶梯费用表（``calculate_pvp_cost``）估算所需钻石；
8. 若设置了 ``max_cost`` 且超出预算，则提前安全终止；
9. 通过 ``request_diamond_spend(..., purpose=SpendPurpose.BUY_WUDOU_TICKET)`` 校验消费总闸与用途许可；
10. 演练模式（dry_run）仅打印意图与边界；真实模式受控逐次发送 ``WuDouBuyChallenge(26047)``。
"""

from __future__ import annotations

import logging
from typing import ClassVar, Final

from client.game_client import GameClient
from client.session import GameSession
from client.spending import SpendPolicy, SpendPurpose
from models.buy_pvp import (
    WuDouBuyChallenge,
    WuDouBuyChallengeRes,
    calculate_pvp_cost,
    get_vip_max_buy_times,
)
from models.wudou import WuDouChallengeHome, WuDouChallengeHomeRes
from tasks.base import BaseTask, RunOptions, TaskResult, register_task
from tasks.packet_guard import guarded_send

ALLOWED_PACKET_IDS: Final[dict[str, int]] = {
    "WuDouChallengeHome": 26033,
    "WuDouBuyChallenge": 26047,
}

FORBIDDEN_PACKET_IDS: Final[dict[str, int]] = {
    "WuDouChallenge": 26039,
    "WuDouReplayPacket": 26051,
}

PACKET_PURPOSE: Final[dict[int, str]] = {
    26033: "拉取天命对决挑战页（只读：查询今日已购次数与剩余挑战次数）",
    26047: "用钻石购买天命对决挑战次数（单次 1 次）",
}


@register_task
class BuyWuDouTicketTask(BaseTask):
    """用钻石购买天命对决挑战次数。购买次数至少 1 次（``times=0`` 已不再表示只读）。"""

    name = "buy_wudou_ticket"

    requires_auth = True

    #: 仍保持 ``False``：网页只在「钻石使用」板块执行它，该板块不展示
    #: 「有消耗」徽标；真正的硬闸是钻石总闸 + 用途白名单。
    spends_diamond = False

    diamond_purposes = (SpendPurpose.BUY_WUDOU_TICKET,)

    opt_in_hint: ClassVar[str] = (
        "需 --times ≥ 1 且 --allow-diamond 才会用钻石购买天命对决挑战次数（不再提供只读查询）"
    )

    def __init__(
        self,
        session: GameSession,
        *,
        logger: logging.Logger | None = None,
        dry_run: bool = False,
        spend_policy: SpendPolicy | None = None,
        run_options: RunOptions | None = None,
        times: int = 1,
        max_cost: int = 0,
        vip_lv: int = 0,
    ) -> None:
        super().__init__(
            session,
            logger=logger,
            dry_run=dry_run,
            spend_policy=spend_policy,
            run_options=run_options,
        )
        self.times = max(0, int(times))
        self.max_cost = max(0, int(max_cost))
        self.vip_lv = max(0, int(vip_lv))
        self.sent_packet_ids: list[int] = []

    def _query_challenge_home(self, game: GameClient) -> WuDouChallengeHomeRes:
        """拉取天命对决挑战页（只读）。"""
        view = guarded_send(
            game,
            WuDouChallengeHome(),
            WuDouChallengeHomeRes,
            allowed=ALLOWED_PACKET_IDS,
            forbidden=FORBIDDEN_PACKET_IDS,
            record=self.sent_packet_ids,
            purpose=PACKET_PURPOSE,
            logger=self.logger,
        )
        return view.decode_sub_packet(WuDouChallengeHomeRes)

    def _execute_buy_single(self, game: GameClient) -> WuDouBuyChallengeRes:
        """发送单次购买请求（26047）。"""
        view = guarded_send(
            game,
            WuDouBuyChallenge(),
            WuDouBuyChallengeRes,
            allowed=ALLOWED_PACKET_IDS,
            forbidden=FORBIDDEN_PACKET_IDS,
            record=self.sent_packet_ids,
            purpose=PACKET_PURPOSE,
            logger=self.logger,
        )
        return view.decode_sub_packet(WuDouBuyChallengeRes)

    def execute(self) -> TaskResult:
        # ★ 2026-10-18：0 次不再表示"只读查询"（与购买体力同一口径）。
        # 明确拒绝，而不是静默只读或静默升成 1（后者会真扣钻石）。
        if self.times < 1:
            msg = "购买次数至少 1 次（0 已不再表示只读查询）；本次未发送任何请求"
            self.logger.warning("%s", msg)
            return TaskResult(task=self.name, ok=False, message=msg)

        with self.open_game_client() as game:
            # 步骤 1：查询已购次数
            home = self._query_challenge_home(game)
            bought_count = int(home.buyedCount)
            max_limit = get_vip_max_buy_times(self.vip_lv, "wudou")
            remaining = max(0, max_limit - bought_count)

            # 步骤 2：购买前先查看账户实际可购买次数，杜绝超发
            if remaining <= 0:
                msg = (
                    f"天命对决今日购买次数已达上限（已购买 {bought_count}/{max_limit} 次），"
                    "已安全跳过，未发送任何购买请求"
                )
                self.logger.info("%s", msg)
                return TaskResult(
                    task=self.name,
                    ok=True,
                    message=msg,
                    data={"bought_count": bought_count, "purchased": 0},
                )

            actual_to_buy = min(self.times, remaining)
            if actual_to_buy < self.times:
                self.logger.warning(
                    "申请购买 %d 次超过剩余上限 %d 次，自动截断为 %d 次以避免超量发包",
                    self.times,
                    remaining,
                    actual_to_buy,
                )

            # 步骤 3：计算阶梯费用与预算检查
            cost = calculate_pvp_cost(bought_count, actual_to_buy)
            if self.max_cost > 0 and cost > self.max_cost:
                msg = (
                    f"购买 {actual_to_buy} 次预计消耗 {cost} 钻石，超过预算上限 {self.max_cost} 钻石，已取消购买"
                )
                self.logger.warning("%s", msg)
                return TaskResult(task=self.name, ok=False, message=msg)

            # 步骤 4：过钻石消费许可闸门
            self.request_diamond_spend(
                cost,
                reason=f"购买天命对决挑战次数 {actual_to_buy} 次，预计消耗 {cost} 钻石",
                purpose=SpendPurpose.BUY_WUDOU_TICKET,
            )

            # 步骤 5：演练模式分支
            if self.dry_run:
                msg = (
                    f"[演练] 拟购买天命对决挑战次数 {actual_to_buy} 次（已购 {bought_count}/{max_limit} 次），"
                    f"预计消耗 {cost} 钻石"
                )
                self.logger.info("%s", msg)
                return TaskResult(
                    task=self.name,
                    ok=True,
                    message=msg,
                    data={
                        "planned_times": actual_to_buy,
                        "estimated_cost": cost,
                        "dry_run": True,
                    },
                )

            # 步骤 6：真实发包购买（逐次发包受控循环）
            successful_buys = 0
            for i in range(actual_to_buy):
                res = self._execute_buy_single(game)
                if not res.is_success:
                    self.logger.error(
                        "第 %d 次购买失败：errorCode=%d，中止后续购买",
                        i + 1,
                        res.errorCode,
                    )
                    break
                successful_buys += 1

            msg = (
                f"成功购买天命对决挑战次数 {successful_buys}/{actual_to_buy} 次，"
                f"消耗钻石约 {calculate_pvp_cost(bought_count, successful_buys)}"
            )
            self.logger.info("%s", msg)
            return TaskResult(
                task=self.name,
                ok=successful_buys > 0,
                message=msg,
                data={
                    "bought_count": bought_count + successful_buys,
                    "purchased": successful_buys,
                    "cost": calculate_pvp_cost(bought_count, successful_buys),
                },
            )
