"""失控炼成阵（Yimo）战斗自动化任务。

【业务说明】
对应 SystemID 51「失控煉成陣」，协议模块 YimoBattleModule（22101~22110）。
每日提供免费挑战次数，向暴走锡樵夫发起进攻并提交伤害得分。

【流程】
1. 22101 GetYimoState：读取剩余挑战次数 remainCount、当前排名 nowRank 与当前伤害总分；
   - 若 remainCount <= 0，安全退出，不发任何无效请求；
   - 若 dry_run=True，提示演练并安全退出；
2. 22107 YimoBattleRequest：进入炼成阵战斗，下发 fightToken；
3. 本地防风控等待（默认 18~28 秒随机耗时，单测可传 0）；
4. 22109 YimoBattleResult：按 MD5(fightToken + score + "03120908") 计算 Token，提交战绩；
5. 22110 YimoBattleResultRes：接收积分结算与道具奖励。

【红线守则】
- 严禁触碰天命系统（Destiny）；
- 严禁触碰高难属性试炼；
- 严禁花钻石购买次数（22103 BuyYimoCount 严格列入禁发清单）。
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Final

import config
from client.exceptions import ProtocolError
from client.game_client import GameClient
from client.session import GameSession
from client.spending import SpendPolicy
from services import cancellation
from models.battle_yimo import (
    YimoBattleRequest,
    YimoBattleRequestRes,
    YimoBattleResult,
    YimoBattleResultRes,
)
from models.main_stage import BattleResultClass
from models.packet_ids import packet_id
from models.assemble import decode_lineup
from models.yimo import GetYimoState, GetYimoStateRes, load_part_base_hp
from tasks.base import BaseTask, RunOptions, TaskResult, register_task
from tasks.battle_record_builder import (
    YIMO_MINE,
    build_lineup_records,
    estimate_lineup_power,
    lineup_from_roles,
)
from tasks.packet_guard import guarded_send
from crypto.battle_signer import (
    BATTLE_TYPE_YIMO,
    calc_yimo_token,
    make_empty_server_verify,
)

_LOGGER: Final[logging.Logger] = logging.getLogger("tasks.battle_yimo")

#: 本模块允许出站的消息号
ALLOWED_PACKET_IDS: Final[dict[str, int]] = {
    "GetYimoState": 22101,
    "YimoBattleRequest": 22107,
    "YimoBattleResult": 22109,
}

#: 严禁出站的消息号（绝对禁止买次数）
FORBIDDEN_PACKET_IDS: Final[dict[str, int]] = {
    "BuyYimoCount": 22103,
    "BuyYimoCountPacket": 22103,
}

#: 报文用途说明
PACKET_PURPOSE: Final[dict[int, str]] = {
    22101: "查询失控炼成阵状态与剩余挑战次数（只读，零消耗）",
    22107: "申请进入失控炼成阵战斗（消耗免费挑战次数）",
    22109: "提交失控炼成阵伤害得分并结算发奖",
}


def calculate_dynamic_yimo_score(
    roles: tuple[dict[str, int], ...] | list[dict[str, int]],
    boss_lv: int,
    now_score: int,
    remain_count: int,
    custom_score: int | None = None,
    team_power: int | None = None,
    fallback_power: int = 2_000_000,
) -> tuple[int, int]:
    """动态估算失控炼成阵单场得分与出战战力。

    :param team_power: 出战战力。由调用方按**服务端下发的出战名单**折算后传入；
        未传时才回退账号名册。
    :param fallback_power: 名册也解不出战力时的兜底值（仅测试/异常路径会用到）。
    :return: (calculated_score, fighting_power)
    """
    if team_power is None:
        team_power = estimate_lineup_power(lineup_from_roles(roles)) or fallback_power

    if custom_score is not None and custom_score > 0:
        return custom_score, team_power

    # 1. 优先根据玩家今日已有场次历史表现推导基准分（带拟真微幅随机波动）
    battles_done = max(5 - remain_count, 0)
    if now_score > 0 and battles_done > 0:
        avg_score = now_score / battles_done
        jitter = random.uniform(0.97, 1.03)
        final_score = int(avg_score * jitter)
        return max(final_score, 1000), team_power

    # 2. 若今日为首次出战，根据名册英雄等级与 Boss 等级动态拟真推导
    top_levels = sorted([r.get("roleLV", 1) for r in roles], reverse=True)[:5] if roles else []
    avg_lv = sum(top_levels) / len(top_levels) if top_levels else 100.0

    # 单场伤害得分基准：根据 Boss 等级与英雄练度
    boss_scale = 1.0 + (boss_lv * 0.001)
    base_score = int((avg_lv / 100.0) * 48000 * boss_scale * random.uniform(0.96, 1.04))
    final_score = max(min(base_score, 115000), 5000)

    return final_score, team_power


@register_task
class YimoBattleTask(BaseTask):
    """失控炼成阵自动挑战与发奖结算任务。"""

    name: Final[str] = "battle_yimo"
    requires_auth: Final[bool] = True
    spends_diamond: Final[bool] = False
    accepts_run_options: Final[bool] = True

    def __init__(
        self,
        session: GameSession | None = None,
        *,
        logger: logging.Logger | None = None,
        dry_run: bool = False,
        spend_policy: SpendPolicy | None = None,
        run_options: RunOptions | None = None,
        battles: int | None = None,
        battle_wait_seconds: float | None = None,
        target_score: int | None = None,
        part: int | None = None,
        boss_lv: int | None = None,
        daily_only: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            session or GameSession(),
            logger=logger,
            dry_run=dry_run,
            spend_policy=spend_policy,
            run_options=run_options,
        )
        self.battles = battles
        self.battle_wait_seconds = battle_wait_seconds
        self.target_score = target_score
        self.part = part
        self.boss_lv = boss_lv
        if "daily_only" in kwargs:
            self.daily_only = bool(kwargs["daily_only"])
        else:
            self.daily_only = daily_only
        self.sent_packet_ids: list[int] = []

    def execute(self) -> TaskResult:
        """执行失控炼成阵挑战全流程。"""
        with self.open_game_client() as game:
            return self._run(game)

    def _run(self, game: GameClient) -> TaskResult:
        if self.battles is not None:
            planned_battles = self.battles
        elif self.run_options and self.run_options.battles != 0:
            planned_battles = self.run_options.battles
        else:
            # 默认打 1 场（兼顾单测与单次调用；-1 为打完全部）
            planned_battles = 1

        if planned_battles == 0:
            planned_battles = -1  # 显式传 0 表示打完全部可用免费次数

        completed_battles = 0
        last_new_rank = 0
        last_new_total = 0
        last_left_count = 0
        last_reward_count = 0
        last_score = 0

        while True:
            cancellation.raise_if_cancelled()

            # 1. 查询当前状态（22101）
            self.logger.info("正在查询失控炼成阵状态 (22101)...")
            view = guarded_send(
                game,
                GetYimoState(),
                GetYimoStateRes,
                allowed=ALLOWED_PACKET_IDS,
                forbidden=FORBIDDEN_PACKET_IDS,
                record=self.sent_packet_ids,
                purpose=PACKET_PURPOSE,
                logger=self.logger,
            )
            state = view.decode_sub_packet(GetYimoStateRes)
            remain_count = state.remainCount
            now_rank = state.nowRank
            now_score = state.nowScore

            if remain_count <= 0:
                if completed_battles == 0:
                    msg = f"失控炼成阵今日挑战次数已用尽（当前排名 {now_rank}，总分 {now_score}），跳过挑战"
                    self.logger.info(msg)
                    return TaskResult(
                        task=self.name,
                        ok=True,
                        message=msg,
                        data={
                            "remain_count": 0,
                            "rank": now_rank,
                            "score": now_score,
                            "battles_completed": 0,
                        },
                    )
                else:
                    self.logger.info("失控炼成阵挑战次数已用尽，结束本次任务。")
                    break

            # 动态获取当前可挑战部位与当前等级（绝不硬编码死数据）
            active_part, current_boss_lv = state.get_target_part_and_lv()
            target_part = self.part if self.part is not None else active_part
            target_boss_lv = self.boss_lv if self.boss_lv is not None else current_boss_lv

            self.logger.info(
                "失控炼成阵当前状态：剩余挑战次数 %d，当前排名 %d，当前总分 %d（动态选定部位 %d，Boss等级 %d）",
                remain_count,
                now_rank,
                now_score,
                target_part,
                target_boss_lv,
            )

            if self.dry_run:
                msg = f"【演练】检测到失控炼成阵剩余挑战次数 {remain_count}，演练模式跳过战斗"
                self.logger.info(msg)
                return TaskResult(
                    task=self.name,
                    ok=True,
                    message=msg,
                    data={
                        "remain_count": remain_count,
                        "rank": now_rank,
                        "part": target_part,
                        "boss_lv": target_boss_lv,
                        "dry_run": True,
                        "battles_completed": 0,
                    },
                )

            # 严格防风控校验：检测名册角色，0 人未配队严禁进战与发包（快速失败拦截）
            roles = getattr(getattr(self.session, "game_state", None), "roles", ())
            if not roles:
                msg = "检测到账号名册角色为空（0人未配队），禁止进入失控炼成阵战斗"
                self.logger.warning(msg)
                return TaskResult(
                    task=self.name,
                    ok=False,
                    message=msg,
                    data={"battles_completed": completed_battles},
                )

            # 2. 申请进战（22107）
            self.logger.info(
                "正在申请进入失控炼成阵战斗 (22107，部位 %d，Boss等级 %d)...",
                target_part,
                target_boss_lv,
            )
            req_view = guarded_send(
                game,
                YimoBattleRequest(part=target_part, bossLv=target_boss_lv),
                YimoBattleRequestRes,
                allowed=ALLOWED_PACKET_IDS,
                forbidden=FORBIDDEN_PACKET_IDS,
                record=self.sent_packet_ids,
                purpose=PACKET_PURPOSE,
                logger=self.logger,
                include_defaults=True,
            )
            req_res = req_view.decode_sub_packet(YimoBattleRequestRes)
            if not req_res.is_success():
                return TaskResult(
                    task=self.name,
                    ok=False,
                    message=f"进入失控炼成阵失败（errorCode={req_res.errorCode}）",
                    data={
                        "error_code": req_res.errorCode,
                        "battles_completed": completed_battles,
                    },
                )

            fight_token = req_res.fightToken
            self.logger.info("成功进入战斗，取得战斗令牌 fightToken: %d", fight_token)

            # 3. 防风控合理拟真耗时（炼成阵允许跳过动画，拟真 5~10 秒）
            if self.battle_wait_seconds is not None:
                if self.battle_wait_seconds > 0:
                    wait_time = (
                        random.uniform(
                            self.battle_wait_seconds * 0.9,
                            self.battle_wait_seconds * 1.1,
                        )
                        if self.battle_wait_seconds >= 10
                        else self.battle_wait_seconds
                    )
                else:
                    wait_time = 0.0
            else:
                wait_time = random.uniform(8.5, 13.5)

            if wait_time > 0:
                self.logger.info("模拟防风控战斗耗时中（等待 %.1f 秒）...", wait_time)
                cancellation.sleep_interruptible(wait_time)

            # 4. 出战名单取自服务端 22108.mineAssemble（战报存证唯一数据源）
            lineup = decode_lineup(req_res.mineAssemble)
            if not lineup:
                lineup = lineup_from_roles(roles)
                self.logger.warning(
                    "22108.mineAssemble 未解出我方阵容，回退账号名册（%d 人）", len(lineup)
                )
            if not lineup:
                msg = "服务端未下发我方阵容且账号名册为空，已放弃本场结算"
                self.logger.warning(msg)
                return TaskResult(
                    task=self.name,
                    ok=False,
                    message=msg,
                    data={"battles_completed": completed_battles, "aborted": "no_lineup"},
                )

            fighting_power = estimate_lineup_power(lineup)

            # 部位基础血量来自配置表（YimoPartData.basePartHp），不写死常量
            part_base_hp = load_part_base_hp().get(target_part, 0)
            boss_current_hp_rate = getattr(req_res, "hpRate", 10000)

            if self.daily_only:
                # 仅日常打卡安全模式：1200~2800 保底分；扣血万分比由「上报伤害 / 部位血量」折算，
                # 量级极小（约 0.0x%）→ 实际不改变全服 Boss 进度，但不再是一个写死的 0。
                battle_score = self.target_score if self.target_score is not None else random.randint(1200, 2800)
                reported_damage = battle_score
                dmg_hp_rate = (
                    min(int(reported_damage / part_base_hp * 10000), boss_current_hp_rate)
                    if part_base_hp > 0
                    else 0
                )
                self.logger.info(
                    "【日常打卡安全模式】生成保底得分: %d（出战战力: %d），按部位血量 %d 折算扣血万分比 %d",
                    battle_score,
                    fighting_power,
                    part_base_hp,
                    dmg_hp_rate,
                )
            else:
                battle_score, fighting_power = calculate_dynamic_yimo_score(
                    roles=roles,
                    boss_lv=target_boss_lv,
                    now_score=now_score,
                    remain_count=remain_count,
                    custom_score=self.target_score,
                    team_power=fighting_power,
                )
                reported_damage = battle_score
                dmg_hp_rate = (
                    min(int(reported_damage / part_base_hp * 10000), boss_current_hp_rate)
                    if part_base_hp > 0
                    else 0
                )

            last_score = battle_score

            token = calc_yimo_token(fight_token, battle_score)
            self.logger.info(
                "动态推导战斗得分: %d（扣血万分比: %d, 出战战力: %d），计算结算 Token: %s",
                battle_score,
                dmg_hp_rate,
                fighting_power,
                token,
            )

            empty_verify = make_empty_server_verify(
                section_id=target_part, battle_type=BATTLE_TYPE_YIMO
            )
            # dps 合计 == reported_damage（与扣血万分比同源，避免「报了伤害却 0 扣血」）
            my_team_records = build_lineup_records(
                lineup,
                total_damage=reported_damage,
                team_power=fighting_power,
                shape=YIMO_MINE,
            )
            battle_res_packet = YimoBattleResult(
                fightToken=fight_token,
                dmgHpRate=dmg_hp_rate,
                score=battle_score,
                battleResult=BattleResultClass(
                    teamId=80,
                    damage=0,  # 真机抓包 raw/26 确凿：失控炼成阵 damage 实际为 0（成绩由外层 score 承载）
                    yimoLv=target_boss_lv,
                    fightingPower=fighting_power,
                    damageContent=f"t=1$Empty$Score:{battle_score}",
                    myTeamRecord=my_team_records,
                    enemyTeamRecord=[],  # 真机抓包 raw/26 确凿：炼成阵 enemyTeamRecord 恒为空
                    battleServerVerify=empty_verify,
                ),
                token=token,
            )

            result_view = guarded_send(
                game,
                battle_res_packet,
                YimoBattleResultRes,
                allowed=ALLOWED_PACKET_IDS,
                forbidden=FORBIDDEN_PACKET_IDS,
                record=self.sent_packet_ids,
                purpose=PACKET_PURPOSE,
                logger=self.logger,
                include_defaults=True,
            )
            result_res = result_view.decode_sub_packet(YimoBattleResultRes)

            if not result_res.is_success():
                return TaskResult(
                    task=self.name,
                    ok=False,
                    message=f"失控炼成阵结算失败（errorCode={result_res.errorCode}）",
                    data={
                        "error_code": result_res.errorCode,
                        "battles_completed": completed_battles,
                    },
                )

            last_reward_count = len(result_res.rewardList)
            last_new_rank = result_res.rank
            last_new_total = result_res.totalValue
            last_left_count = result_res.remainCount
            completed_battles += 1

            # ★ 2026-10-18：逐场结果走 progress（网页「运行结果」实时显示）。
            self.progress(
                "第 %d 场完成：造成得分 %d，最新全服排名 %d，累计积分 %d，剩余次数 %d",
                completed_battles,
                battle_score,
                last_new_rank,
                last_new_total,
                last_left_count,
            )

            if planned_battles != -1 and completed_battles >= planned_battles:
                break

            # 场间拟真礼貌等待
            interval = (
                self.run_options.interval_seconds if self.run_options else 0.0
            )
            if interval > 0:
                inter_wait = interval
            elif self.battle_wait_seconds == 0:
                inter_wait = 0.0
            else:
                inter_wait = random.uniform(
                    config.TOP_PVP_INTERVAL_MIN, config.TOP_PVP_INTERVAL_MAX
                )
            if inter_wait > 0:
                self.logger.debug("等待 %.1f 秒拟真间隔后进入下一场...", inter_wait)
                cancellation.sleep_interruptible(inter_wait)

        msg = (
            f"失控炼成阵挑战成功！共完成 {completed_battles} 场，"
            f"单场得分 {last_score}，最新全服排名 {last_new_rank}，累计积分 {last_new_total}，"
            f"获得奖励项 {last_reward_count} 组，剩余挑战次数 {last_left_count}"
        )
        self.logger.info(msg)

        return TaskResult(
            task=self.name,
            ok=True,
            message=msg,
            data={
                "score": last_score,
                "rank": last_new_rank,
                "total_value": last_new_total,
                "remain_count": last_left_count,
                "reward_count": last_reward_count,
                "battles_completed": completed_battles,
                "daily_only": self.daily_only,
            },
        )
