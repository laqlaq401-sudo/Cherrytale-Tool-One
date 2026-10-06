"""荣耀之巅宝箱自动领取任务。

【需求与设计原则】
1. 消息号 39003 ReceiveTopPvpRewardPacket → 39004 ReceiveTopPvpRewardRes；
2. 每日登录时执行一次，无须重复轮询；
3. 严格受白名单保护（仅发送 39003，绝不发 39005 购买挑战次数）；
4. 零钻石消耗保证。
"""

from __future__ import annotations

import logging
from typing import Final

from client.game_client import GameClient
from client.session import GameSession
from client.spending import SpendPolicy
from models.top_pvp import (
    ALLOWED_PACKET_IDS,
    FORBIDDEN_PACKET_IDS,
    PACKET_PURPOSE,
    ReceiveTopPvpRewardPacket,
    ReceiveTopPvpRewardRes,
)
from tasks.base import BaseTask, RunOptions, TaskResult, register_task
from tasks.packet_guard import guarded_send

_LOGGER: Final[logging.Logger] = logging.getLogger("tasks.top_pvp_box")


@register_task
class TopPvpBoxTask(BaseTask):
    """荣耀之巅宝箱自动领取任务（零消耗，每日一次）。"""

    name = "top_pvp_box"

    #: 需要登录态
    requires_auth = True

    #: 绝不花钻石
    spends_diamond = False

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
        self.sent_packet_ids: list[int] = []

    def _claim_reward(self, game: GameClient) -> ReceiveTopPvpRewardRes:
        """发送 39003 请求领取荣耀之巅宝箱。"""
        view = guarded_send(
            game,
            ReceiveTopPvpRewardPacket(),
            ReceiveTopPvpRewardRes,
            allowed=ALLOWED_PACKET_IDS,
            forbidden=FORBIDDEN_PACKET_IDS,
            record=self.sent_packet_ids,
            purpose=PACKET_PURPOSE,
            logger=self.logger,
            include_defaults=True,
        )
        return view.decode_sub_packet(ReceiveTopPvpRewardRes)

    def execute(self) -> TaskResult:
        """主流程：发送 39003 领取荣耀之巅宝箱。"""
        with self.open_game_client() as game:
            if self.dry_run:
                return self._dry_run(game)

            res = self._claim_reward(game)
            if res.is_success():
                # ★ 2026-10-04 单位修正（C 组）：
                # ``addList`` 的类型是 ``list[bytes]`` —— 每条是**一个未解析的
                # GetObjClass 原始字节**，所以 ``len()`` 得到的是**组数**，
                # 不是"件数"。原来写成「获得 N 项奖励道具」正是"把组数当件数"。
                #
                # 既然每条都没解析，连数量口径都无从谈起（既不知道有几件、
                # 也不知道 itemAmount 是增量还是余额）→ 运行视图**不报数字**，
                # 只留一个名字即口径的结构化字段供程序消费。
                group_count = len(res.addList)
                self.logger.debug(
                    "荣耀之巅宝箱领取成功：响应含 %d 组奖励（原始字节，未解析）",
                    group_count,
                )
                return TaskResult(
                    task=self.name,
                    ok=True,
                    message="荣耀之巅宝箱领取成功（零钻石消耗）",
                    data={
                        "reward_group_count": group_count,
                        "sent_packets": tuple(self.sent_packet_ids),
                    },
                )

            # 服务端返回非 0 错误码通常表示今日已领过或暂无待领宝箱
            self.logger.info("荣耀之巅宝箱暂无可领或今日已领（服务端返回码：%d）", res.errorCode)
            return TaskResult(
                task=self.name,
                ok=True,
                message=f"荣耀之巅宝箱：今日已领取或当前暂无待领奖励（服务端返回码：{res.errorCode}）",
                data={
                    "error_code": res.errorCode,
                    "sent_packets": tuple(self.sent_packet_ids),
                },
            )

    def _dry_run(self, game: GameClient) -> TaskResult:
        """演练：打印计划发送的报文与安全检查，不联网。"""
        packet = ReceiveTopPvpRewardPacket()
        print("【荣耀之巅宝箱 · 演练】未发送任何请求")
        print()
        print(f"计划发送：{PACKET_PURPOSE[39003]}")
        print(game.build(packet, include_defaults=True).describe())
        print()
        print("【安全边界】")
        print(f"  · 允许发出的消息号：{sorted(ALLOWED_PACKET_IDS.values())}")
        print(f"  · 明确禁发（含花钻石买次数的 39005）：{sorted(FORBIDDEN_PACKET_IDS.values())}")
        print("  · 每日执行一次，零钻石消耗")
        print()
        return TaskResult(
            task=self.name,
            ok=True,
            message="演练完成：计划发出 39003 领取荣耀之巅宝箱（未实际发送）",
            data={"planned_packets": (39003,), "sent_packets": ()},
        )
