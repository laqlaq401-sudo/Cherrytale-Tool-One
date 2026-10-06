"""失控炼成阵只读任务：读剩余次数 / 积分 / 排名 / boss 数据有无。

【这个任务做什么】
``22101 GetYimoState``（空包）→ ``22102``，打印：

    剩余次数（remainCount）、已购买（buyExtraCount）、积分（nowScore）、
    排名（nowRank）、boss 数据有无、奖励条目数、下次购买价格（**仅展示**）

【为什么不进战】
结果上报 ``22109`` 需要 32 位 hex 的 ``token``，而它的算法**尚未拿到**
（vCode 那套公式不适用，见 ``notes/battle_engine_project.md`` 的 U1）
—— 这是本模式唯一缺的东西（存证实测可以为空）。本任务只发一个只读包。
"""

from __future__ import annotations

import logging

from client.game_client import GameClient
from client.session import GameSession
from client.spending import SpendPolicy
from models.yimo import (
    ALLOWED_PACKET_IDS,
    FORBIDDEN_PACKET_IDS,
    PACKET_PURPOSE,
    GetYimoState,
    GetYimoStateRes,
)
from tasks.base import BaseTask, RunOptions, TaskResult, register_task
from tasks.packet_guard import guarded_send


@register_task
class YimoStatusTask(BaseTask):
    """失控炼成阵只读状态（零消耗：不进战、不买次数）。"""

    name = "yimo_status"

    #: 需要登录态
    requires_auth = True

    #: 本任务不存在花钻石的路径（白名单里只有一个只读包）
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
        #: 本次实际发出的消息号（测试用它断言"没有越界发包"）
        self.sent_packet_ids: list[int] = []

    # ------------------------------------------------------------------
    def _fetch(self, game: GameClient) -> GetYimoStateRes:
        """拉取失控炼成阵状态（唯一的只读包）。"""
        view = guarded_send(
            game,
            GetYimoState(),
            GetYimoStateRes,
            allowed=ALLOWED_PACKET_IDS,
            forbidden=FORBIDDEN_PACKET_IDS,
            record=self.sent_packet_ids,
            purpose=PACKET_PURPOSE,
            logger=self.logger,
        )
        return view.decode_sub_packet(GetYimoStateRes)

    def execute(self) -> TaskResult:
        """只读：拉一次状态并汇报。"""
        with self.open_game_client() as game:
            if self.dry_run:
                return self._dry_run(game)

            state = self._fetch(game)
            self.logger.info("失控炼成阵：%s", state.summary())
            return TaskResult(
                task=self.name,
                ok=True,
                message=(
                    f"失控炼成阵（只读）：{state.summary()}。"
                    "本任务不进战、不购买次数 —— 战斗需要 token 签名，见 notes/battle_engine_project.md"
                ),
                data={
                    "remain_count": state.remainCount,
                    "buy_extra_count": state.buyExtraCount,
                    "now_score": state.nowScore,
                    "now_rank": state.nowRank,
                    "has_boss_data": state.has_boss_data(),
                    "reward_item_count": len(state.rewardData),
                    "notice": tuple(state.popUpInfo),
                    "sent_packets": tuple(self.sent_packet_ids),
                },
            )

    def _dry_run(self, game: GameClient) -> TaskResult:
        """演练：打印将要发出的字节与安全边界，**不发包**。"""
        packet = GetYimoState()
        print("【失控炼成阵 · 演练】只读，未发送任何请求")
        print()
        print(f"将要发送：{PACKET_PURPOSE[22101]}")
        print(game.build(packet).describe())
        print()
        print("【安全边界】")
        print(f"  · 允许发出的消息号：{sorted(ALLOWED_PACKET_IDS.values())}")
        print(f"  · 明确禁发（含花钻石买次数的 22103）：{sorted(FORBIDDEN_PACKET_IDS.values())}")
        print("  · 进战 22107 / 结算 22109 未建模 —— 结算依赖尚未拿到的 token 签名")
        print()
        print("—— 以上为演练输出，未发送任何请求 ——")
        return TaskResult(
            task=self.name,
            ok=True,
            message="演练完成：计划发出 1 个包（只读 22101），未发送",
            data={"planned_packets": (22101,), "sent_packets": ()},
        )
