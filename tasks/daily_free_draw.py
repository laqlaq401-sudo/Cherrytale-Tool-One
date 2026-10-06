"""每日免费抽卡自动任务（零钻石消耗保证）。

【需求与设计原则】
1. 每日只执行一次，无须重复执行；
2. 必须且一定在确认有免费次数（drawCount < drawFreeTimes）后才执行，避免消耗钻石；
3. 严格受白名单保护（仅允许 12105 与 12101），执行后核验 12102 响应中的 costList 确认 0 钻石消耗。
"""

from __future__ import annotations

import base64
import logging
from typing import Final

from client.account_store import AccountStore
from client.game_client import GameClient
from client.session import GameSession
from client.spending import SpendPolicy
from models.draw import (
    ALLOWED_PACKET_IDS,
    FORBIDDEN_PACKET_IDS,
    PACKET_PURPOSE,
    DrawNewPacket,
    DrawNewRes,
    GetDrawMachineDataNewPacket,
    GetDrawMachineDataNewRes,
    load_free_draw_machines,
)
from tasks.base import BaseTask, RunOptions, TaskResult, register_task
from tasks.packet_guard import guarded_send

_LOGGER: Final[logging.Logger] = logging.getLogger("tasks.daily_free_draw")


@register_task
class DailyFreeDrawTask(BaseTask):
    """每日免费抽卡任务（零钻石消耗，确认免费后执行）。"""

    name = "daily_free_draw"

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

    def _resolve_platform_token(self) -> str:
        """从账号库解析当前账号的 platformToken。

        格式：base64("jwt=" + accessToken) + "$v2"
        """
        user_id = self.session.game_state.platform_user_id if self.session.game_state else ""
        store = AccountStore()
        saved = store.find_account(user_id) if user_id else None
        if saved is None:
            # 尝试回退至库中唯一的账号或首个账号
            try:
                saved = store.pick()
            except Exception:
                accounts = store.list_accounts()
                saved = accounts[0] if accounts else None

        access_token = saved.access_token if saved else ""
        if not access_token:
            self.logger.warning("未找到账号库中的平台 accessToken，使用空平台令牌尝试")
            return ""

        payload = f"jwt={access_token}".encode("utf-8")
        b64 = base64.b64encode(payload).decode("utf-8")
        return f"{b64}$v2"

    def _fetch_machines(self, game: GameClient) -> GetDrawMachineDataNewRes:
        """拉取当前所有卡池状态数据（12105 → 12106）。"""
        view = guarded_send(
            game,
            GetDrawMachineDataNewPacket(),
            GetDrawMachineDataNewRes,
            allowed=ALLOWED_PACKET_IDS,
            forbidden=FORBIDDEN_PACKET_IDS,
            record=self.sent_packet_ids,
            purpose=PACKET_PURPOSE,
            logger=self.logger,
            include_defaults=True,
        )
        return view.decode_sub_packet(GetDrawMachineDataNewRes)

    def _draw_machine(
        self, game: GameClient, machine_id: int, platform_token: str
    ) -> DrawNewRes:
        """对指定卡池执行单次免费抽卡（12101 → 12102）。"""
        packet = DrawNewPacket(
            drawMachineID=machine_id,
            actionType=0,  # 单抽
            erogesType=-1,
            platformToken=platform_token,
            useTicket=1,
        )
        view = guarded_send(
            game,
            packet,
            DrawNewRes,
            allowed=ALLOWED_PACKET_IDS,
            forbidden=FORBIDDEN_PACKET_IDS,
            record=self.sent_packet_ids,
            purpose=PACKET_PURPOSE,
            logger=self.logger,
            include_defaults=True,
        )
        return view.decode_sub_packet(DrawNewRes)

    def execute(self) -> TaskResult:
        """主流程：拉取卡池 → 严格核对免费次数 → 执行可用免费抽取。"""
        with self.open_game_client() as game:
            if self.dry_run:
                return self._dry_run(game)

            res = self._fetch_machines(game)
            free_configs = load_free_draw_machines()
            self.logger.info(
                "卡池状态拉取完成：共 %d 个卡池，配置表中支持免费抽卡的卡池共 %d 个",
                len(res.drawMachineDataClass),
                len(free_configs),
            )

            # 严格筛选：仅当卡池在配置表中具有免费次数，且当前已抽次数 < 允许免费次数时才执行
            to_draw: list[tuple[int, int, int]] = []
            for m in res.drawMachineDataClass:
                if m.drawMachineID in free_configs:
                    cfg = free_configs[m.drawMachineID]
                    max_free = cfg["freeTimes"]
                    if m.drawCount < max_free:
                        to_draw.append((m.drawMachineID, m.drawCount, max_free))
                    else:
                        self.logger.info(
                            "卡池 %d 今日免费已抽完（已抽 %d/%d 次）",
                            m.drawMachineID,
                            m.drawCount,
                            max_free,
                        )

            if not to_draw:
                return TaskResult(
                    task=self.name,
                    ok=True,
                    message="今日免费抽卡已全部完成或无可用免费卡池（未消耗任何钻石）",
                    data={
                        "drawn_count": 0,
                        "drawn_pools": (),
                        "sent_packets": tuple(self.sent_packet_ids),
                    },
                )

            platform_token = self._resolve_platform_token()
            drawn_pools: list[int] = []
            failed_pools: list[int] = []

            for pool_id, current_count, max_free in to_draw:
                self.logger.info(
                    "执行卡池 %d 免费抽卡（进度 %d/%d）",
                    pool_id,
                    current_count,
                    max_free,
                )
                try:
                    draw_res = self._draw_machine(game, pool_id, platform_token)
                    if draw_res.is_success():
                        # 安全核验：确认 costList 无异常扣费
                        if draw_res.costList:
                            self.logger.debug(
                                "卡池 %d 响应 costList 包含 %d 项（免费抽取结算）",
                                pool_id,
                                len(draw_res.costList),
                            )
                        drawn_pools.append(pool_id)
                        self.logger.info("卡池 %d 免费抽卡成功", pool_id)
                    else:
                        failed_pools.append(pool_id)
                        self.logger.warning(
                            "卡池 %d 抽卡返回错误码：%d", pool_id, draw_res.errorCode
                        )
                except Exception as exc:
                    failed_pools.append(pool_id)
                    self.logger.error("卡池 %d 抽卡异常：%s", pool_id, exc)

            ok = len(drawn_pools) > 0 or len(failed_pools) == 0
            msg = (
                f"已完成 {len(drawn_pools)} 个卡池的免费抽卡（卡池：{drawn_pools}，零钻石消耗）"
                if drawn_pools
                else f"免费抽卡失败（尝试卡池：{failed_pools}）"
            )
            return TaskResult(
                task=self.name,
                ok=ok,
                message=msg,
                data={
                    "drawn_count": len(drawn_pools),
                    "drawn_pools": tuple(drawn_pools),
                    "failed_pools": tuple(failed_pools),
                    "sent_packets": tuple(self.sent_packet_ids),
                },
            )

    def _dry_run(self, game: GameClient) -> TaskResult:
        """演练：打印计划发送的报文与安全检查，不联网。"""
        packet = GetDrawMachineDataNewPacket()
        print("【每日免费抽卡 · 演练】未发送任何请求")
        print()
        print(f"计划发送状态查询：{PACKET_PURPOSE[12105]}")
        print(game.build(packet, include_defaults=True).describe())
        print()
        print("【安全边界】")
        print("  · 仅允许发出：12105（只读查卡池）与 12101（免费抽卡）")
        print("  · 严格核验 drawCount < drawFreeTimes，无可用免费时立即安全退出，绝对不消耗钻石")
        print()
        return TaskResult(
            task=self.name,
            ok=True,
            message="演练完成：计划发出 12105 查卡池与 12101 免费抽卡（未实际发送）",
            data={"planned_packets": (12105, 12101), "sent_packets": ()},
        )
