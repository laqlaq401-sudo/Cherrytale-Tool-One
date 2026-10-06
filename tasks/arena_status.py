"""竞技场只读任务：读可挑战次数 / 已购次数 / 对手数量。

【这个任务做什么】
``26001 PVPHomePacket``（空包）→ ``26002 PVPHomeRes``，打印：

    可挑战次数（challengeCount）、已购买（buyedCount）、已刷新（reflashCount）、
    对手数量、战力字段、公会名

【为什么不进战】
竞技场的战斗是客户端本地结算后上报（7.2KB 战报 + 13.9 万字节逐施法存证 + token），
没有战斗引擎就过不了服务端校验 —— 详见 ``models/pvp.py`` 与
``notes/glory_summit_capture.md`` 第二、四节。本任务**只发一个只读包**：
``models/pvp.py`` 的白名单里就只有它，进战/结算/买次数全在禁发清单里。
"""

from __future__ import annotations

import logging

from client.game_client import GameClient
from client.session import GameSession
from client.spending import SpendPolicy
from models.pvp import (
    ALLOWED_PACKET_IDS,
    FORBIDDEN_PACKET_IDS,
    PACKET_PURPOSE,
    PVPHomePacket,
    PVPHomeRes,
)
from tasks.base import BaseTask, RunOptions, TaskResult, register_task
from tasks.packet_guard import guarded_send


@register_task
class ArenaStatusTask(BaseTask):
    """竞技场只读状态（零消耗：不进战、不买次数）。"""

    name = "arena_status"

    #: 需要登录态
    requires_auth = True

    #: 本任务不存在花钻石的路径（白名单里只有一个只读包，买次数的包连模型都没有）
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
    def _fetch(self, game: GameClient) -> PVPHomeRes:
        """拉取竞技场主页（唯一的只读包）。"""
        view = guarded_send(
            game,
            PVPHomePacket(),
            PVPHomeRes,
            allowed=ALLOWED_PACKET_IDS,
            forbidden=FORBIDDEN_PACKET_IDS,
            record=self.sent_packet_ids,
            purpose=PACKET_PURPOSE,
            logger=self.logger,
        )
        return view.decode_sub_packet(PVPHomeRes)

    def execute(self) -> TaskResult:
        """只读：拉一次主页并汇报。

        :return: 统一任务结果（``ok`` 表示"读到了"，不表示"能打"）。
        """
        with self.open_game_client() as game:
            if self.dry_run:
                return self._dry_run(game)

            home = self._fetch(game)
            self.logger.info("竞技场：%s", home.summary())
            return TaskResult(
                task=self.name,
                ok=True,
                message=(
                    f"竞技场（只读）：{home.summary()}。"
                    "本任务不进战、不购买次数 —— 战斗需要客户端本地结算，见 notes/battle_engine_project.md"
                ),
                data={
                    "challenge_count": home.challengeCount,
                    "buyed_count": home.buyedCount,
                    "reflash_count": home.reflashCount,
                    "opponent_count": home.opponent_count(),
                    "my_fighting": home.myFighting,
                    "alliance_name": home.allianceName,
                    "sent_packets": tuple(self.sent_packet_ids),
                },
            )

    def _dry_run(self, game: GameClient) -> TaskResult:
        """演练：打印将要发出的字节与安全边界，**一个包都不发**。"""
        packet = PVPHomePacket()
        print("【竞技场 · 演练】只读，未发送任何请求")
        print()
        print(f"将要发送：{PACKET_PURPOSE[26001]}")
        print(game.build(packet).describe())
        print()
        print("【安全边界】")
        print(f"  · 允许发出的消息号：{sorted(ALLOWED_PACKET_IDS.values())}")
        print(f"  · 明确禁发（含花钻石买次数的 26015）：{sorted(FORBIDDEN_PACKET_IDS.values())}")
        print("  · 进战 26003 / 结算 26019 未建模 —— 本任务没有「打」的能力")
        print()
        print("—— 以上为演练输出，未发送任何请求 ——")
        return TaskResult(
            task=self.name,
            ok=True,
            message="演练完成：计划发出 1 个包（只读 26001），未发送",
            data={"planned_packets": (26001,), "sent_packets": ()},
        )
