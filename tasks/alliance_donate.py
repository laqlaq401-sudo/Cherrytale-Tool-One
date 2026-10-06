"""工会金币捐献（消息号 21009 读状态 → 21017 固定捐 5 次金币档）。

【行为契约（用户决策）】
每次执行**固定捐 5 次**金币档（初級捐獻：每次 5000 金币 → 10 贡献，
共 25000 金币）。没有参数、没有档位选择 —— 简单、可预期。

【为什么"钻石捐献通道"在这份代码里不存在】
``DonatePacket.actionType`` 决定服务端扣哪种货币（金币档 = 693000001，
中級/高級 = 693000002/693000003 扣钻石）。模型层
:meth:`models.alliance.DonatePacket.for_gold_tier` 是唯一构造途径，
``actionType`` 恒为金币档 —— 中/高級档**没有可构造路径**，
测试（``tests/test_tasks_alliance_donate.py``）会解码实际发出的字节钉死这一点。

【被服务端拒绝时：停、不重试、如实报】
金币不足（errorCode=7）、今日次数用完（errorCode=10）等都会让当次捐献
失败：任务**立即停止**（后续次数注定同样失败，多发无益），把"已捐 X/5"
与拒绝原因写进结果。超时**绝不重试** —— 服务端可能已经扣了金币。

【连捐之间有拟人化间隔（2026-10-04 安全审计 P2）】
5 次捐献原先在同一毫秒里连发，节律极其规整；现在按
``config.polite_interval()``（默认随机 1~2 秒，与扫荡共用同一组上下限）
隔开，并用 ``cancellation.sleep_interruptible`` 等待 ——
用户点「停止执行」时立刻生效，不必等满这一轮。

【"没入会"不算失败】
21009 回 ``errorCode=19`` → ``ok=True`` 并说明，与签到/挖矿同一哲学。
"""

from __future__ import annotations

from typing import ClassVar, Final

import config
from client.game_client import GameClient
from client.spending import SpendPolicy
from models.alliance import (
    DONATE_ALLOWED_PACKET_IDS,
    FORBIDDEN_PACKET_IDS,
    AllianceHomePacket,
    AllianceHomeRes,
    DonatePacket,
    DonateRes,
    ERR_NOT_IN_ALLIANCE,
    is_not_in_alliance,
    GOLD_TIER_COST,
    GOLD_TIER_DONATE_ID,
    error_code_name,
)
from models.const_ids import CONSTS
from services import cancellation
from tasks.base import BaseTask, TaskResult, register_task
from tasks.packet_guard import guarded_send

#: 每次执行固定捐献的次数（用户决策：固定 5 次，不做参数）
DONATE_TIMES: Final[int] = 5


@register_task
class AllianceDonateTask(BaseTask):
    """工会金币捐献：向工会捐 5 次金币档（每次 5000 金币 → 10 贡献）。"""

    name = "alliance_donate"

    requires_auth = True

    #: 消耗的是**金币**（普通道具），不是钻石 —— 钻石捐献档没有任何可构造路径
    spends_diamond = False

    opt_in_hint: ClassVar[str] = (
        f"每次执行固定捐 {DONATE_TIMES} 次金币档（共 {DONATE_TIMES * GOLD_TIER_COST} 金币）；"
        "钻石档（中級/高級）不实现"
    )

    def __init__(
        self,
        session,
        *,
        logger=None,
        dry_run: bool = False,
        spend_policy: SpendPolicy | None = None,
        run_options=None,
    ) -> None:
        super().__init__(
            session,
            logger=logger,
            dry_run=dry_run,
            spend_policy=spend_policy,
            run_options=run_options,
        )
        self.sent_packet_ids: list[int] = []

    # ------------------------------------------------------------------
    # ① 读工会主页（是否入会 + 今日已捐次数）
    # ------------------------------------------------------------------
    def _fetch_home(self, game: GameClient) -> AllianceHomeRes:
        """拉取工会主页（21009 → 21010，只读）。"""
        view = guarded_send(
            game,
            AllianceHomePacket(),
            AllianceHomeRes,
            allowed=DONATE_ALLOWED_PACKET_IDS,
            forbidden=FORBIDDEN_PACKET_IDS,
            record=self.sent_packet_ids,
            logger=self.logger,
        )
        return view.decode_sub_packet(AllianceHomeRes)

    # ------------------------------------------------------------------
    # ③ 捐一次（金币档）
    # ------------------------------------------------------------------
    def _donate_once(self, game: GameClient) -> tuple[bool, str, DonateRes]:
        """捐一次金币档（21017）。

        :return: ``(是否成功, 可读结果, 原始响应)``。

        .. warning::
           **不重试**。捐献会真的扣金币，超时后服务端可能已经扣了，
           重试等于多捐一次。
        """
        view = guarded_send(
            game,
            DonatePacket.for_gold_tier(),
            DonateRes,
            allowed=DONATE_ALLOWED_PACKET_IDS,
            forbidden=FORBIDDEN_PACKET_IDS,
            record=self.sent_packet_ids,
            logger=self.logger,
        )
        res = view.decode_sub_packet(DonateRes)
        if not res.is_success():
            return False, f"errorCode={res.errorCode}（{error_code_name(res.errorCode)}）", res
        return True, f"扣 {GOLD_TIER_COST} 金币 → 今日捐献值 {res.dailyDonateAmount}", res

    # ------------------------------------------------------------------
    # ④ 主流程
    # ------------------------------------------------------------------
    def execute(self) -> TaskResult:
        """读主页 → 固定捐 5 次（被拒即停）→ 汇总对账。

        :raises client.exceptions.CherrytaleError: 网络失败时（由基类转成失败结果）。
        """
        with self.open_game_client() as game:
            if self.dry_run:
                return self._dry_run(game)

            home = self._fetch_home(game)
            if not home.is_success():
                if is_not_in_alliance(home.errorCode):
                    message = "未加入工会，无法捐献"
                    self.logger.info("%s", message)
                    return TaskResult(
                        task=self.name, ok=True, message=message, data={"skipped": True}
                    )
                message = (
                    f"拉取工会主页失败：errorCode={home.errorCode}"
                    f"（{error_code_name(home.errorCode)}）"
                )
                self.logger.warning("%s", message)
                return TaskResult(task=self.name, ok=False, message=message)

            gold_item_id = int(CONSTS["COINS_ID"])
            donated = 0
            last_error = ""
            for attempt in range(1, DONATE_TIMES + 1):
                if attempt > 1:
                    # 【拟人化间隔（2026-10-04 安全审计 P2）】原先这 5 次捐献是在
                    # 同一毫秒里连发出去的（节律极其规整，一眼像脚本）。
                    # 现在按 ``config.polite_interval()``（默认随机 1~2 秒）隔开 ——
                    # 与扫荡共用同一组上下限配置。
                    # 必须用**可中断**的等待：用户点「停止执行」时立刻生效，
                    # 而不是还要等满这一轮（`time.sleep` 做不到这点）。
                    cancellation.sleep_interruptible(config.polite_interval())
                ok, text, res = self._donate_once(game)
                if not ok:
                    last_error = text
                    self.logger.warning("✘ 第 %d/%d 次捐献被拒：%s", attempt, DONATE_TIMES, text)
                    break
                donated += 1
                # ★ 2026-10-04 口径修正：21017 的 ``costList`` 数量语义**尚未实证**
                # （可能同 6006/11010/37020 一样是"操作后持有量"）—— 若照读，
                # 5 次捐献会把 5 个**金币余额**累加起来，得出一个荒谬的总额。
                # 所以消耗量改由**本地配置表的金币档单价 × 成功次数**推算，
                # 响应里的数字只留在 DEBUG 日志里供对账。
                self.logger.debug(
                    "第 %d/%d 次捐献响应 costList 里的金币值（口径未实证）：%s",
                    attempt,
                    DONATE_TIMES,
                    res.gold_spent(gold_item_id),
                )
                self.logger.info(
                    "✔ 第 %d/%d 次捐献成功：%s", attempt, DONATE_TIMES, text
                )

        # 消耗量一律按本地单价推算（见循环内注释）：这是**完全可核对**的数字，
        # 而响应里的 costList 口径未实证，不能当消耗量用。
        gold_spent = GOLD_TIER_COST * donated

        if donated == DONATE_TIMES:
            message = (
                f"工会捐献完成：{donated}/{DONATE_TIMES} 次，共扣 {gold_spent} 金币"
                f"（今日捐献值 {home.myAlliance.dailyDonateAmount if home.myAlliance else '？'}）"
            )
            ok = True
        elif donated == 0:
            ok = False
            message = f"工会捐献失败：一次都没捐出去；{last_error}"
        else:
            ok = False
            message = (
                f"工会捐献部分完成：已捐 {donated}/{DONATE_TIMES} 次、共扣 {gold_spent} 金币，"
                f"第 {donated + 1} 次被拒后停止；{last_error}"
            )
        if ok:
            self.logger.info("%s", message)
        else:
            self.logger.warning("%s", message)
        return TaskResult(
            task=self.name,
            ok=ok,
            message=message,
            data={
                "donated": donated,
                "planned": DONATE_TIMES,
                "gold_spent": gold_spent,
                "gold_tier_id": GOLD_TIER_DONATE_ID,
                "last_error": last_error,
                "donate_count_before": home.donateCount,
            },
        )

    def _dry_run(self, game: GameClient) -> TaskResult:
        """演练：打印"拉主页"与"单次金币捐献"两包的样子，不发送。"""
        home_request = game.build(AllianceHomePacket())
        print(home_request.describe())
        print()
        donate_request = game.build(DonatePacket.for_gold_tier())
        print(donate_request.describe())
        print()
        print(
            f"意图：固定捐 {DONATE_TIMES} 次金币档，预计共扣 {DONATE_TIMES * GOLD_TIER_COST} 金币"
            "（实际以服务端响应 costList 对账为准；被拒即停）。"
        )
        print("—— 以上为演练输出，未发送任何请求 ——")
        return TaskResult(
            task=self.name,
            ok=True,
            message=(
                f"演练完成：拟捐 {DONATE_TIMES} 次（{DONATE_TIMES * GOLD_TIER_COST} 金币），"
                f"21017 请求体 {len(donate_request.body)} 字节，未发送"
            ),
            data={"body_size": len(donate_request.body)},
        )
