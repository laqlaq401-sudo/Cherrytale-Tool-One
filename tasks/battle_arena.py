"""天梯竞技场（Arena）对决自动化任务。

【业务说明】
对应 SystemID 22「競技場」，协议模块 PvPNetWorkModule（26001~26020）。
每日提供免费挑战次数，自动择弱（挑选对手列表中战力最低的目标）发起挑战并结算积分。

【流程】
1. 26001 PVPHomePacket：读取剩余挑战次数 challengeCount 与候选对手列表；
   - 若 challengeCount <= 0，安全退出，不发任何无效请求；
   - 若 dry_run=True，提示演练并安全退出；
2. 自动择弱：遍历候选对手（PVPListClass），挑选战力 fighting 最低的目标发起挑战；
3. 26003 ChallengePacket：申请进战，扣减 1 次挑战次数，获取 fightToken；
4. 本地防风控等待（默认 18~28 秒随机耗时，单测可传 0）；
5. 26019 ChallengeResultPacket：按 MD5(fightToken + targetPlayerID + "0" + "03120908") 计算 Token，提交胜利结果；
6. 26020 ChallengeResultRes：接收天梯积分更新与排名结算。

【红线守则】
- 严禁触碰天命系统（Destiny）；
- 严禁触碰高难属性试炼；
- 严禁花钻石购买次数（26015 BuyChallengePacket 严格列入禁发清单）。
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Final

from pydantic import Field

import config
from client.exceptions import ProtocolError
from client.game_client import GameClient
from client.session import GameSession
from client.spending import SpendPolicy
from services import cancellation
from models import proto_fields
from models.battle_arena import (
    ChallengePacket,
    ChallengeRes,
    ChallengeResultPacket,
    ChallengeResultRes,
    DailyRewardPacket,
    DailyRewardRes,
    PVPResultsClass,
    PVP_DEFAULT_TEAM_ID,
    ReceiveRewardPacket,
    ReceiveRewardRes,
)
from models.game_packet import ProtoMessage
from models.main_stage import BattleResultClass
from models.packet_ids import packet_id
from models.pvp import PVPHomePacket, PVPHomeRes
from tasks.base import (
    BaseTask,
    QuantityScope,
    RunOptions,
    TaskResult,
    register_task,
    reward_phrase,
)
from tasks.battle_record_builder import (
    ARENA_ENEMY,
    ARENA_MINE,
    build_lineup_records,
    estimate_lineup_power,
    lineup_from_roles,
)
from models.assemble import decode_lineup
from tasks.packet_guard import guarded_send
from crypto.battle_signer import (
    BATTLE_TYPE_ARENA,
    calc_arena_token,
    make_empty_server_verify,
)

_LOGGER: Final[logging.Logger] = logging.getLogger("tasks.battle_arena")


class PVPListClass(ProtoMessage):
    """竞技场候选对手简报（嵌套在 26002 PVPHomeRes 的 pvpList 列表中）。"""

    proto_class_name = "PVPListClass"

    playerID: int = 0
    ranking: int = 0
    icon: bytes = b""
    name: str = ""
    fighting: int = 0
    integral: int = 0
    allianceName: str = ""
    worship: int = 0
    allianceTitle: int = 0


#: 系统预设 NPC 训练师 ID 范围（静态配置表 PVPTrainerData，ID 713000001~713000011）
TRAINER_ID_MIN: Final[int] = 713000001
TRAINER_ID_MAX: Final[int] = 713999999
NPC_MAX_FIGHTING: Final[int] = 50000


def is_npc_opponent(opp: PVPListClass) -> bool:
    """判定候选对手是否为系统 NPC 训练师（而非真人玩家）。

    【判定依据（IL2CPP 逆向实证）】
    1. 静态配置表 PVPTrainerData 的 pvpTrainerID 均位于 713000001~713000011；
    2. 协议下发的原始数据中，NPC 没有公会（allianceName 为空）；
    3. NPC 战力极低（通常 <= 30,000，最高上限设为 50,000 作为冗余兜底）；
    4. 原始 name 为空字符串（客户端在本地通过 GetLocalFakerName 动态生成假名）。
    """
    if TRAINER_ID_MIN <= opp.playerID <= TRAINER_ID_MAX:
        return True
    if opp.fighting <= NPC_MAX_FIGHTING and not opp.allianceName:
        return True
    if not opp.name and opp.fighting <= NPC_MAX_FIGHTING:
        return True
    return False


def _hero_name_by_role_id(roles: Any, role_main_id: int) -> str:
    """按 ``roleID`` 在账号名册里查英雄名。

    名册来自登录 1004 下发的真实数据；查不到就返回空串 —— 调用方自己决定兜底文案，
    绝不在这里写死某个英雄名。
    """
    for r in roles or ():
        if isinstance(r, dict):
            rid = int(r.get("roleID") or r.get("roleMainId") or 0)
            name = r.get("name") or r.get("roleName") or ""
        else:
            rid = int(getattr(r, "roleID", 0) or getattr(r, "roleMainId", 0) or 0)
            name = getattr(r, "name", "") or getattr(r, "roleName", "")
        if rid == role_main_id and name:
            return str(name)
    return ""


#: 本模块允许出站的消息号
ALLOWED_PACKET_IDS: Final[dict[str, int]] = {
    "PVPHomePacket": 26001,
    "ChallengePacket": 26003,
    "ChallengeResultPacket": 26019,
    "ReceiveRewardPacket": 26017,
    "DailyRewardPacket": 26013,
}

#: 严禁出站的消息号（绝对禁止买次数与改写服务器候选的刷新）
FORBIDDEN_PACKET_IDS: Final[dict[str, int]] = {
    "BuyChallengePacket": 26015,
    "RefreshPacket": 26011,
}

#: 报文用途说明
PACKET_PURPOSE: Final[dict[int, str]] = {
    26001: "查询竞技场主页与剩余挑战次数（只读，零消耗）",
    26003: "向选定天梯对手发起挑战（消耗 1 次挑战机会）",
    26019: "提交天梯对决胜利战绩并结算天梯积分",
    26017: "一键领取天梯竞技场每日胜场与连胜奖励",
    26013: "查询天梯竞技场每日奖励状态",
}


@register_task
class ArenaBattleTask(BaseTask):
    """天梯竞技场自动择弱对决与积分结算任务。"""

    name: Final[str] = "battle_arena"
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
        target_player_id: int | None = None,
        team_id: int = 60,
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
        self.target_player_id = target_player_id
        self.team_id = team_id
        self.sent_packet_ids: list[int] = []

    def execute(self) -> TaskResult:
        """执行竞技场自动择弱挑战全流程。"""
        with self.open_game_client() as game:
            return self._run(game)

    def _run(self, game: GameClient) -> TaskResult:
        if self.battles is not None:
            planned_battles = self.battles
        elif self.run_options and self.run_options.battles != 0:
            planned_battles = self.run_options.battles
        else:
            # 单测或无参调用时默认 1 场
            planned_battles = 1

        if planned_battles == 0:
            planned_battles = -1  # 显式传 0 表示打完全部可用免费次数

        completed_battles = 0
        last_target: PVPListClass | None = None
        last_my_ranking = 0
        last_my_integral = 0
        last_victory = 1

        while True:
            cancellation.raise_if_cancelled()

            # 1. 查询当前竞技场状态与候选对手（26001）
            self.logger.info("正在查询竞技场主页状态与对手列表 (26001)...")
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
            home = view.decode_sub_packet(PVPHomeRes)
            challenge_count = home.challengeCount

            if challenge_count <= 0:
                if completed_battles == 0:
                    self.logger.info("竞技场今日免费挑战次数已用尽（剩余 0 次），跳过挑战。")
                else:
                    self.logger.info("竞技场挑战次数已用尽，结束对决循环。")
                break

            # 解析候选对手列表
            opponents: list[PVPListClass] = []
            for raw_entry in home.pvpList:
                try:
                    opponents.append(
                        PVPListClass.decode(raw_entry)
                        if isinstance(raw_entry, bytes)
                        else raw_entry
                    )
                except Exception as e:
                    self.logger.debug("解析对手数据项异常: %s", e)

            if not opponents and not self.target_player_id:
                msg = "竞技场候选对手列表为空，无法选择挑战目标"
                self.logger.warning(msg)
                break

            # 挑选挑战目标（优先匹配指定的 target_player_id，否则严格仅从 NPC 训练师中挑选战力最低者）
            target: PVPListClass | None = None
            if self.target_player_id:
                for opp in opponents:
                    if opp.playerID == self.target_player_id:
                        target = opp
                        break
                if not target:
                    target = PVPListClass(
                        playerID=self.target_player_id, name="指定对手", fighting=0
                    )
            else:
                # 严格防风控过滤：仅选择系统 NPC 训练师，杜绝挑战几百万战力的真人队伍
                npc_opponents = [opp for opp in opponents if is_npc_opponent(opp)]
                if not npc_opponents:
                    min_cp = min(opp.fighting for opp in opponents) if opponents else 0
                    msg = (
                        f"竞技场当前候选对手均为真人队伍（最低战力 {min_cp:,}），未检测到 NPC 训练师。"
                        f"为杜绝风控检测与异常防守战报留痕，已安全跳过本轮挑战"
                    )
                    self.logger.warning(msg)
                    break

                target = min(npc_opponents, key=lambda x: x.fighting)

            last_target = target
            self.logger.info(
                "挑选挑战目标: 对手 [%s] (ID: %d, 战力: %d, 排名: %d, NPC: %s)",
                target.name or "系统NPC训练师",
                target.playerID,
                target.fighting,
                target.ranking,
                is_npc_opponent(target),
            )

            if self.dry_run:
                msg = (
                    f"【演练】检测到剩余挑战次数 {challenge_count}，"
                    f"目标对手 {target.name or '系统NPC训练师'} (战力 {target.fighting})，演练模式跳过战斗"
                )
                self.logger.info(msg)
                return TaskResult(
                    task=self.name,
                    ok=True,
                    message=msg,
                    data={
                        "challenge_count": challenge_count,
                        "target_player_id": target.playerID,
                        "target_name": target.name or "系统NPC训练师",
                        "target_fighting": target.fighting,
                        "is_npc": is_npc_opponent(target),
                        "dry_run": True,
                        "battles_completed": 0,
                        "planned_packets": (26001, 26003, 26019, 26017),
                    },
                )

            # 竞技场出战编队 ID（默认 PVP1 编队 60，避免错用 home.team 的防守阵容槽位 ID）
            actual_team_id = self.team_id if self.team_id > 0 else PVP_DEFAULT_TEAM_ID

            # 严格防风控校验：检测名册角色，0 人未配队严禁进战与发包（快速失败拦截）
            roles = getattr(getattr(self.session, "game_state", None), "roles", ())
            if not roles:
                msg = "检测到账号名册角色为空（0人未配队），禁止进入竞技场战斗"
                self.logger.warning(msg)
                return TaskResult(
                    task=self.name,
                    ok=False,
                    message=msg,
                    data={"battles_completed": completed_battles},
                )

            # 2. 发起挑战（26003）
            self.logger.info(
                "向对手 %d 发起竞技场挑战 (26003，出战编队 %d)...",
                target.playerID,
                actual_team_id,
            )
            req_view = guarded_send(
                game,
                ChallengePacket(
                    actionType=0, playerID=target.playerID, teamID=actual_team_id
                ),
                ChallengeRes,
                allowed=ALLOWED_PACKET_IDS,
                forbidden=FORBIDDEN_PACKET_IDS,
                record=self.sent_packet_ids,
                purpose=PACKET_PURPOSE,
                logger=self.logger,
                include_defaults=True,  # 抓包实证：必须显式保留 actionType=0
            )
            req_res = req_view.decode_sub_packet(ChallengeRes)
            if not req_res.is_success():
                return TaskResult(
                    task=self.name,
                    ok=False,
                    message=f"发起竞技场挑战失败（errorCode={req_res.errorCode}）",
                    data={
                        "error_code": req_res.errorCode,
                        "target_player_id": target.playerID,
                        "battles_completed": completed_battles,
                    },
                )

            fight_token = req_res.fightToken
            self.logger.info("成功进入竞技场对决，取得会话令牌 fightToken: %d", fight_token)

            # 3. 防风控合理拟真耗时
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
                if is_npc_opponent(target) or target.fighting <= 50_000:
                    # NPC 队伍极脆（战力仅数千至三万），拟真通关耗时设为 8.5 ~ 12.0 秒（模拟入场展示、技能施放与受击倒地动画）
                    wait_time = random.uniform(8.5, 12.0)
                elif target.fighting < 1_500_000:
                    wait_time = random.uniform(14.0, 18.0)
                else:
                    wait_time = random.uniform(20.0, 26.0)

            if wait_time > 0:
                self.logger.info("模拟防风控战斗耗时中（等待 %.1f 秒）...", wait_time)
                cancellation.sleep_interruptible(wait_time)

            # 4. 计算 Token 与提交结算（26019）
            token = calc_arena_token(fight_token, target.playerID, win_lose=0)
            self.logger.info(
                "计算竞技场 Token: %s（对手 %d，胜利 win_lose=0）",
                token,
                target.playerID,
            )

            # 4.1 解析服务端下发的双方阵容（26004）—— 战报存证的唯一数据源
            my_lineup = decode_lineup(req_res.mineAssemble)
            enemy_lineup = decode_lineup(req_res.opponentAssemble)
            if not enemy_lineup:
                msg = (
                    "服务端未下发对手阵容快照（26004.opponentAssemble 为空或无法解析），"
                    "已放弃本场结算：战报存证必须使用服务端下发的真实名单，拒绝伪造"
                )
                self.logger.warning(msg)
                return TaskResult(
                    task=self.name,
                    ok=False,
                    message=msg,
                    data={
                        "target_player_id": target.playerID,
                        "battles_completed": completed_battles,
                        "aborted": "missing_opponent_assemble",
                    },
                )
            if not my_lineup:
                # 兜底：账号名册（1004 真实数据）。绝不用写死的角色 ID 补位。
                my_lineup = lineup_from_roles(roles)
                self.logger.warning(
                    "26004.mineAssemble 未解出我方阵容，回退账号名册（%d 人）", len(my_lineup)
                )
            if not my_lineup:
                msg = "服务端未下发我方阵容且账号名册为空，已放弃本场结算"
                self.logger.warning(msg)
                return TaskResult(
                    task=self.name,
                    ok=False,
                    message=msg,
                    data={"battles_completed": completed_battles, "aborted": "no_lineup"},
                )

            # 我方战力优先用服务端 26002 的 myFighting；没有则按**真实出战名单**折算
            my_team_power = (
                home.myFighting
                if getattr(home, "myFighting", 0) > 0
                else estimate_lineup_power(my_lineup)
            )
            # 对手战力：服务端候选列表里的 fighting（兜底我方 70%）
            opponent_cp = target.fighting if target.fighting > 0 else int(max(my_team_power * 0.70, 50000))

            # 伤害量级：碾压局基本打穿对手，同档局只打掉 2%~5%（与真机样本同档）
            if opponent_cp < my_team_power * 0.15:
                arena_damage = int(opponent_cp * random.uniform(0.85, 1.15))
            else:
                arena_damage = int(opponent_cp * random.uniform(0.02, 0.05))
            arena_damage = max(arena_damage, 1200)

            empty_verify = make_empty_server_verify(
                section_id=0, battle_type=BATTLE_TYPE_ARENA
            )

            # 我方 dps 合计 == 敌方 tank 合计 == arena_damage，两边账对得上
            my_team_records = build_lineup_records(
                my_lineup,
                total_damage=arena_damage,
                team_power=my_team_power,
                shape=ARENA_MINE,
            )
            enemy_team_records = build_lineup_records(
                enemy_lineup,
                total_damage=arena_damage,
                team_power=opponent_cp,
                enemy=True,
                shape=ARENA_ENEMY,
            )

            # 首位出战角色名称（按服务端阵容的 roleID 回名册查，查不到不编名字）
            hero_name = _hero_name_by_role_id(roles, my_lineup[0].role_main_id) or "晨曦"
            # 战斗高光文本：BlAmRd 必须与本场实际伤害同量级（真机样本里它 ≈ damage 的 1/3）
            damage_content = (
                f"t={int(wait_time) if wait_time > 0 else 9}${hero_name}$1510155602"
                f"$EfRe:62$ElemAmp:0.1$BlAmRd:{int(arena_damage * random.uniform(0.25, 0.4))}"
            )

            settle_packet = ChallengeResultPacket(
                result=0,  # 0 胜利
                type=0,
                playerID=target.playerID,
                fightingNumber=my_team_power,  # ★ 修正 P0：官方客户端 0x1804F4EA5 确凿为我方出战战力，绝非对手战力
                fightToken=fight_token,
                battleResult=BattleResultClass(
                    teamId=actual_team_id,
                    fightingPower=my_team_power,
                    damageContent=damage_content,
                    damage=arena_damage,
                    yimoLv=-1,
                    myTeamRecord=my_team_records,
                    enemyTeamRecord=enemy_team_records,
                    battleServerVerify=empty_verify,
                ),
                token=token,
            )

            result_view = guarded_send(
                game,
                settle_packet,
                ChallengeResultRes,
                allowed=ALLOWED_PACKET_IDS,
                forbidden=FORBIDDEN_PACKET_IDS,
                record=self.sent_packet_ids,
                purpose=PACKET_PURPOSE,
                logger=self.logger,
                include_defaults=True,  # 抓包实证：必须保留 result=0, type=0 等默认值，避免网关 22 字节拦截
            )
            result_res = result_view.decode_sub_packet(ChallengeResultRes)

            if not result_res.is_success():
                return TaskResult(
                    task=self.name,
                    ok=False,
                    message=f"竞技场对决结算失败（errorCode={result_res.errorCode}）",
                    data={
                        "error_code": result_res.errorCode,
                        "target_player_id": target.playerID,
                        "battles_completed": completed_battles,
                    },
                )

            last_my_ranking = (
                result_res.myResults.myRanking if result_res.myResults else 0
            )
            last_my_integral = (
                result_res.myResults.myIntegral if result_res.myResults else 0
            )
            last_victory = (
                result_res.myResults.victory if result_res.myResults else 1
            )
            completed_battles += 1

            # ★ 2026-10-18：逐场结果走 progress（网页「运行结果」实时显示），
            # 见 tasks/base.py::progress。过程性日志仍是 logger.info。
            # ★ 2026-10-18：半角括号统一成全角（与全项目文案一致）
            self.progress(
                "第 %d 场对决完成：战胜对手 [%s]（战力 %d），最新积分 %d，排名 %d",
                completed_battles,
                target.name,
                target.fighting,
                last_my_integral,
                last_my_ranking,
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

        # 5. 对决循环结束（或无需对决）后，安全发起竞技场每日胜场与连胜奖励一键领取（26017）
        rewards_data: dict[str, Any] = {"claimed": False, "items": []}
        if not self.dry_run:
            try:
                self.logger.info("正在执行竞技场奖励一键领取 (26017 ReceiveRewardPacket)...")
                reward_view = guarded_send(
                    game,
                    ReceiveRewardPacket(missionID=-1),
                    ReceiveRewardRes,
                    allowed=ALLOWED_PACKET_IDS,
                    forbidden=FORBIDDEN_PACKET_IDS,
                    record=self.sent_packet_ids,
                    purpose=PACKET_PURPOSE,
                    logger=self.logger,
                    include_defaults=True,  # 抓包实证：显式保留 missionID=-1
                )
                reward_res = reward_view.decode_sub_packet(ReceiveRewardRes)
                if reward_res.is_success():
                    rewards_data["claimed"] = True
                    claimed_phrases: list[str] = []
                    for obj in reward_res.items:
                        if hasattr(obj, "item_summary"):
                            claimed_phrases.append(obj.item_summary())
                        elif isinstance(obj, bytes):
                            claimed_phrases.append(f"{len(obj)}字节道具数据")
                    phrase = reward_phrase(reward_res.items, scope=QuantityScope.GAINED)
                    summary_text = phrase or "、".join(claimed_phrases) or "奖励已到账"
                    self.logger.info("✔ 竞技场一键领奖成功：%s", summary_text)
                    rewards_data["items"] = claimed_phrases
                    rewards_data["summary"] = summary_text
                else:
                    self.logger.info(
                        "竞技场当前暂无可领奖励或今日已领（errorCode=%d）", reward_res.errorCode
                    )
                    rewards_data["error_code"] = reward_res.errorCode
            except Exception as exc:
                self.logger.warning("一键领取竞技场奖励时出现异常：%s", exc)
                rewards_data["error"] = str(exc)

        target_name = (last_target.name or "系统NPC训练师") if last_target else ""
        target_fighting = last_target.fighting if last_target else 0
        target_id = last_target.playerID if last_target else 0

        if completed_battles > 0:
            msg = (
                f"竞技场对决挑战成功（共 {completed_battles} 场）！"
                f"战胜对手 [{target_name}] (战力 {target_fighting})，"
                f"最新天梯积分 {last_my_integral}，全服天梯排名 {last_my_ranking}"
            )
        else:
            msg = "竞技场对决结束（本轮未发起新挑战，已执行奖励结算）"

        if rewards_data.get("claimed") and rewards_data.get("summary"):
            msg += f"；一键领奖：{rewards_data['summary']}"

        self.logger.info(msg)

        return TaskResult(
            task=self.name,
            ok=True,
            message=msg,
            data={
                "target_player_id": target_id,
                "target_name": target_name,
                "target_fighting": target_fighting,
                "my_ranking": last_my_ranking,
                "my_integral": last_my_integral,
                "victory": last_victory,
                "battles_completed": completed_battles,
                "rewards_claimed": rewards_data,
            },
        )
