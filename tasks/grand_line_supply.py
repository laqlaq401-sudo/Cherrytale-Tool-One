"""辉煌航迹搜集物资自动领取任务。

【业务说明】
辉煌航迹系统（SystemID 77）中随着时间推移自动积攒「蒐集物資」（远航补给）。
每到整点累计 1 次，上限 24 次。本任务在每日巡检时一键领取。零钻石消耗、纯领取。

【★ 2026-10-03 流程修订（依据 10.3 抓包 ``captures/辉煌航迹奖励领取.saz``）】
真实客户端的领取流程是 ``34019（空包，拉奖励目录）→ 34021（领取）``，
**并不发 34015 主页查询**。旧流程用 34016.rewardCount 判断"有没有可领"，
≤0 就报"暂无可领"—— 结果任务永远停在"暂无可领"。
现在改为镜像真实流程：

1. ``34019`` 拉奖励目录（顺带确认功能开放、给用户展示能领什么）；
2. ``34021 {type=1, rewardID=-1}`` 直接领取，由服务端 errorCode 做最终裁决。

【★ 同修的第二个 bug】旧代码解析奖励时读 ``GetObjClass.id/.count`` ——
该模型根本没有这两个属性（奖励明细在 ``.itemList[].itemID/.itemAmount``），
一旦真领成功就会 AttributeError 崩溃。
"""

from __future__ import annotations

import logging
from typing import Any, Final

from client.game_client import GameClient
from client.session import GameSession
from models.envelope import EnvelopeView
from models.game_packet import ProtoMessage
from models.grand_line import (
    ALLOWED_PACKET_IDS,
    FORBIDDEN_PACKET_IDS,
    PACKET_PURPOSE,
    GrandLineCountRewardPacket,
    GrandLineCountRewardRes,
    GrandLineGetCountRewardPacket,
    GrandLineGetCountRewardRes,
)
from tasks.base import BaseTask, TaskResult, count_item_kinds, register_task
from tasks.packet_guard import guarded_send

_LOGGER: Final[logging.Logger] = logging.getLogger("tasks.grand_line_supply")


@register_task
class GrandLineSupplyTask(BaseTask):
    """领取辉煌航迹远航搜集物资奖励。"""

    name: Final[str] = "grand_line_supply"
    requires_auth: Final[bool] = True
    spends_diamond: Final[bool] = False

    def __init__(
        self,
        session: GameSession | None = None,
        *,
        logger: logging.Logger | None = None,
        dry_run: bool = False,
        spend_policy: Any = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            session=session or GameSession(),
            logger=logger,
            dry_run=dry_run,
            spend_policy=spend_policy,
        )

    def emit(self, msg: str) -> None:
        self.logger.info(msg)

    def _guarded_send(
        self,
        game: GameClient,
        packet: ProtoMessage,
        expect: type[ProtoMessage],
    ) -> EnvelopeView:
        return guarded_send(
            game,
            packet,
            expect,
            allowed=ALLOWED_PACKET_IDS,
            forbidden=FORBIDDEN_PACKET_IDS,
            purpose=PACKET_PURPOSE,
            logger=_LOGGER,
        )

    def execute(self) -> TaskResult:
        """执行辉煌航迹物资领取流程。"""
        with self.open_game_client() as game:
            return self._run(game)

    def _run(self, game: GameClient) -> TaskResult:
        # 1. 拉奖励目录（34019 → 34020）—— 领取前的实际查询（镜像 10.3 抓包）
        self.emit("正在查询辉煌航迹搜集物资奖励目录 (34019)...")
        catalog_view = self._guarded_send(
            game, GrandLineCountRewardPacket(), GrandLineCountRewardRes
        )
        catalog_res = catalog_view.decode_sub_packet(GrandLineCountRewardRes)

        catalog = catalog_res.rewardItems
        if not catalog:
            msg = "辉煌航迹奖励目录为空（搜集物资可能未开放），跳过领取"
            self.emit(msg)
            return TaskResult(
                task=self.name,
                ok=True,
                message=msg,
                data={"catalog_size": 0},
            )
        self.emit(f"奖励目录共 {len(catalog)} 项，正在领取...")

        if self.dry_run:
            msg = f"【演练】检测到辉煌航迹奖励目录 {len(catalog)} 项，演练模式跳过领取"
            self.emit(msg)
            return TaskResult(
                task=self.name,
                ok=True,
                message=msg,
                data={"catalog_size": len(catalog), "dry_run": True},
            )

        # 2. 发包领取物资（type=1, rewardID=-1：一次性领取全部累计）
        claim_view = self._guarded_send(
            game,
            GrandLineGetCountRewardPacket(type=1, rewardID=-1),
            GrandLineGetCountRewardRes,
        )
        claim_res = claim_view.decode_sub_packet(GrandLineGetCountRewardRes)

        if not claim_res.is_success():
            return TaskResult(
                task=self.name,
                ok=False,
                message=f"领取辉煌航迹物资失败（errorCode={claim_res.errorCode}）",
                data={"error_code": claim_res.errorCode, "catalog_size": len(catalog)},
            )

        # ★ 2026-10-04 单位/口径修正：
        # ① 原来报的是「获得 {len(reward_items)} 种物品」—— ``len()`` 是**条数**，
        #    同一 itemID 出现两次就会被算成"两种"。要报"种"必须**去重**，
        #    这里改用 count_item_kinds（= 不同 itemID 的个数）。
        # ② 34021 的 itemAmount 数量口径**尚未实证**（同源 6006/11010/37020
        #    已证实是"操作后持有量"）→ 不报数量，只报种数。
        kinds = count_item_kinds(claim_res.getObjList)
        msg = (
            f"成功领取辉煌航迹搜集物资"
            f"（获得 {kinds} 种物品，目录 {len(catalog)} 项）"
        )
        self.emit(msg)
        return TaskResult(
            task=self.name,
            ok=True,
            message=msg,
            data={
                "catalog_size": len(catalog),
                "item_kinds": kinds,
            },
        )
