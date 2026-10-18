"""荣耀之巅只读状态（39001 → 39002）：段位 / 排名 / 积分 / 已购次数 / 跳过演出开关。

【为什么单独一个任务（★ 2026-10-18）】
``top_pvp_battle`` 的 ``battles`` 语义已改成"0 = 打到次数用完为止"（与天梯竞技场、
天命对决、失控炼成阵一致，用户要求）。于是它**不再有只读分支**：只要被叫到就是真打。

但 ``/api/player`` 的信息面板仍然需要一份"我现在第几名、多少分"的只读快照。
把这份只读能力放在战斗任务里，就意味着"0 = 只读还是打到完"这种危险的双关；
拆成独立任务后：

- 白名单里**只有 39001**（连 39011 / 39019 都不在 allowed 里），
  结构上不可能打出任何一场，也不可能改动账号设置；
- 与 ``arena_status`` / ``wudou_status`` / ``yimo_status`` 完全同构，
  信息面板那一侧只用换一个任务名。

【为什么不进任务清单】
与其它 ``*_status`` 一样声明为 ``hidden=True``：它零消耗、无参数、不给人勾选，
出现在界面上只会让用户以为"这是个要做的事"。
"""

from __future__ import annotations

import logging
from typing import Final

from client.game_client import GameClient
from client.session import GameSession
from client.spending import SpendPolicy
from models.top_pvp import (
    FORBIDDEN_PACKET_IDS,
    PACKET_PURPOSE,
    GetTopPvpHomePacket,
    GetTopPvpHomeRes,
)
from tasks.base import BaseTask, RunOptions, TaskResult, register_task
from tasks.packet_guard import guarded_send

#: 本任务允许发出的包 —— **只有** 39001（比战斗任务的白名单更窄）。
STATUS_ALLOWED_PACKET_IDS: Final[dict[str, int]] = {"GetTopPvpHomePacket": 39001}


@register_task
class TopPvpStatusTask(BaseTask):
    """荣耀之巅只读状态（零消耗：只发一个 39001 空包）。"""

    name = "top_pvp_status"

    #: 需要登录态（39001 要带会话令牌）
    requires_auth = True

    #: 本任务不存在花钻石的路径：白名单里只有一个只读包
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
        #: 本次实际发出的消息号（测试用它断言"只有 39001"）
        self.sent_packet_ids: list[int] = []

    def _fetch_home(self, game: GameClient) -> GetTopPvpHomeRes:
        """拉主页（唯一的只读包）。"""
        view = guarded_send(
            game,
            GetTopPvpHomePacket(),
            GetTopPvpHomeRes,
            allowed=STATUS_ALLOWED_PACKET_IDS,
            forbidden=FORBIDDEN_PACKET_IDS,
            record=self.sent_packet_ids,
            purpose=PACKET_PURPOSE,
            logger=self.logger,
        )
        return view.decode_sub_packet(GetTopPvpHomeRes)

    def execute(self) -> TaskResult:
        """只读：拉一次主页并汇报。

        :return: 统一任务结果（``ok`` 表示"读到了"，不表示"能打"）。
        """
        with self.open_game_client() as game:
            if self.dry_run:
                return self._dry_run(game)

            home = self._fetch_home(game)
            self.logger.info("荣耀之巅（只读）：%s", home.summary())
            if not home.is_success():
                return TaskResult(
                    task=self.name,
                    ok=False,
                    message=f"拉取荣耀之巅主页失败（errorCode={home.errorCode}）",
                    data={
                        "error_code": home.errorCode,
                        "sent_packets": tuple(self.sent_packet_ids),
                    },
                )

            mine = home.my_rank()
            return TaskResult(
                task=self.name,
                ok=True,
                message=f"荣耀之巅（只读）：{home.summary()}",
                data={
                    "mode": "read_only",
                    "my_ranking": None if mine is None else mine.ranking,
                    "my_rank_class_id": None if mine is None else mine.rankClassID,
                    # 积分字段在协议里就叫 ``rankPoing``（不是笔误）
                    "my_points": None if mine is None else mine.rankPoing,
                    "buy_challenge_count": home.buyChallengeCount,
                    "skip_battle_enabled": home.isSkipBattle,
                    "sent_packets": tuple(self.sent_packet_ids),
                },
            )

    def _dry_run(self, game: GameClient) -> TaskResult:
        """演练：打印将要发出的字节与安全边界，**一个包都不发**。"""
        packet = GetTopPvpHomePacket()
        print("【荣耀之巅（只读） · 演练】未发送任何请求")
        print()
        print(f"将要发送：{PACKET_PURPOSE[39001]}")
        print(game.build(packet).describe())
        print()
        print("【安全边界】")
        print(f"  · 本任务允许发出的消息号：{sorted(STATUS_ALLOWED_PACKET_IDS.values())}")
        print(
            f"  · 明确禁发（含花钻石买次数的 39005 / 会改动配置的 39009）"
            f"：{sorted(FORBIDDEN_PACKET_IDS.values())}"
        )
        print("  · 连挑战包 39007 都不在白名单里 —— 本任务没有「打」的能力")
        print()
        print("—— 以上为演练输出，未发送任何请求 ——")
        return TaskResult(
            task=self.name,
            ok=True,
            message="演练完成：计划发出 1 个包（只读 39001），未发送",
            data={"planned_packets": (39001,), "sent_packets": ()},
        )
