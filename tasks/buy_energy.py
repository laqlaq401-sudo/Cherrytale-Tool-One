"""钻石购买体力任务（消息号 24011/24012 VIP详情 与 24005/24006 购买体力）。

【两步流程】
1. 先发 ``VIPDetailPacket(24011)``（空报文）拉取今日已购次数 ``buyEnergyCount``；
2. 若 ``times == 0``：**只读**汇报当前已购次数与下次费用，不发写包、不扣钻石；
3. 若 ``times > 0``：
   - 按阶梯费用表（``models.buy_energy.calculate_energy_cost``）估算所需钻石；
   - 若设置了 ``max_cost`` 且超出上限，则提前终止；
   - 通过 ``request_diamond_spend(..., purpose=SpendPurpose.BUY_ENERGY)`` 过消费闸门；
   - 演练模式（dry_run）仅打印意图，真实模式发送 ``BuyEnergyPacket(24005)`` 执行购买。
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
    """用钻石购买体力任务。默认只读（times=0 时不消耗钻石）。"""

    name = "buy_energy"

    requires_auth = True

    #: 存在花钻石分支（times > 0 时），但 times=0 分支零消耗
    spends_diamond = False

    #: 用途声明：只有 BUY_ENERGY 授权时才放行购买分支
    diamond_purposes = (SpendPurpose.BUY_ENERGY,)

    opt_in_hint: ClassVar[str] = "需 --times > 0 且 --allow-diamond 才会用钻石购买体力（默认只读查询）"

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
        with self.open_game_client() as game:
            # 步骤 1：查询已购体力次数
            vip_res = self._query_vip_detail(game)
            bought_count = int(vip_res.buyEnergyCount)
            next_single_cost = calculate_energy_cost(bought_count, 1)

            # 步骤 2：只读分支
            if self.times <= 0:
                msg = (
                    f"只读查询：今日已购买体力 {bought_count} 次；"
                    f"下一次购买预计消耗 {next_single_cost} 钻石"
                    f"（产出 {ENERGY_PER_PURCHASE} 体力）"
                )
                self.logger.info("%s", msg)
                return TaskResult(
                    task=self.name,
                    ok=True,
                    message=msg,
                    data={
                        "bought_count": bought_count,
                        "next_single_cost": next_single_cost,
                    },
                )

            # 步骤 3：购买分支，先算费用
            cost = calculate_energy_cost(bought_count, self.times)
            if self.max_cost > 0 and cost > self.max_cost:
                msg = (
                    f"预计消耗 {cost} 钻石，超过上限 {self.max_cost} 钻石，已取消购买"
                )
                self.logger.warning("%s", msg)
                return TaskResult(task=self.name, ok=False, message=msg)

            # 步骤 4：通过消费许可闸门
            self.request_diamond_spend(
                cost,
                reason=f"购买体力 {self.times} 次，预计消耗 {cost} 钻石",
                purpose=SpendPurpose.BUY_ENERGY,
            )

            # 步骤 5：演练分支
            if self.dry_run:
                msg = (
                    f"[演练] 拟购买体力 {self.times} 次（已购 {bought_count} 次），"
                    f"预计消耗 {cost} 钻石，产出 {self.times * ENERGY_PER_PURCHASE} 体力"
                )
                self.logger.info("%s", msg)
                return TaskResult(
                    task=self.name,
                    ok=True,
                    message=msg,
                    data={"planned_times": self.times, "estimated_cost": cost},
                )

            # 步骤 6：真实发包
            res = self._execute_buy(game, self.times)
            if not res.is_success():
                msg = f"购买体力失败：errorCode={res.errorCode}"
                self.logger.error("%s", msg)
                return TaskResult(task=self.name, ok=False, message=msg)

            # ★ 2026-10-04 口径修正（B 组）：
            # 24006 的 ``addList`` / ``costList`` 数量语义**尚未实证** ——
            # 6006 / 11010 / 37020 都已证实回的是"操作后持有量"，24006 很可能相同。
            # 所以**不把响应里的数字当"获得/消耗"念出来**（那正是"获得数量错误
            # 报告成本身拥有数量"的成因），只报我们完全确定的事实：买了几次。
            # 花费与产出改用**本地阶梯表 / 配置常量**算，并明确标注来源。
            expected_energy = self.times * ENERGY_PER_PURCHASE
            msg = (
                f"成功购买 {self.times} 次体力（今日累计 {bought_count + self.times} 次；"
                f"按本地阶梯表计 {cost} 钻石、{expected_energy} 体力）"
            )
            self.logger.info("%s", msg)
            return TaskResult(
                task=self.name,
                ok=True,
                message=msg,
                data={
                    "times": self.times,
                    "energy_expected": expected_energy,
                    "diamond_expected": cost,
                    "total_bought_count": bought_count + self.times,
                    # 响应原样留一份供排障（口径未实证，**不进文案**）
                    "energy_reported": res.energy_gained(),
                    "diamond_reported": res.diamond_spent(),
                },
            )
