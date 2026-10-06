"""背包货币只读任务：金币 / 钻石 / 体力（消息号 2007 → 2008）。

【这个任务做什么】
``2007 GetAllItemPacket``（**空报文**）→ ``2008 GetAllItemRes``，从 ``itemList``
里读三个 itemID 的数量：

    金币 ``COINS_ID``   = 200000050
    钻石 ``DIAMOND_ID`` = 200000001
    体力 ``ENERGY_ID``  = 200000002

【它为什么存在】
网页 / 窗口顶部的「信息展示」面板要显示"我还有多少资源"。这件事必须在**跑任务之前**
就看得见 —— 否则"扫荡会不会把体力花光""这个箱子值不值得开"只能靠猜。

【为什么只发一个空包】
``GetAllItemPacket`` 是空报文，服务端只回一份背包快照，**不改动任何东西**：
不消耗、不领取、不购买。所以本任务的白名单只有一个包，
并把 ``2001 UseItemPacket`` / ``2005 MixItemsPacket`` /
``2011 HarvestDreamStonePacket`` 显式列进禁发清单 —— 将来有人"顺手加功能"时，
越界会在**运行期**立刻报错，而不是悄悄改动了背包。
"""

from __future__ import annotations

import logging

from client.game_client import GameClient
from client.session import GameSession
from client.spending import SpendPolicy
from models.wallet import (
    ALLOWED_PACKET_IDS,
    FORBIDDEN_PACKET_IDS,
    PACKET_PURPOSE,
    GetAllItemPacket,
    GetAllItemRes,
)
from tasks.base import BaseTask, RunOptions, TaskResult, register_task
from tasks.packet_guard import guarded_send


@register_task
class WalletTask(BaseTask):
    """背包货币只读查询（金币 / 钻石 / 体力）。零消耗：只发一个空包。"""

    name = "wallet"

    #: 读背包当然需要登录态（要带令牌与 playerId）
    requires_auth = True

    #: 不存在任何花钱路径（白名单里只有一个只读包）
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
    # 只读取数
    # ------------------------------------------------------------------
    def _fetch(self, game: GameClient) -> GetAllItemRes:
        """拉取背包全量列表（唯一的只读包）。"""
        view = guarded_send(
            game,
            GetAllItemPacket(),
            GetAllItemRes,
            allowed=ALLOWED_PACKET_IDS,
            forbidden=FORBIDDEN_PACKET_IDS,
            record=self.sent_packet_ids,
            purpose=PACKET_PURPOSE,
            logger=self.logger,
        )
        return view.decode_sub_packet(GetAllItemRes)

    def execute(self) -> TaskResult:
        """只读：拉一次背包并汇报三种资源。"""
        with self.open_game_client() as game:
            if self.dry_run:
                return self._dry_run(game)

            result = self._fetch(game)
            self.logger.info("背包货币：%s", result.summary())
            return TaskResult(
                task=self.name,
                ok=True,
                message=f"只读完成：{result.summary()}",
                data={
                    "coins": result.coins(),
                    "diamonds": result.diamonds(),
                    "stamina": result.stamina(),
                    "item_count": len(result.itemList),
                    "sent_packets": tuple(self.sent_packet_ids),
                },
            )

    def _dry_run(self, game: GameClient) -> TaskResult:
        """演练：只组装并打印将要发送的字节，**不联网**。"""
        request = game.build(GetAllItemPacket())
        print(f"将要发送：{PACKET_PURPOSE[2007]}")
        print(request.describe())
        print()
        print("—— 以上为演练输出，未发送任何请求 ——")
        return TaskResult(
            task=self.name,
            ok=True,
            message=f"演练完成：2007 请求体 {len(request.body)} 字节，未发送",
            data={"planned_packets": (2007,), "sent_packets": ()},
        )
