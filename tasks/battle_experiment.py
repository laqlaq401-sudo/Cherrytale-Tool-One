"""辉煌航迹·试炼与每日终极挑战（Experiment）战斗自动化任务。

【业务说明】
对应 SystemID 77「輝煌航跡」中的试炼系统，协议模块 ExperimentModule（34001~34026）。
包含：
1. 每日「終極試煉」（关卡 671000000 ~ 671000006，共 7 关，每日 1 次挑战额度）；
2. 基础「初始试炼」（关卡 671000100 ~ 671000141，共 42 关，按需推进）。

【自动化全流程】
1. 34007 ExperimentInfo：查询试炼当前进度与终极挑战额度；
   - 检查 infoClass.finalTestCount：
     - 若 finalTestCount <= 0，提示今日终极挑战已完成，安全退出；
     - 若 dry_run=True，提示演练并安全退出；
2. 提取当前待挑战关卡 sectionID（例如 671000001 终极试炼 2）；
3. 34009 ExperimentFightingRequest：申请进入关卡，下发 fightToken；
4. 本地防风控等待（默认 18~28 秒随机耗时，单测传 0）；
5. 34011 ExperimentFightingResult：提交战斗胜负（携带 FightDataClass(token, sectionID) 与我方存活战绩）；
6. 34012 ExperimentFightingResultRes：接收通关道具奖励，完成当日打卡！

【安全与红线守则】
- 严禁触碰天命系统（Destiny）；
- 严禁触碰高难属性试炼（675000000 系列的火山/极地/荒漠，需属性钥匙）；
- 严禁花钻石复活角色（34025 GrandLineRebirthPacket 严格列入禁发清单）。
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Final

from pydantic import Field

from client.exceptions import ProtocolError
from client.game_client import GameClient
from client.session import GameSession
from models.battle_experiment import (
    ExperimentBattleProtoMessage,
    ExperimentFightingRequest,
    ExperimentFightingRequestRes,
    ExperimentFightingResult,
    ExperimentFightingResultRes,
    ExperimentRoleClass,
    ExperimentTeamClass,
    get_mirror_machine_id,
)
from models.daily import GetObjClass
from models.main_stage import BattleResultClass, FightDataClass
from models.packet_ids import packet_id
from models.assemble import decode_lineup
from tasks.base import BaseTask, RunOptions, TaskResult, register_task
from tasks.battle_record_builder import (
    EXPERIMENT_MINE,
    build_lineup_records,
    estimate_lineup_power,
    lineup_from_roles,
)
from tasks.packet_guard import guarded_send
from services import cancellation
from crypto.battle_signer import (
    BATTLE_TYPE_EXPERIMENT,
    make_empty_server_verify,
)

_LOGGER: Final[logging.Logger] = logging.getLogger("tasks.battle_experiment")



class ExperimentInfo(ExperimentBattleProtoMessage):
    """34007 查询试炼当前状态与每日终极挑战剩余次数（空报文，只读）。"""

    proto_class_name = "ExperimentInfo"


class ExperimentInfoClass(ExperimentBattleProtoMessage):
    """34008 响应中嵌套的试炼与终极挑战核心数据。"""

    proto_class_name = "ExperimentInfoClass"

    #: 当前待推进的关卡 ID（终极试炼通常在 671000000~671000006 之间）
    sectionID: int = 671000000
    #: 怪物血量状态
    monsterHpList: list[ExperimentTeamClass] = Field(default_factory=list)
    monster_mana: int = 0
    #: 我方角色血量状态
    roleHpList: list[ExperimentRoleClass] = Field(default_factory=list)
    role_mana: int = 0
    #: ★ 终极挑战今日剩余挑战次数（1 表示未挑战，0 表示今日已打过）
    finalTestCount: int = 0
    #: 复活/恢复次数
    restoreCount: int = 0
    #: 镜像增益 ID
    mirrorBuffID: int = 0


class ExperimentInfoRes(ExperimentBattleProtoMessage):
    """34008 试炼状态响应。"""

    proto_class_name = "ExperimentInfoRes"

    errorCode: int = 0
    infoClass: ExperimentInfoClass | None = None
    sectionList: list[bytes] = Field(default_factory=list)
    isAutoReward: int = 0
    getRewardList: list[GetObjClass] = Field(default_factory=list)
    nextResetTime: int = 0

    def is_success(self) -> bool:
        return self.errorCode == 0


#: 本模块允许出站的消息号
ALLOWED_PACKET_IDS: Final[dict[str, int]] = {
    "ExperimentInfo": 34007,
    "ExperimentFightingRequest": 34009,
    "ExperimentFightingResult": 34011,
}

#: 严禁出站的消息号（绝对禁止买活/复活）
FORBIDDEN_PACKET_IDS: Final[dict[str, int]] = {
    "GrandLineRebirthPacket": 34025,
}

#: 报文用途说明
PACKET_PURPOSE: Final[dict[int, str]] = {
    34007: "查询试炼当前关卡与每日终极挑战剩余次数（只读，零消耗）",
    34009: "申请进入试炼关卡战斗（消耗当日挑战机会）",
    34011: "提交试炼通关战绩并领取道具奖励",
}


@register_task
class ExperimentBattleTask(BaseTask):
    """辉煌航迹·试炼与终极挑战每日自动打卡任务。"""

    name: Final[str] = "battle_experiment"
    requires_auth: Final[bool] = True
    spends_diamond: Final[bool] = False

    def __init__(
        self,
        session: GameSession | None = None,
        *,
        logger: logging.Logger | None = None,
        dry_run: bool = False,
        spend_policy: SpendPolicy | None = None,
        run_options: RunOptions | None = None,
        battle_wait_seconds: float | None = None,
        target_section_id: int | None = None,
        team_sid: list[int] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            session or GameSession(),
            logger=logger,
            dry_run=dry_run,
            spend_policy=spend_policy,
            run_options=run_options,
        )
        self.battle_wait_seconds = battle_wait_seconds
        self.target_section_id = target_section_id
        # 默认不设死数据，进战时优先动态从 34008 roleHpList 提取当前布阵英雄
        self.team_sid = team_sid
        self.sent_packet_ids: list[int] = []

    def execute(self) -> TaskResult:
        """执行试炼/终极挑战每日自动打卡。"""
        with self.open_game_client() as game:
            return self._run(game)

    def _run(self, game: GameClient) -> TaskResult:
        cancellation.raise_if_cancelled()

        # 1. 查询试炼当前状态（34007）
        self.logger.info("正在查询试炼当前状态与终极挑战剩余次数 (34007)...")
        view = guarded_send(
            game,
            ExperimentInfo(),
            ExperimentInfoRes,
            allowed=ALLOWED_PACKET_IDS,
            forbidden=FORBIDDEN_PACKET_IDS,
            record=self.sent_packet_ids,
            purpose=PACKET_PURPOSE,
            logger=self.logger,
        )
        status_res = view.decode_sub_packet(ExperimentInfoRes)
        if not status_res.is_success() or not status_res.infoClass:
            return TaskResult(
                task=self.name,
                ok=False,
                message=f"查询试炼状态失败（errorCode={status_res.errorCode}）",
                data={"error_code": status_res.errorCode},
            )

        info = status_res.infoClass
        current_section = self.target_section_id or info.sectionID
        final_test_count = info.finalTestCount

        self.logger.info(
            "试炼状态：当前待挑战关卡 ID: %d，终极挑战今日剩余次数: %d",
            current_section,
            final_test_count,
        )

        # 检查是否为终极挑战（671000000 ~ 671000006）
        is_ultimate_trial = (671000000 <= current_section <= 671000006)

        if is_ultimate_trial and final_test_count <= 0:
            msg = f"终极挑战（关卡 {current_section}）今日挑战次数已用尽，无需重复挑战"
            self.logger.info(msg)
            return TaskResult(
                task=self.name,
                ok=True,
                message=msg,
                data={
                    "section_id": current_section,
                    "final_test_count": 0,
                    "is_ultimate_trial": True,
                },
            )

        if self.dry_run:
            msg = f"【演练】检测到当前关卡 {current_section}（终极挑战剩余 {final_test_count} 次），演练模式跳过战斗"
            self.logger.info(msg)
            return TaskResult(
                task=self.name,
                ok=True,
                message=msg,
                data={
                    "section_id": current_section,
                    "final_test_count": final_test_count,
                    "dry_run": True,
                },
            )

        cancellation.raise_if_cancelled()

        # 动态提取真实出战队伍 SID（绝不硬编码他人的抓包 SID）
        if self.team_sid:
            active_team_sids = list(self.team_sid)
        else:
            # ① 优先从 34008 下发的 roleHpList 提取当前已布阵（isTeam == 1）的英雄
            active_team_sids = [
                r.role_sid for r in info.roleHpList if r.isTeam == 1 and r.role_sid > 0
            ]
            # ② 若玩家尚未在试炼布阵（全部为 0），从当前名册选取存活且血量/SID 最高的 5 名英雄
            if not active_team_sids and info.roleHpList:
                sorted_roles = sorted(
                    [r for r in info.roleHpList if r.role_sid > 0],
                    key=lambda x: (x.hp, x.role_sid),
                    reverse=True,
                )
                active_team_sids = [r.role_sid for r in sorted_roles[:5]]

        if len(active_team_sids) < 5:
            # 若仍不足 5 人，尝试从已知会话名册补足
            known_sids = [
                r.get("roleSid", 0)
                for r in getattr(getattr(self.session, "game_state", None), "roles", ())
                if r.get("roleSid", 0) > 0
            ]
            for sid in known_sids:
                if sid not in active_team_sids:
                    active_team_sids.append(sid)
                if len(active_team_sids) >= 5:
                    break
        active_team_sids = active_team_sids[:5]

        if not active_team_sids:
            msg = "未在试炼系统或账号名册中检索到可用出战角色，已安全中止"
            self.logger.warning(msg)
            return TaskResult(
                task=self.name,
                ok=False,
                message=msg,
                data={"section_id": current_section},
            )

        self.logger.info("出战阵容 5 人 SID: %s (来源: 当前试炼布阵)", active_team_sids)

        # 2. 申请进战（34009）
        mirror_machine_id = get_mirror_machine_id(info.mirrorBuffID)
        self.logger.info(
            "正在申请进入试炼战斗 (34009，关卡 %d，Buff %d，装置 %d)...",
            current_section,
            info.mirrorBuffID,
            mirror_machine_id,
        )
        req_view = guarded_send(
            game,
            ExperimentFightingRequest(
                sectionID=current_section,
                version=-1,
                teamSid=active_team_sids,
                mirrorBuffID=info.mirrorBuffID,
                mirrorMachineID=mirror_machine_id,
            ),
            ExperimentFightingRequestRes,
            allowed=ALLOWED_PACKET_IDS,
            forbidden=FORBIDDEN_PACKET_IDS,
            record=self.sent_packet_ids,
            purpose=PACKET_PURPOSE,
            logger=self.logger,
            include_defaults=True,
        )
        req_res = req_view.decode_sub_packet(ExperimentFightingRequestRes)
        if not req_res.is_success():
            return TaskResult(
                task=self.name,
                ok=False,
                message=f"进入试炼关卡 {current_section} 失败（errorCode={req_res.errorCode}）",
                data={"error_code": req_res.errorCode, "section_id": current_section},
            )

        fight_token = req_res.fightToken
        self.logger.info("成功进入关卡 %d，取得令牌 fightToken: %d", current_section, fight_token)

        # 3. 防风控合理拟真耗时（方案 A：10.0~15.0 秒）
        if self.battle_wait_seconds is not None:
            if self.battle_wait_seconds > 0:
                wait_time = (
                    random.uniform(self.battle_wait_seconds * 0.9, self.battle_wait_seconds * 1.1)
                    if self.battle_wait_seconds >= 10
                    else self.battle_wait_seconds
                )
            else:
                wait_time = 0.0
        else:
            wait_time = random.uniform(10.0, 15.0)

        if wait_time > 0:
            self.logger.info("模拟防风控战斗耗时中（等待 %.1f 秒）...", wait_time)
            cancellation.sleep_interruptible(wait_time)

        # 4. 出战名单以服务端 34010.mineAssemble 为准（战报存证唯一数据源）
        roles = getattr(getattr(self.session, "game_state", None), "roles", ())
        lineup = decode_lineup(req_res.mineAssemble)
        if not lineup:
            # 兜底：账号名册（1004 真实数据）按 34008 的出战 sid 过滤；绝不补造角色
            lineup = lineup_from_roles(roles, active_team_sids)
            self.logger.warning(
                "34010.mineAssemble 未解出我方阵容，回退账号名册（%d 人）", len(lineup)
            )
        if not lineup:
            msg = "服务端未下发我方阵容且账号名册为空，已放弃本场结算"
            self.logger.warning(msg)
            return TaskResult(
                task=self.name,
                ok=False,
                message=msg,
                data={"section_id": current_section, "aborted": "no_lineup"},
            )

        team_power = estimate_lineup_power(lineup)

        # 动态对齐怪物阵亡列表（优先按 34008 真实存在的怪物位置）
        if info.monsterHpList:
            monster_hps = [
                ExperimentTeamClass(position=m.position, hp=0)
                for m in info.monsterHpList
            ]
        else:
            monster_hps = [ExperimentTeamClass(position=i, hp=0) for i in range(1, 6)]

        # 我方存活状态：通关以近乎满血收尾（真机样本为 98/100/100/100/100）
        role_hps = [
            ExperimentRoleClass(role_sid=member.role_sid, hp=100, isTeam=1)
            for member in lineup
        ]
        if len(role_hps) >= 3:
            role_hps[random.randrange(len(role_hps))].hp = random.randint(96, 99)

        empty_verify = make_empty_server_verify(
            section_id=current_section, battle_type=BATTLE_TYPE_EXPERIMENT
        )
        # 本场总伤害按**真实出战名单**的战力折算（真机样本约为队伍战力的 17%）
        total_damage = max(int(team_power * random.uniform(0.12, 0.20)), 1000)
        my_team_records = build_lineup_records(
            lineup,
            total_damage=total_damage,
            team_power=team_power,
            shape=EXPERIMENT_MINE,
        )

        settle_packet = ExperimentFightingResult(
            winLose=1,  # 实测线上通关样本 winLose = 1
            monsterHpList=monster_hps,
            # 残蓝：真机通关样本为非零值（2080 / 1920），0 反而是不可能出现的状态
            monster_mana=random.randint(1500, 2500),
            roleHpList=role_hps,
            role_mana=random.randint(1500, 2500),
            battleResult=BattleResultClass(
                teamId=7,  # TeamType.Experiment = 7（对齐 dump.cs，绝非 40）
                fightingPower=team_power,
                damageContent="t=3$Empty",  # 真机抓包 raw/09 确凿：试炼 damageContent 仅为字面量 "t=3$Empty"
                damage=0,  # 真机抓包 raw/09 确凿：试炼结算 damage 字段为 0（伤害由各单位 HP 变化承载）
                yimoLv=-1,
                myTeamRecord=my_team_records,
                enemyTeamRecord=[],  # 真机抓包 raw/09 确凿：试炼 enemyTeamRecord 恒为空
                battleServerVerify=empty_verify,
            ),
            fightData=FightDataClass(token=fight_token, sectionId=current_section),
        )

        result_view = guarded_send(
            game,
            settle_packet,
            ExperimentFightingResultRes,
            allowed=ALLOWED_PACKET_IDS,
            forbidden=FORBIDDEN_PACKET_IDS,
            record=self.sent_packet_ids,
            purpose=PACKET_PURPOSE,
            logger=self.logger,
            include_defaults=True,
        )
        result_res = result_view.decode_sub_packet(ExperimentFightingResultRes)

        if not result_res.is_success():
            return TaskResult(
                task=self.name,
                ok=False,
                message=f"试炼关卡 {current_section} 结算失败（errorCode={result_res.errorCode}）",
                data={"error_code": result_res.errorCode, "section_id": current_section},
            )

        reward_count = len(result_res.rewardList)
        msg = (
            f"试炼挑战成功！关卡 {current_section}"
            f"{' (终极试炼)' if is_ultimate_trial else ''} 已通关，获得道具奖励项 {reward_count} 组"
        )
        self.logger.info(msg)

        return TaskResult(
            task=self.name,
            ok=True,
            message=msg,
            data={
                "section_id": current_section,
                "is_ultimate_trial": is_ultimate_trial,
                "reward_count": reward_count,
            },
        )
