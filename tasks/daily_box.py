"""日常 / 周常活跃宝箱与任务奖励领取（消息号 6001 拉状态 → 6005 领奖）。

【协议与调用机制】
游戏客户端源码反汇编（``DailyMission_UIView.OnClickEvent_DailyRewardBox`` 及
``OnClick_ReceiveMissionReward``）确认：
服务端对日常/周常任务与活跃度宝箱采用「一键结算」设计：
- 一键领取日常任务：``DMReward(misID=-1, mistypeID=0)``
- 一键领取周常任务：``DMReward(misID=-1, mistypeID=14)``
- 一键领取日常活跃宝箱：``DMReward(misID=-1, mistypeID=6)``
- 一键领取周常活跃宝箱：``DMReward(misID=-1, mistypeID=15)``

⚠️ 注意：传入配置表的具体宝箱 ID（如 ``dmrID=939000001``）会被服务端拒绝并返回 ``errorCode=-2``。
只有传入 ``misID=-1``（客户端常量 ``RPS_Const.NullDataOnSetting``）时，
服务端才会结算当前全部达标未领取的项目：
- 有可领取的项目时：服务端回传 ``errorCode=0``，附带奖励列表 ``rewardList``；
- 无可领取的项目时：服务端回传 ``errorCode=-1``，``rewardList`` 为空（属正常无待领状态，非错误）。

【完整领取流程】
1. 消息号 6001 拉取当前任务状态，计算当前日常/周常活跃点与达标情况；
2. 先一键领取已完成的日常任务与周常任务（若有奖励入账，刷新活跃点）；
3. 一键领取日常活跃宝箱与周常活跃宝箱；
4. 汇总所有领取的道具明细，向用户汇报。
"""

from __future__ import annotations

from client.game_client import GameClient
from models.daily import (
    DailyMission,
    DailyMissionRes,
    DMReward,
    DMRewardRes,
    MissionType,
)
from models.game_config import (
    boxes_reachable,
    load_daily_missions,
    load_daily_reward_boxes,
    load_weekly_missions,
    load_weekly_reward_boxes,
    total_active_point,
)
from tasks.base import BaseTask, QuantityScope, TaskResult, register_task, reward_phrase


@register_task
class DailyBoxTask(BaseTask):
    """领取日常 / 周常活跃度宝箱（活跃点够了才会成功）。"""

    name = "daily_box"

    # ------------------------------------------------------------------
    # ① 拉状态
    # ------------------------------------------------------------------
    def _fetch_state(self, game: GameClient) -> DailyMissionRes:
        """拉取任务状态（消息号 6001 → 6002）。

        ``type`` 传空列表表示"全都要" —— 服务端会把它当默认处理。
        """
        view = game.send(
            DailyMission(type=[]),
            expect=DailyMissionRes,
            include_defaults=True,
        )
        return view.decode_sub_packet(DailyMissionRes)

    # ------------------------------------------------------------------
    # ② 算活跃点 + 求差集
    # ------------------------------------------------------------------
    def _plan_boxes(
        self, state: DailyMissionRes
    ) -> tuple[list[int], list[int], int, int]:
        """算出"这次要尝试领取哪些宝箱"。

        :return: ``(日常待领 ID 列表, 周常待领 ID 列表, 日常活跃点, 周常活跃点)``。
        """
        daily_point = total_active_point(
            load_daily_missions(),
            {item.misID: item.completedTimes for item in state.dmInitList},
        )
        weekly_point = total_active_point(
            load_weekly_missions(),
            {item.misID: item.completedTimes for item in state.wmInitList},
        )

        received_daily = set(state.dmrReceivedList)
        received_weekly = set(state.wmrReceivedList)

        daily_pending = [
            box_id
            for box_id in boxes_reachable(load_daily_reward_boxes(), daily_point)
            if box_id not in received_daily
        ]
        weekly_pending = [
            box_id
            for box_id in boxes_reachable(load_weekly_reward_boxes(), weekly_point)
            if box_id not in received_weekly
        ]
        return daily_pending, weekly_pending, daily_point, weekly_point

    # ------------------------------------------------------------------
    # ③ 一键领取
    # ------------------------------------------------------------------
    def _claim(
        self,
        game: GameClient,
        mission_type_or_box_id: MissionType | int,
        mission_type: MissionType | None = None,
    ) -> tuple[bool, str]:
        """一键领取指定类型的奖励（misID 固定为 -1）。

        兼容两种调用方式：
        - ``_claim(game, mission_type)``
        - ``_claim(game, box_id, mission_type)``（旧签名兼容，忽略 box_id）

        :return: ``(是否业务正常, 可读结果/错误描述)``。
        """
        if mission_type is not None:
            target_type = mission_type
        elif isinstance(mission_type_or_box_id, MissionType):
            target_type = mission_type_or_box_id
        else:
            target_type = MissionType(mission_type_or_box_id)

        packet = DMReward.for_mission(-1, target_type)
        view = game.send(packet, expect=DMRewardRes, include_defaults=True)
        res = view.decode_sub_packet(DMRewardRes)
        if res.errorCode == 0:
            return True, self._balance_text(res.rewardList)
        if res.errorCode == -1:
            return True, ""  # 无待领奖励，属正常情况
        return False, f"errorCode={res.errorCode}（服务端未回成功码）"

    @staticmethod
    def _balance_text(reward_list: list) -> str:
        """把响应里的道具汇总成"**当前持有**"口径的一行。

        【★ 2026-10-03 措辞修正（Q-021 的同类结论）】
        ``6006 DMRewardRes.rewardList`` 里的数量是服务端回传的**领取后钱包
        余额**，不是本次入账增量 —— 以前按"获得"展示，用户会看到
        "金币×3661 万"这种账户级数字，误以为宝箱发了这么多。
        真实增量需要"领取前后各发一次 2007 钱包查询求差"，多发只读包，
        本任务不做；这里只把口径说清楚。

        【★ 2026-10-04】口径从"写在 docstring 里的约定"变成**类型**：
        ``scope=QuantityScope.HELD`` 会让 :func:`models.reward.reward_phrase`
        自动选用"当前持有"这个词，想再写错都难。
        """
        phrase = reward_phrase(reward_list, scope=QuantityScope.HELD)
        return f"奖励已入账（{phrase}）" if phrase else ""

    # ------------------------------------------------------------------
    # ④ 主流程
    # ------------------------------------------------------------------
    def execute(self) -> TaskResult:
        """拉状态 → 算活跃点 → 逐项一键领取，最后汇总成功/失败。

        :raises client.exceptions.CherrytaleError: 网络失败时（由基类转成失败结果）。
        """
        with self.open_game_client() as game:
            if self.dry_run:
                return self._dry_run(game)

            state = self._fetch_state(game)
            daily_pending, weekly_pending, daily_point, weekly_point = self._plan_boxes(state)
            self.logger.info(
                "当前活跃度：日常 %d 点（待领宝箱 %s）；周常 %d 点（待领宝箱 %s）",
                daily_point,
                daily_pending or "无",
                weekly_point,
                weekly_pending or "无",
            )

            claimed: list[str] = []
            failed: list[str] = []

            # 步骤 1：先一键领取已完成的日常任务和周常任务
            mission_claims = (
                (MissionType.DAILY, "日常任务"),
                (MissionType.WEEKLY, "周常任务"),
            )
            mission_rewarded = False
            for mtype, label in mission_claims:
                ok, text = self._claim(game, mtype)
                line = f"{label}：{text}"
                if ok:
                    if text:
                        claimed.append(line)
                        mission_rewarded = True
                        self.logger.info("✔ 已领取 %s", line)
                else:
                    failed.append(line)
                    self.logger.warning("✘ 领取失败 %s", line)

            # 若领到了日常/周常任务，重新拉取状态以刷新最新活跃点与待领宝箱清单
            if mission_rewarded:
                state = self._fetch_state(game)
                daily_pending, weekly_pending, daily_point, weekly_point = self._plan_boxes(state)
                self.logger.info(
                    "任务领奖后最新活跃度：日常 %d 点（待领宝箱 %s）；周常 %d 点（待领宝箱 %s）",
                    daily_point,
                    daily_pending or "无",
                    weekly_point,
                    weekly_pending or "无",
                )

            # 步骤 2：一键领取日常活跃宝箱与周常活跃宝箱
            box_claims = (
                (MissionType.DAILY_REWARD_BOX, "日常宝箱"),
                (MissionType.WEEKLY_REWARD_BOX, "周常宝箱"),
            )
            for mtype, label in box_claims:
                ok, text = self._claim(game, mtype)
                line = f"{label}：{text}"
                if ok:
                    if text:
                        claimed.append(line)
                        self.logger.info("✔ 已领取 %s", line)
                else:
                    failed.append(line)
                    self.logger.warning("✘ 领取失败 %s", line)

        if not claimed and not failed:
            message = f"没有可领的宝箱或任务（日常 {daily_point} 点 / 周常 {weekly_point} 点，均已领完或未达标）"
        elif failed:
            message = f"领到 {len(claimed)} 项、失败 {len(failed)} 项：" + "；".join(failed)
        else:
            message = f"领取成功 {len(claimed)} 项：" + "；".join(claimed)

        return TaskResult(
            task=self.name,
            ok=not failed,
            message=message,
            data={
                "daily_point": daily_point,
                "weekly_point": weekly_point,
                "claimed": tuple(claimed),
                "failed": tuple(failed),
            },
        )

    def _dry_run(self, game: GameClient) -> TaskResult:
        """演练：只组装"拉状态"这一包，并列出本地宝箱清单。"""
        request = game.build(DailyMission(type=[]), include_defaults=True)
        print(request.describe())
        print()
        print("本地宝箱清单（真实待领清单必须等 6002 响应才能确定）：")
        for box in load_daily_reward_boxes():
            print(f"  · 日常宝箱 {box.box_id}：需活跃点 {box.required_point}")
        for box in load_weekly_reward_boxes():
            print(f"  · 周常宝箱 {box.box_id}：需活跃点 {box.required_point}")
        print("—— 以上为演练输出，未发送任何请求 ——")
        return TaskResult(
            task=self.name,
            ok=True,
            message=f"演练完成：6001 请求体 {len(request.body)} 字节，未发送",
            data={"body_size": len(request.body)},
        )
