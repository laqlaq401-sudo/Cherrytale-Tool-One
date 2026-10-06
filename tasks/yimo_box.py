"""失控炼成阵全服通关宝箱自动领取任务。

【需求与设计原则】
1. 炼成阵宝箱解锁条件依赖全服务器通关次数，无法预先确定何时解锁；
2. 每次登录时执行一次；登录期间每隔一个小时自动执行一次；
3. 直到 3 份宝箱全部领满（rewardState 全部为 2）后，停止重复领取；
4. 严格受白名单保护（仅发送 22101 查状态与 22105 领宝箱，绝不碰 22103 购买次数或 22107 进战）。
"""

from __future__ import annotations

import logging
from typing import Any, Final

from client.game_client import GameClient
from client.session import GameSession
from client.spending import SpendPolicy
from models.yimo import (
    ALLOWED_PACKET_IDS,
    FORBIDDEN_PACKET_IDS,
    PACKET_PURPOSE,
    GetYimoState,
    GetYimoStateRes,
    OpenYimoRewardPacket,
    OpenYimoRewardRes,
)
from tasks.base import BaseTask, RunOptions, TaskResult, register_task
from tasks.packet_guard import guarded_send

_LOGGER: Final[logging.Logger] = logging.getLogger("tasks.yimo_box")


@register_task
class YimoBoxTask(BaseTask):
    """失控炼成阵全服通关宝箱领取任务（零消耗，3 份全满后停止）。"""

    name = "yimo_box"

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
        #: 22106 的原始响应视图（仅排障用；失败时日志里已记了 ``describe()``）
        self.last_reward_view: Any = None

    def _fetch_state(self, game: GameClient) -> GetYimoStateRes:
        """拉取失控炼成阵状态与全服宝箱信息（22101 → 22102）。"""
        view = guarded_send(
            game,
            GetYimoState(),
            GetYimoStateRes,
            allowed=ALLOWED_PACKET_IDS,
            forbidden=FORBIDDEN_PACKET_IDS,
            record=self.sent_packet_ids,
            purpose=PACKET_PURPOSE,
            logger=self.logger,
            include_defaults=True,
        )
        return view.decode_sub_packet(GetYimoStateRes)

    def _open_reward(self, game: GameClient) -> OpenYimoRewardRes:
        """发送开箱领奖请求（22105 → 22106）。

        ★ 2026-10-06：把原始响应的**一行摘要**留在日志里。

        【为什么值得记】
        ``OpenYimoRewardRes`` 按 dump.cs 只有 ``errorCode`` + ``getList``
        两个字段（已核对 dump.cs:410725），**没有**可读的错误描述 ——
        所以失败时只能报出裸错误码 ``-5``，无从判断是"今天已领过"
        "宝箱未达标" 还是"活动已结束"。
        真实完整响应里往往另有线索（多余子包 / 未知字段 / 元信息），
        记一行 ``describe()`` 就能让下次复现时**有据可查**，
        不必再靠加字段或猜。
        """
        view = guarded_send(
            game,
            OpenYimoRewardPacket(),
            OpenYimoRewardRes,
            allowed=ALLOWED_PACKET_IDS,
            forbidden=FORBIDDEN_PACKET_IDS,
            record=self.sent_packet_ids,
            purpose=PACKET_PURPOSE,
            logger=self.logger,
            include_defaults=True,
        )
        self.last_reward_view = view
        # 诊断日志**绝不能**因为视图没有 describe()（测试替身 / 未来换实现）
        # 就把任务本身搞崩 —— 它只是"多留一份现场"，不是主逻辑。
        describe = getattr(view, "describe", None)
        if callable(describe):
            try:
                self.logger.info("22106 原始响应：%s", describe())
            except Exception:  # noqa: BLE001 - 排障信息不该影响业务
                pass
        return view.decode_sub_packet(OpenYimoRewardRes)

    def execute(self) -> TaskResult:
        """主流程：查全服宝箱状态 → 有可领则领取 → 汇报是否 3 份全部领满。"""
        with self.open_game_client() as game:
            if self.dry_run:
                return self._dry_run(game)

            state = self._fetch_state(game)
            claimed = state.claimed_chest_count()
            claimable_ids = state.claimable_chest_ids()
            total_chests = max(len(state.rewardData), 3)

            self.logger.info("失控炼成阵全服宝箱状态：%s", state.chest_summary())

            # 分支 1：已经全部领满 3 份
            if state.all_chests_claimed():
                return TaskResult(
                    task=self.name,
                    ok=True,
                    message="失控炼成阵全服宝箱已全部领满（3/3 已领），今日无需再领",
                    data={
                        "all_claimed": True,
                        "claimed_count": claimed,
                        "total_count": total_chests,
                        "claimable_count": 0,
                        "sent_packets": tuple(self.sent_packet_ids),
                    },
                )

            # 分支 2：存在已达标未领取的宝箱
            if claimable_ids:
                self.logger.info("发现 %d 个可领取的全服宝箱（ID: %s），发起领取", len(claimable_ids), claimable_ids)
                res = self._open_reward(game)
                if not res.is_success():
                    return TaskResult(
                        task=self.name,
                        ok=False,
                        message=f"领取失控炼成阵宝箱失败，服务端返回错误码：{res.errorCode}",
                        data={
                            "all_claimed": False,
                            "claimed_count": claimed,
                            "total_count": total_chests,
                            "sent_packets": tuple(self.sent_packet_ids),
                        },
                    )

                new_claimed = claimed + len(claimable_ids)
                all_claimed = new_claimed >= total_chests
                msg = (
                    f"成功领取失控炼成阵全服宝箱（本次领取 {len(claimable_ids)} 个，当前共已领 {new_claimed}/{total_chests} 个"
                    + ("，已全部领满！" if all_claimed else "，剩余宝箱将在后续轮询中继续检测）")
                )
                return TaskResult(
                    task=self.name,
                    ok=True,
                    message=msg,
                    data={
                        "all_claimed": all_claimed,
                        "claimed_count": new_claimed,
                        "total_count": total_chests,
                        "claimable_count": 0,
                        # ★ 2026-10-04：`getList` 是 ``list[bytes]``（未解析的
                        # GetObjClass），``len()`` 是**组数**不是件数 —— 名字必须如实。
                        "reward_group_count": len(res.getList),
                        "sent_packets": tuple(self.sent_packet_ids),
                    },
                )

            # 分支 3：暂无可领宝箱，全服次数尚未达标
            return TaskResult(
                task=self.name,
                ok=True,
                message=f"失控炼成阵全服宝箱当前进度：已领 {claimed}/{total_chests} 个，暂无达标待领宝箱（将在登录期间每小时继续检测）",
                data={
                    "all_claimed": False,
                    "claimed_count": claimed,
                    "total_count": total_chests,
                    "claimable_count": 0,
                    "sent_packets": tuple(self.sent_packet_ids),
                },
            )

    def _dry_run(self, game: GameClient) -> TaskResult:
        """演练：打印计划发送的报文与安全检查，不联网。"""
        packet = GetYimoState()
        print("【失控炼成阵全服宝箱 · 演练】未发送任何请求")
        print()
        print(f"计划发送状态查询：{PACKET_PURPOSE[22101]}")
        print(game.build(packet, include_defaults=True).describe())
        print()
        print("【安全边界】")
        print(f"  · 允许发出的消息号：{sorted(ALLOWED_PACKET_IDS.values())}")
        print(f"  · 明确禁发（含花钻石买次数的 22103）：{sorted(FORBIDDEN_PACKET_IDS.values())}")
        print("  · 3 份宝箱全满时自动停领，未满时每隔一小时自动检测")
        print()
        return TaskResult(
            task=self.name,
            ok=True,
            message="演练完成：计划发出 22101 查宝箱状态与 22105 领宝箱（未实际发送）",
            data={"planned_packets": (22101, 22105), "sent_packets": ()},
        )
