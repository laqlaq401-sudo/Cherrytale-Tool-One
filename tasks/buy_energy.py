"""钻石购买体力任务（消息号 24011/24012 VIP详情 与 24005/24006 购买体力）。

【流程】
1. 若 ``times < 1``：**直接拒绝**（★ 2026-10-18：``0`` 不再表示只读查询），
   一个包都不发 —— 界面下限已是 1，但 CLI / 旧存档 / API 仍可能把 0 送进来；
2. 发 ``VIPDetailPacket(24011)``（空报文）拉取今日已购次数 ``buyEnergyCount``；
3. 按阶梯费用表（``models.buy_energy.calculate_energy_cost``）估算所需钻石；
4. 若设置了 ``max_cost`` 且超出上限，则提前终止；
5. 通过 ``request_diamond_spend(..., purpose=SpendPurpose.BUY_ENERGY)`` 过消费闸门；
6. 演练模式（dry_run）仅打印意图，真实模式发送 ``BuyEnergyPacket(24005)`` 执行购买。
"""

from __future__ import annotations

import logging
from typing import ClassVar

from client.game_client import GameClient
from client.session import GameSession
from client.spending import SpendPolicy, SpendPurpose
from models.buy_energy import (
    ALLOWED_PACKET_IDS,
    ENERGY_PER_PURCHASE,
    FORBIDDEN_PACKET_IDS,
    PACKET_PURPOSE,
    BuyEnergyPacket,
    BuyEnergyRes,
    VIPDetailPacket,
    VIPDetailRes,
    calculate_energy_cost,
)
from tasks.base import BaseTask, RunOptions, TaskResult, register_task
from tasks.packet_guard import guarded_send


@register_task
class BuyEnergyTask(BaseTask):
    """用钻石购买体力任务。购买次数至少 1 次（``times=0`` 已不再表示只读）。"""

    name = "buy_energy"

    requires_auth = True

    #: 仍保持 ``False``：网页只在「钻石使用」板块执行它，该板块不展示
    #: 「有消耗」徽标；真正的硬闸是钻石总闸 + 用途白名单（见 diamond_purposes）。
    spends_diamond = False

    #: 用途声明：只有 BUY_ENERGY 授权时才放行购买分支
    diamond_purposes = (SpendPurpose.BUY_ENERGY,)

    opt_in_hint: ClassVar[str] = "需 --times ≥ 1 且 --allow-diamond 才会用钻石购买体力（不再提供只读查询）"

    def __init__(
        self,
        session: GameSession,
        *,
        logger: logging.Logger | None = None,
        dry_run: bool = False,
        spend_policy: SpendPolicy | None = None,
        run_options: RunOptions | None = None,
        times: int = 0,
        max_cost: int = 0,
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
        self.sent_packet_ids: list[int] = []

    def _query_vip_detail(self, game: GameClient) -> VIPDetailRes:
        """拉取 VIP 详情（只读）。"""
        view = guarded_send(
            game,
            VIPDetailPacket(),
            VIPDetailRes,
            allowed=ALLOWED_PACKET_IDS,
            forbidden=FORBIDDEN_PACKET_IDS,
            record=self.sent_packet_ids,
            purpose=PACKET_PURPOSE,
            logger=self.logger,
        )
        return view.decode_sub_packet(VIPDetailRes)

    def _execute_buy(self, game: GameClient, buy_times: int) -> BuyEnergyRes:
        """发送购买请求。"""
        view = guarded_send(
            game,
            BuyEnergyPacket(buyTimes=buy_times),
            BuyEnergyRes,
            allowed=ALLOWED_PACKET_IDS,
            forbidden=FORBIDDEN_PACKET_IDS,
            record=self.sent_packet_ids,
            purpose=PACKET_PURPOSE,
            logger=self.logger,
        )
        return view.decode_sub_packet(BuyEnergyRes)

    def execute(self) -> TaskResult:
        # ★ 2026-10-18：0 次不再表示"只读查询"。这里**明确拒绝**，而不是静默
        # 只读（用户以为买了）或静默升成 1（用户没要求就真扣钻石）—— 后者在
        # 花钱这件事上是最不能接受的"猜测"。
        if self.times < 1:
            msg = "购买次数至少 1 次（0 已不再表示只读查询）；本次未发送任何请求"
            self.logger.warning("%s", msg)
            return TaskResult(task=self.name, ok=False, message=msg)

        with self.open_game_client() as game:
            # 步骤 1：查询已购体力次数
            vip_res = self._query_vip_detail(game)
            bought_count = int(vip_res.buyEnergyCount)

            # 步骤 2：购买前查看实际可购次数，杜绝超发
            max_limit = 15
            remaining = max(0, max_limit - bought_count)
            if remaining <= 0:
                msg = (
                    f"今日体力购买次数已达上限（已购买 {bought_count}/{max_limit} 次），"
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
                    "申请购买 %d 次体力超过剩余上限 %d 次，自动截断为 %d 次以避免超量发包",
                    self.times,
                    remaining,
                    actual_to_buy,
                )

            # 步骤 3：计算阶梯费用与预算检查
            cost = calculate_energy_cost(bought_count, actual_to_buy)
            if self.max_cost > 0 and cost > self.max_cost:
                msg = (
                    f"预计消耗 {cost} 钻石，超过上限 {self.max_cost} 钻石，已取消购买"
                )
                self.logger.warning("%s", msg)
                return TaskResult(task=self.name, ok=False, message=msg)

            # 步骤 4：通过消费许可闸门
            self.request_diamond_spend(
                cost,
                reason=f"购买体力 {actual_to_buy} 次，预计消耗 {cost} 钻石",
                purpose=SpendPurpose.BUY_ENERGY,
            )

            # 步骤 5：演练分支
            if self.dry_run:
                msg = (
                    f"[演练] 拟购买体力 {actual_to_buy} 次（已购 {bought_count} 次），"
                    f"预计消耗 {cost} 钻石，产出 {actual_to_buy * ENERGY_PER_PURCHASE} 体力"
                )
                self.logger.info("%s", msg)
                return TaskResult(
                    task=self.name,
                    ok=True,
                    message=msg,
                    data={"planned_times": actual_to_buy, "estimated_cost": cost},
                )

            # 步骤 6：真实发包
            res = self._execute_buy(game, actual_to_buy)
            if not res.is_success():
                msg = f"购买体力失败：errorCode={res.errorCode}"
                self.logger.error("%s", msg)
                return TaskResult(task=self.name, ok=False, message=msg)

            expected_energy = actual_to_buy * ENERGY_PER_PURCHASE
            # ★ 2026-10-18：去掉"按本地阶梯表计"这种免责声明式措辞 ——
            # 用户要的是"花了多少、拿到多少"；本地阶梯表是估算依据，不是文案主角。
            msg = (
                f"成功购买 {actual_to_buy} 次体力（今日累计 {bought_count + actual_to_buy} 次，"
                f"预计消耗 {cost} 钻石、获得 {expected_energy} 体力）"
            )
            self.logger.info("%s", msg)
            return TaskResult(
                task=self.name,
                ok=True,
                message=msg,
                data={
                    "times": actual_to_buy,
                    "energy_expected": expected_energy,
                    "diamond_expected": cost,
                    "total_bought_count": bought_count + actual_to_buy,
                    # 响应原样留一份供排障（口径未实证，**不进文案**）
                    "energy_reported": res.energy_gained(),
                    "diamond_reported": res.diamond_spent(),
                },
            )
