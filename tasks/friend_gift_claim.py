"""好友赠礼一键领取（10009 → 10010，零消耗）。

【为什么单独一个任务（★ 2026-10-18 用户要求）】
做日常任务「好友贈禮 3 次」时，抓包里同时录到了**一键领取**的操作 ——
它与「一键全送」是**同一个报文**，只差 ``actionType``：

========================================  ==================================
抓包会话                                   请求
========================================  ==================================
``raw/09``（一键全送，已由每日任务实现）   ``{actionType=4, playerID=-1}``
``raw/13``（一键领取，本任务）             ``{actionType=3, playerID=-1}``
========================================  ==================================

证据文件：``captures/好友礼物 一键赠送 一键领取.saz``。
``raw/13`` 的响应：``errorCode=0``、``friendList`` **25 个 playerID**、
``itemList`` 1 组（``itemID=200000002`` 体力 × 67）。

「送」与「收」是两件事，因此**不合并**：
``daily_task_auto`` 继续负责送 3 次（补日常任务缺口），本任务只负责把
好友送来的礼物一次领完，可单独勾选、单独执行。

【三条执行纪律】
1. **只发一个包**：白名单里只有 10009 —— 连好友列表 10001 都不在名单里
   （响应自带 ``friendList``，不需要先查列表，少一个包就少一处失败面）；
2. **软成功**（与 ``top_pvp_box`` 同口径）：``errorCode != 0`` 视为
   "今天没有可领的"而不是故障 —— 不打红叉，只把返回码报出来；
3. **不报具体数量**：``itemAmount`` 是"本次获得"还是"领取后持有量"尚未实证
   （见 ``models/reward.py`` 的口径表），所以道具文案走
   ``QuantityScope.UNKNOWN``，最多只说「获得 N 件道具」。
"""

from __future__ import annotations

import logging
from typing import Final

from client.game_client import GameClient
from client.session import GameSession
from client.spending import SpendPolicy
from models.friend import (
    ALL_GIFT_TARGET,
    FORBIDDEN_PACKET_IDS,
    OPERATION_GAIN_ALL_GIFT,
    PACKET_PURPOSE,
    FriendOperationPacket,
    FriendOperationRes,
)
from models.reward import QuantityScope, reward_phrase
from tasks.base import BaseTask, RunOptions, TaskResult, register_task
from tasks.packet_guard import guarded_send

_LOGGER: Final[logging.Logger] = logging.getLogger("tasks.friend_gift_claim")

#: 本任务允许发出的包 —— **只有** 10009（比 ``models/friend.py`` 的模块白名单更窄，
#: 那份还含 10001 好友列表；本任务不需要它，就不该有能力发它）。
CLAIM_ALLOWED_PACKET_IDS: Final[dict[str, int]] = {"FriendOperationPacket": 10009}


@register_task
class FriendGiftClaimTask(BaseTask):
    """好友赠礼一键领取（零消耗：只发一个 10009）。"""

    name = "friend_gift_claim"

    #: 需要登录态（10009 要带会话令牌）
    requires_auth = True

    #: 本任务没有花钻石的路径：白名单里只有一个领取包
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
        #: 本次实际发出的消息号（测试用它断言"只有 10009"）
        self.sent_packet_ids: list[int] = []

    def _claim(self, game: GameClient) -> FriendOperationRes:
        """发送一键领取（10009 → 10010）。"""
        view = guarded_send(
            game,
            FriendOperationPacket(
                actionType=OPERATION_GAIN_ALL_GIFT, playerID=ALL_GIFT_TARGET
            ),
            FriendOperationRes,
            allowed=CLAIM_ALLOWED_PACKET_IDS,
            forbidden=FORBIDDEN_PACKET_IDS,
            record=self.sent_packet_ids,
            purpose=PACKET_PURPOSE,
            logger=self.logger,
            # 两个字段都不是 0，字节上与抓包一致；显式声明是纪律（0 值字段也不能省）
            include_defaults=True,
        )
        # 与 yimo_box 同款：把响应的一行摘要留在日志里，失败时才有现场可查。
        # 诊断日志绝不能因为视图没有 describe()（测试替身）就把任务搞崩。
        describe = getattr(view, "describe", None)
        if callable(describe):
            try:
                self.logger.info("10010 原始响应：%s", describe())
            except Exception:  # noqa: BLE001 - 排障信息不该影响业务
                pass
        return view.decode_sub_packet(FriendOperationRes)

    def execute(self) -> TaskResult:
        """主流程：一键领取 → 汇报领到几位好友 / 是否无可领。"""
        with self.open_game_client() as game:
            if self.dry_run:
                return self._dry_run(game)
            return self._run(game)

    def _run(self, game: GameClient) -> TaskResult:
        """执行主体（拆出来是为了单测能直接传入假客户端，不碰网络）。"""
        res = self._claim(game)
        friends = list(res.friendList)
        data = {
            "error_code": res.errorCode,
            "friend_count": len(friends),
            "claimed_friend_ids": tuple(friends),
            # 组数不是件数（见 models/reward.py::count_items 的注释）
            "item_group_count": len(res.itemList),
            "sent_packets": tuple(self.sent_packet_ids),
        }

        if res.errorCode != 0:
            # 与 top_pvp_box 同口径：非 0 通常表示"今天没有可领的"，
            # 不是故障 —— 打红叉会让用户以为工具坏了。
            self.logger.info("好友赠礼一键领取：服务端返回码 %d", res.errorCode)
            return TaskResult(
                task=self.name,
                ok=True,
                # ★ 2026-10-18：错误码只进 logger / data，用户文案不带 code=…
                message="好友赠礼：服务端未返回可领取的赠礼（按无待领处理）",
                data=data,
            )

        if not friends:
            return TaskResult(
                task=self.name,
                ok=True,
                message="好友赠礼：当前没有可领取的赠礼",
                data=data,
            )

        # 道具口径未实证 → UNKNOWN 只会产出「获得 N 件道具」，不会报数字
        items = reward_phrase(res.itemList, scope=QuantityScope.UNKNOWN)
        suffix = f" · {items}" if items else ""
        return TaskResult(
            task=self.name,
            ok=True,
            message=f"好友赠礼一键领取成功（{len(friends)} 位好友{suffix}）",
            data=data,
        )

    def _dry_run(self, game: GameClient) -> TaskResult:
        """演练：打印将要发出的字节与安全边界，**一个包都不发**。"""
        packet = FriendOperationPacket(
            actionType=OPERATION_GAIN_ALL_GIFT, playerID=ALL_GIFT_TARGET
        )
        print("【好友赠礼一键领取 · 演练】未发送任何请求")
        print()
        print(f"计划发送：{PACKET_PURPOSE[10009]}")
        print(game.build(packet, include_defaults=True).describe())
        print()
        print("【安全边界】")
        print(f"  · 本任务允许发出的消息号：{sorted(CLAIM_ALLOWED_PACKET_IDS.values())}")
        print("  · 只做领取（actionType=3）；一键全送（actionType=4）归每日任务自动执行")
        print("  · 零消耗：连好友列表 10001 都不在名单里")
        print()
        print("—— 以上为演练输出，未发送任何请求 ——")
        return TaskResult(
            task=self.name,
            ok=True,
            message="演练完成：计划发出 10009 一键领取（未实际发送）",
            data={"planned_packets": (10009,), "sent_packets": ()},
        )
