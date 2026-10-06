"""天命对决只读任务：读剩余挑战次数 / NPC 次数 / 已购买 / 我的名次 / 结算时间。

【这个任务做什么】
``26031 WuDouHome{groupID=-1}`` → ``26032``（剩余挑战次数、分组）
``26033 WuDouChallengeHome{}``（空子包）→ ``26034``（剩余次数、NPC 次数、我的名次、结算时间）

两次都只是**查询**；``26031`` 的 ``groupID`` 用抓包实测值 ``-1``。

【为什么不进战】
``26038`` 会下发双方角色明细、``26039`` 要客户端上报带存证的结果（字段与竞技场 ``26019``
逐字相同）；更麻烦的是实测抓包里**进战/结算包根本没走 HTTP 通道**
（疑似握手响应里那个 ``serverPort=1888`` 的长连接）。详见 ``notes/glory_summit_capture.md``
第三、八节。本任务只发两个只读包，进战/结算/买次数都在禁发清单里。
"""

from __future__ import annotations

import logging

from client.game_client import GameClient
from client.session import GameSession
from client.spending import SpendPolicy
from models.wudou import (
    ALLOWED_PACKET_IDS,
    FORBIDDEN_PACKET_IDS,
    PACKET_PURPOSE,
    WuDouChallengeHome,
    WuDouChallengeHomeRes,
    WuDouHome,
    WuDouHomeRes,
)
from tasks.base import BaseTask, RunOptions, TaskResult, register_task
from tasks.packet_guard import guarded_send


@register_task
class WuDouStatusTask(BaseTask):
    """天命对决只读状态（零消耗：不进战、不买次数）。"""

    name = "wudou_status"

    #: 需要登录态
    requires_auth = True

    #: 本任务不存在花钻石的路径（白名单里只有两个只读包）
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
    def _send(self, game: GameClient, packet: object, expect: type) -> object:
        """统一走白名单守卫（``packet`` 的类型由各调用点给出，见 models/wudou.py）。"""
        return guarded_send(
            game,
            packet,  # type: ignore[arg-type]
            expect,  # type: ignore[arg-type]
            allowed=ALLOWED_PACKET_IDS,
            forbidden=FORBIDDEN_PACKET_IDS,
            record=self.sent_packet_ids,
            purpose=PACKET_PURPOSE,
            logger=self.logger,
        )

    def _fetch(self, game: GameClient) -> tuple[WuDouHomeRes, WuDouChallengeHomeRes]:
        """拉两页只读数据：主页 + 挑战页。"""
        home_view = self._send(game, WuDouHome.for_default_group(), WuDouHomeRes)
        home = home_view.decode_sub_packet(WuDouHomeRes)  # type: ignore[attr-defined]
        challenge_view = self._send(game, WuDouChallengeHome(), WuDouChallengeHomeRes)
        challenge = challenge_view.decode_sub_packet(WuDouChallengeHomeRes)  # type: ignore[attr-defined]
        return home, challenge

    def execute(self) -> TaskResult:
        """只读：拉主页与挑战页并汇报。"""
        with self.open_game_client() as game:
            if self.dry_run:
                return self._dry_run(game)

            home, challenge = self._fetch(game)
            self.logger.info("天命对决主页：%s", home.summary())
            self.logger.info("天命对决挑战页：%s", challenge.summary())
            return TaskResult(
                task=self.name,
                ok=True,
                message=(
                    f"天命对决（只读）：{challenge.summary()}；"
                    f"分组={home.myGroupID}，主页剩余次数={home.challenge_times}。"
                    "本任务不进战、不购买次数"
                ),
                data={
                    "challenge_times": challenge.challenge_times,
                    "npc_times": challenge.npc_times,
                    "buyed_count": challenge.buyedCount,
                    "my_ranking": challenge.myRanking,
                    "opponent_count": challenge.opponent_count(),
                    "settlement_time": challenge.settlement_time,
                    "home_challenge_times": home.challenge_times,
                    "my_group_id": home.myGroupID,
                    "week_rank_count": len(home.weekRanking),
                    "sent_packets": tuple(self.sent_packet_ids),
                },
            )

    def _dry_run(self, game: GameClient) -> TaskResult:
        """演练：打印两个只读包的实际字节与安全边界，**不发包**。"""
        packets = (WuDouHome.for_default_group(), WuDouChallengeHome())
        print("【天命对决 · 演练】只读，未发送任何请求")
        print()
        for packet in packets:
            number = 26031 if isinstance(packet, WuDouHome) else 26033
            print(f"将要发送：{PACKET_PURPOSE[number]}")
            print(game.build(packet).describe())
            print()
        print("【安全边界】")
        print(f"  · 允许发出的消息号：{sorted(ALLOWED_PACKET_IDS.values())}")
        print(f"  · 明确禁发（含花钻石买次数的 26047）：{sorted(FORBIDDEN_PACKET_IDS.values())}")
        print("  · 进战 26037 / 结算 26039 未建模；实测它们也不走 HTTP 通道")
        print()
        print("—— 以上为演练输出，未发送任何请求 ——")
        return TaskResult(
            task=self.name,
            ok=True,
            message="演练完成：计划发出 2 个包（只读 26031 / 26033），未发送",
            data={"planned_packets": (26031, 26033), "sent_packets": ()},
        )
