"""主线关卡通关任务（push_main_stage）。

【业务链路与设计依据】
1. 起始关卡定位：
   - 调用 11011 ``GetLastSectionPacket`` 获取玩家当前已通关关卡与边界关卡。
   - 过滤得到最高已通关关卡，并顺次定位至下一待攻打关卡（主线无需重复刷取，始终向前推进）。
2. 安全护栏与风控设计：
   - 敏感校验关卡（`cheatCheck=1`）：主线 537 关中仅 23 关开启服务端全量校验。
     遵守风控规范，**绝对不提供自动绕过开关**，遇到立即停止并提醒用户在真机/游戏客户端中手动通关。
   - 绝不主动购买体力：推图仅消耗现有体力，绝不调用 24005/24011 等买体力协议；
     体力不足时平稳停机。
   - 反倍速防刷机制：开战与结算之间模拟真实战斗耗时（默认 15~25 秒），杜绝 0 秒结算。
3. 战斗结算与三星保证：
   - 11015 ``FightingRequestPacket{sectionID, teamID=1}`` 获得 ``fightToken``。
   - 模拟延迟后，依逆向算法生成校验签名与存证：
     ``token = MD5(f"{fightToken}{timeCostSec}{winLose}03120908")``
     ``battleServerVerify = Base64(Gzip({"BattleType":0,"SectionId":sec,"Proofs":[]}))``
   - 11017 ``FightingResultPacket`` 上报 ``starCount=[1, 1, 1]``，确保 100% 满星通过。
4. 小章节满星宝箱领取：
   - 当小章节（Area）的最后一关（``is_last_in_area=True``）通关后，小章累计满星；
   - 自动发送 11007 ``ReceiveStarRewardPacket{areaID}`` 领取本小章的全部星数宝箱。
"""

from __future__ import annotations

import json
import logging
import random
import time
from typing import Any, Final

from client.game_client import GameClient
from client.session import GameSession
from client.spending import SpendPolicy
from models.envelope import EnvelopeView
from models.game_config import (
    MainStageArea,
    MainStageSection,
    load_main_stage_index,
)
from models.game_packet import ProtoMessage
from models.main_stage import (
    ALLOWED_PACKET_IDS,
    FORBIDDEN_PACKET_IDS,
    PACKET_PURPOSE,
    TEAM_TYPE_PVE1,
    BattleAchiAwakeClass,
    BattleAchiComboClass,
    BattleAchiInterupClass,
    BattleResultClass,
    FightDataClass,
    FightingRequestPacket,
    FightingResponseRes,
    FightingResultPacket,
    FightingResultRes,
    GetLastSectionPacket,
    GetLastSectionRes,
    PVPVerifyPacket,
    ReceiveStarRewardPacket,
    ReceiveStarRewardRes,
    SetUseTeamIDPacket,
    SetUseTeamIDRes,
    make_battle_server_verify,
    make_fight_verify_token,
)
from models.wallet import GetAllItemPacket, GetAllItemRes
from services import cancellation
from tasks.base import BaseTask, QuantityScope, TaskResult, register_task, reward_phrase
from tasks.battle_record_builder import (
    build_enemy_team_records,
    build_my_team_records,
    estimate_role_power,
)
from tasks.packet_guard import guarded_send

_LOGGER: Final[logging.Logger] = logging.getLogger("tasks.main_stage")

#: 已通关状态码（与 models.activity_stage.PASSED_SECTION_STATE 保持一致）
PASSED_SECTION_STATE: Final[int] = 2


def resolve_main_stage_progress(
    section_states: list[Any],
    sec_map: dict[int, MainStageSection],
    ordered_ids: list[int],
) -> dict[str, Any]:
    """根据服务端 11012 返回的关卡状态列表与静态配置，解析当前主线最高通关与下一待推关卡。"""
    passed_ids = {
        state.sectionID
        for state in section_states
        if getattr(state, "sectionState", None) == PASSED_SECTION_STATE
    }
    highest_passed_id: int | None = None
    highest_idx = -1
    for sid in passed_ids:
        if sid in sec_map:
            idx = ordered_ids.index(sid)
            if idx > highest_idx:
                highest_idx = idx
                highest_passed_id = sid

    if highest_passed_id is not None:
        sec = sec_map[highest_passed_id]
        highest_desc = sec.friendly_name
        highest_area_id = sec.area_id
        highest_area_name = sec.area_name
        next_sec_id = sec.next_section_id
        next_sec_desc = (
            sec_map[next_sec_id].friendly_name
            if next_sec_id and next_sec_id in sec_map
            else "已全部通关"
        )
        is_all_cleared = next_sec_id is None
    else:
        highest_desc = "尚未通关任何主线关卡"
        highest_area_id = None
        highest_area_name = ""
        # 寻找已解锁关卡或第一关
        unlocked = [
            s.sectionID
            for s in section_states
            if getattr(s, "sectionState", None) == 0 and s.sectionID in sec_map
        ]
        next_sec_id = unlocked[0] if unlocked else (ordered_ids[0] if ordered_ids else None)
        next_sec_desc = (
            sec_map[next_sec_id].friendly_name
            if next_sec_id and next_sec_id in sec_map
            else "无"
        )
        is_all_cleared = False

    return {
        "highest_passed_id": highest_passed_id,
        "highest_passed_desc": highest_desc,
        "highest_passed_area_id": highest_area_id,
        "highest_passed_area_name": highest_area_name,
        "next_section_id": next_sec_id,
        "next_section_desc": next_sec_desc,
        "is_all_cleared": is_all_cleared,
        "passed_count": len(passed_ids),
        "total_count": len(sec_map),
    }


def resolve_account_team_power(
    roles: Any = None,
    fallback_power: int = 250000,
) -> int:
    """基于当前登录账号名册实际获得的英雄卡牌属性计算出战队伍战力。

    【业务依据与安全红线】
    严禁使用关卡配置表的 suggest_cp 捏造战力（杜绝高练大号打前章战力倒挂）。
    100% 绑定当前登录账号在 1004 / 5011 中实际记录的角色等级与星级。
    """
    if not roles:
        return fallback_power
    valid_roles = [r for r in roles if isinstance(r, dict) and r.get("roleSid", 0) > 0]
    if not valid_roles:
        return fallback_power
    # 取账号练度最高的 5 名出战角色
    top_5 = sorted(
        valid_roles,
        key=lambda x: (x.get("roleLV", 1), x.get("roleStar", 1)),
        reverse=True,
    )[:5]
    total_power = 0
    for r in top_5:
        lv = max(int(r.get("roleLV", 1)), 1)
        star = max(int(r.get("roleStar", 1)), 1)
        # 与战斗记录构建器共用同一套等级/星级折算，避免两处公式漂移
        total_power += estimate_role_power(lv, star)
    actual_power = int(total_power * 1.20 * random.uniform(0.98, 1.02))
    return max(actual_power, 20000)


@register_task
class MainStageTask(BaseTask):
    """主线关卡通关主动任务（push_main_stage）。

    :param target_section: 指定推到哪一关停止（如「1-3」，也可填 sectionID）；None 表示不限。
    :param target_area: 指定推到哪一小章停止（如 "1-2"，也可填 areaID）；None 表示不限。
    :param max_stages: 本次最多推进关卡数（0 表示不限，直至阻碍或全通）。
    :param delay_min: 每关模拟战斗时间下限（秒，防倍速风控）。
    :param delay_max: 每关模拟战斗时间上限（秒，防倍速风控）。
    """

    name = "push_main_stage"

    #: 需要登录态（必须携带 1004 会话令牌）
    requires_auth = True

    #: 绝不花钻石：绝不调用买体力协议
    spends_diamond = False

    #: 任务提示
    opt_in_hint = "主线推图：自动推进常规关卡并领取章节满星宝箱；遇到敏感校验关卡或体力不足时自动停止"

    def __init__(
        self,
        session: GameSession,
        *,
        logger: logging.Logger | None = None,
        dry_run: bool = False,
        spend_policy: SpendPolicy | None = None,
        target_section: int | None = None,
        target_area: str | None = None,
        max_stages: int = 0,
        delay_min: float | None = None,
        delay_max: float | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            session, logger=logger, dry_run=dry_run, spend_policy=spend_policy
        )
        self.target_section = int(target_section) if target_section is not None else None
        self.target_area = str(target_area).strip() if target_area is not None else None
        self.max_stages = max(int(max_stages), 0)
        self.delay_min = float(delay_min) if delay_min is not None else None
        self.delay_max = float(delay_max) if delay_max is not None else None

        #: 本次实际发出的消息号清单（测试用）
        self.sent_packet_ids: list[int] = []
        #: 当前可用体力
        self.energy_left: int | None = None

    # ------------------------------------------------------------------
    # 发包守卫
    # ------------------------------------------------------------------
    def _guarded_send(
        self,
        game: GameClient,
        packet: ProtoMessage,
        expect: type[ProtoMessage] | None = None,
    ) -> EnvelopeView:
        """通过白名单守卫发送协议包，拦截任何非法包或买体力包。"""
        return guarded_send(
            game,
            packet,
            expect,
            allowed=ALLOWED_PACKET_IDS,
            forbidden=FORBIDDEN_PACKET_IDS,
            record=self.sent_packet_ids,
            purpose=PACKET_PURPOSE,
            logger=self.logger,
            include_defaults=True,
        )

    # ------------------------------------------------------------------
    # 协议交互
    # ------------------------------------------------------------------
    def _fetch_last_section(self, game: GameClient) -> GetLastSectionRes:
        """11011 空包获取玩家当前边界状态。"""
        view = self._guarded_send(game, GetLastSectionPacket(), GetLastSectionRes)
        return view.decode_sub_packet(GetLastSectionRes)

    def _fetch_energy(self, game: GameClient) -> int:
        """2007 空包获取当前背包体力。"""
        view = self._guarded_send(game, GetAllItemPacket(), GetAllItemRes)
        res = view.decode_sub_packet(GetAllItemRes)
        return res.stamina()

    def _claim_area_star_chests(
        self, game: GameClient, area_id: int, area_name: str = ""
    ) -> list[str]:
        """尝试领取一个小章节的全部星数宝箱（循环最多 3 次）。"""
        claimed: list[str] = []
        for i in range(1, 4):
            view = self._guarded_send(
                game,
                ReceiveStarRewardPacket(areaID=area_id),
                ReceiveStarRewardRes,
            )
            res = view.decode_sub_packet(ReceiveStarRewardRes)
            if not res.is_success() or not res.rewardList:
                break
            # 11016 的数量口径尚未实证 → 保守口径只报件数
            # （见 models/reward.py 的 QuantityScope.UNKNOWN）。
            reward_str = (
                reward_phrase(res.rewardList, scope=QuantityScope.UNKNOWN) or "<空>"
            )
            desc = f"宝箱{i}（{reward_str}）"
            claimed.append(desc)
            self.logger.info(
                "✔ 领取小章节「%s」满星宝箱成功：%s", area_name or area_id, desc
            )
        return claimed

    def _fight_once(
        self, game: GameClient, section: MainStageSection
    ) -> tuple[bool, FightingResultRes | None, str]:
        """执行单关卡战斗流程：申请开战 → 模拟耗时 → 上报三星结果。

        :return: (是否成功, 结算响应, 错误或奖励说明)
        """
        # 0. 敏感校验关卡防御熔断（原则 2）：严禁向 cheatCheck=1 关卡发任何战斗包
        if section.is_cheat_check_required:
            msg = (
                f"遇到敏感校验关卡 {section.friendly_name}（cheatCheck=1）！"
                "本工具严格遵守风控安全红线，严禁发出任何战斗请求包，已触发安全熔断停机。"
            )
            self.logger.warning(msg)
            return False, None, msg

        # 确保出战队伍设定为 PVE1 队伍（5013）
        try:
            self._guarded_send(game, SetUseTeamIDPacket(teamID=TEAM_TYPE_PVE1), SetUseTeamIDRes)
        except Exception as exc:
            self.logger.debug("设置出战队伍 5013 响应异常（非致命）: %s", exc)

        # 1. 申请进入战斗
        req = FightingRequestPacket(sectionID=section.section_id, teamID=TEAM_TYPE_PVE1)
        req_view = self._guarded_send(game, req, FightingResponseRes)
        req_res = req_view.decode_sub_packet(FightingResponseRes)

        if not req_res.is_success() or req_res.fightToken == 0:
            return (
                False,
                None,
                f"关卡 {section.friendly_name} 开战请求失败：errorCode={req_res.errorCode}",
            )

        fight_token = req_res.fightToken

        # 2. 模拟真实战斗耗时（防止 0 秒倍速被封号）
        #    动态分阶拟真耗时模型：1 波怪 16~25s、2 波怪 24~35s、3 波怪 35~50s、BOSS关 55~75s
        if self.delay_min is not None and self.delay_max is not None and self.delay_min == self.delay_max:
            # 兼容单测：允许 delay_min == delay_max 传入固定模拟耗时
            time_cost_sec = int(self.delay_min)
        else:
            if section.is_last_in_area:
                time_cost_sec = random.randint(55, 75)
            elif section.suggest_cp >= 40000:
                time_cost_sec = random.randint(35, 50)
            elif section.suggest_cp >= 20000:
                time_cost_sec = random.randint(24, 35)
            else:
                time_cost_sec = random.randint(16, 25)

        self.logger.info(
            "关卡 %s 战斗进行中，模拟战斗耗时 %d 秒...",
            section.friendly_name,
            time_cost_sec,
        )
        cancellation.sleep_interruptible(time_cost_sec)

        # 3. 计算签名与存证
        verify_token = make_fight_verify_token(fight_token, time_cost_sec, win_lose=1)
        server_verify = make_battle_server_verify(section.section_id)

        # 严格依原则 1：出战战力必须来源于当前登录账号实际获得的角色数据（严禁使用 suggest_cp 捏造）
        account_roles = getattr(getattr(self.session, "game_state", None), "roles", ())
        fighting_power = resolve_account_team_power(account_roles)

        # 拟真伤害总量：根据真实战力与波次拟合
        damage_scale = random.uniform(0.20, 0.35) if section.is_last_in_area else random.uniform(0.08, 0.18)
        damage = max(int(fighting_power * damage_scale), random.randint(8000, 25000))
        combo_count = random.randint(18, 38)
        kill_boss = 1 if section.is_last_in_area else 0

        # 4. 上报战斗结算
        roles = getattr(getattr(self.session, "game_state", None), "roles", ())
        my_team_records = build_my_team_records(
            roles=roles,
            total_damage=damage,
            team_power=fighting_power,
        )
        enemy_team_records = build_enemy_team_records(
            enemy_power=int(fighting_power * 0.8),
            is_pvp=False,
            opponent_lv=max(int(section.section_id % 100), 20),
        )
        damage_content = f"t={time_cost_sec}$MainStage$EfRe:62$ElemAmp:0.3$BlAmRd:{random.randint(3000, 5000)}"

        result_packet = FightingResultPacket(
            fightData=FightDataClass(
                token=fight_token,
                sectionId=section.section_id,
            ),
            winLose=1,
            starCount=[1, 1, 1],
            timeCostSec=time_cost_sec,
            ComboRecord=BattleAchiComboClass(totalComboCount=combo_count, killBossCount=kill_boss),
            InterupRecord=BattleAchiInterupClass(totalInterupCount=random.randint(1, 3)),
            AwakeRecord=BattleAchiAwakeClass(totalInterupCount=random.randint(0, 2)),
            battleResult=BattleResultClass(
                teamId=TEAM_TYPE_PVE1,
                fightingPower=fighting_power,
                damageContent=damage_content,
                damage=damage,
                yimoLv=-1,
                myTeamRecord=my_team_records,
                enemyTeamRecord=enemy_team_records,
                battleServerVerify=server_verify,
            ),
            token=verify_token,
        )

        res_view = self._guarded_send(game, result_packet, FightingResultRes)
        result_res = res_view.decode_sub_packet(FightingResultRes)

        if not result_res.is_success():
            return (
                False,
                result_res,
                f"关卡 {section.friendly_name} 战斗结算失败：errorCode={result_res.errorCode}",
            )

        # 5. 补齐真机 97025 (PVPVerifyPacket) 异步遥测审计包（镜像真机生命周期）
        try:
            self._send_verify_telemetry(
                game,
                fight_token=fight_token,
                section_id=section.section_id,
                time_cost_sec=time_cost_sec,
                fighting_power=fighting_power,
            )
        except Exception as exc:
            self.logger.debug("97025 遥测存证上报被捕获（非致命）: %s", exc)

        # 更新剩余体力
        if result_res.energy_left is not None:
            self.energy_left = result_res.energy_left

        reward_str = (
            reward_phrase(result_res.rewardList, scope=QuantityScope.UNKNOWN)
            or "<无额外掉落>"
        )
        return True, result_res, reward_str

    def _send_verify_telemetry(
        self,
        game: GameClient,
        *,
        fight_token: int,
        section_id: int,
        time_cost_sec: int,
        fighting_power: int,
    ) -> None:
        """镜像官方客户端：结算后异步上报 97025 存证遥测包。"""
        if not self.dry_run and (self.delay_min is None or self.delay_min > 1.0):
            cancellation.sleep_interruptible(random.uniform(0.3, 0.6))

        player_id = int(getattr(getattr(self.session, "game_state", None), "player_id", 0) or 0)
        verify_packet = PVPVerifyPacket(
            battleToken=fight_token,
            reportSid=-1,
            winOrLose=1,
            atkTeamPlayId=player_id if player_id > 0 else -1,
            defTeamPlayId=-1,
            battleType=10,
            message=json.dumps({"sectionId": section_id, "battleTime": time_cost_sec}),
            sectionID=section_id,
            fightRecord=json.dumps({"teamId": TEAM_TYPE_PVE1, "fightingPower": fighting_power}),
            fightData=json.dumps({"battleTime": time_cost_sec}),
        )
        self._guarded_send(game, verify_packet, expect=None)

    # ------------------------------------------------------------------
    # 目标解析
    # ------------------------------------------------------------------
    def _resolve_target_section(
        self,
        sec_map: dict[int, MainStageSection],
        area_map: dict[int, MainStageArea],
    ) -> tuple[int | None, str | None]:
        """解析目标关卡或目标章节。

        :return: (target_section_id, 说明文字)
        """
        if self.target_section is not None:
            if self.target_section not in sec_map:
                return None, f"指定的目标关卡 {self.target_section} 不在主线关卡表中"
            sec = sec_map[self.target_section]
            return self.target_section, f"目标关卡：{sec.friendly_name}"

        if self.target_area is not None:
            # 按 area_id 或名称前缀/子串匹配
            matched_area: MainStageArea | None = None
            if self.target_area.isdigit():
                aid = int(self.target_area)
                matched_area = area_map.get(aid)
            if matched_area is None:
                for a in area_map.values():
                    if (
                        self.target_area in a.area_name
                        or self.target_area == a.area_name.split()[0]
                    ):
                        matched_area = a
                        break
            if matched_area is None:
                return None, f"未能匹配到目标章节「{self.target_area}」"
            last_sec_id = matched_area.section_ids[-1]
            last_sec = sec_map.get(last_sec_id)
            last_desc = last_sec.friendly_name if last_sec else str(last_sec_id)
            return last_sec_id, f"目标章节：{matched_area.area_name}（推至最后一关 {last_desc}）"

        return None, None

    # ------------------------------------------------------------------
    # 执行流程
    # ------------------------------------------------------------------
    def execute(self) -> TaskResult:
        with self.open_game_client() as game:
            if self.dry_run:
                return self._dry_run(game)

            sec_map, area_map, ordered_ids = load_main_stage_index()

            # 1. 查询当前体力
            try:
                self.energy_left = self._fetch_energy(game)
                self.logger.info("当前背包体力：%d", self.energy_left)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("查询背包体力异常（将按结算回传为准）：%s", exc)

            # 2. 查询玩家当前通关进度
            last_res = self._fetch_last_section(game)
            progress = resolve_main_stage_progress(
                last_res.sectionStateList, sec_map, ordered_ids
            )
            highest_passed_id = progress["highest_passed_id"]
            highest_passed_desc = progress["highest_passed_desc"]
            next_sec_id = progress["next_section_id"]
            self.logger.info("玩家当前已通关最高主线：%s（下一待推关卡：%s）", highest_passed_desc, progress["next_section_desc"])

            if next_sec_id is None or next_sec_id not in sec_map:
                return TaskResult(
                    task=self.name,
                    ok=True,
                    message=f"恭喜！所有主线关卡已全部通关，当前已通关最高主线：{highest_passed_desc}。",
                    data={
                        "highest_passed_id": highest_passed_id,
                        "highest_passed_desc": highest_passed_desc,
                        "cleared_stages": [],
                    },
                )

            # 3. 目标关卡校验
            target_sec_id, target_desc = self._resolve_target_section(sec_map, area_map)
            if target_desc:
                self.logger.info("设定推图目标：%s", target_desc)
            if target_sec_id is not None and highest_passed_id is not None:
                target_idx = ordered_ids.index(target_sec_id)
                highest_idx = ordered_ids.index(highest_passed_id)
                if target_idx <= highest_idx:
                    return TaskResult(
                        task=self.name,
                        ok=True,
                        message=f"{target_desc} 已经通关，无需重复推进。当前已通关最高主线：{highest_passed_desc}。",
                        data={
                            "highest_passed_id": highest_passed_id,
                            "highest_passed_desc": highest_passed_desc,
                            "cleared_stages": [],
                        },
                    )

            # 4. 推进循环
            current_sec = sec_map[next_sec_id]
            cleared_stages: list[int] = []
            claimed_chests: list[str] = []
            stop_reason: str = ""

            while True:
                # ★ 2026-10-04：每一关都是一个安全的中止点。用户点了「停止执行」
                # 之后，最多再打完手上的这一关就停 —— 这与"敏感关卡/体力不足"
                # 一样属于"平稳停机"，不做任何补偿动作。
                cancellation.raise_if_cancelled()

                # 检查最大推关限制
                if self.max_stages > 0 and len(cleared_stages) >= self.max_stages:
                    stop_reason = f"已达成单次最大推图关卡数上限（{self.max_stages} 关）"
                    break

                # 检查敏感关卡校验（硬安全护栏：遇 cheatCheck=1 必须停机）
                if current_sec.is_cheat_check_required:
                    stop_reason = (
                        f"遇到敏感校验关卡 {current_sec.friendly_name}（cheatCheck=1）！"
                        "本工具严格遵守风控安全规范，不提供自动绕过，请在游戏客户端手动通关后再运行。"
                    )
                    self.logger.warning(stop_reason)
                    break

                # 检查体力是否充足
                if (
                    self.energy_left is not None
                    and self.energy_left < current_sec.energy_cost
                ):
                    stop_reason = (
                        f"体力不足（当前剩余 {self.energy_left}，本关需要 {current_sec.energy_cost}）。"
                        "主线推图不主动购买体力，已平稳停机。"
                    )
                    self.logger.info(stop_reason)
                    break

                # 执行战斗
                self.logger.info(
                    "▶ 开始推进关卡：%s（消耗体力 %d）",
                    current_sec.friendly_name,
                    current_sec.energy_cost,
                )
                ok, res, detail = self._fight_once(game, current_sec)
                if not ok:
                    stop_reason = f"关卡通关失败：{detail}"
                    self.logger.error(stop_reason)
                    break

                cleared_stages.append(current_sec.section_id)
                # ★ 2026-10-18：逐关结果走 progress（网页「运行结果」实时逐条显示），
                # 不再是"整轮推完只看到一行汇总"。见 tasks/base.py::progress。
                self.progress(
                    "✔ 关卡 %s 通关成功（三星）！剩余体力：%s，掉落：%s",
                    current_sec.friendly_name,
                    self.energy_left,
                    detail,
                )

                # 检查是否完成了小章节最后一关
                if current_sec.is_last_in_area:
                    self.logger.info(
                        "🎉 小章节「%s」已全关卡满星通关，正在领取章节满星宝箱...",
                        current_sec.area_name,
                    )
                    chests = self._claim_area_star_chests(
                        game, current_sec.area_id, current_sec.area_name
                    )
                    claimed_chests.extend(chests)

                # 检查是否达成目标关卡
                if target_sec_id is not None and current_sec.section_id == target_sec_id:
                    stop_reason = f"已成功达成目标关卡：{current_sec.friendly_name}"
                    break

                # 下一关定位
                if current_sec.next_section_id is None:
                    stop_reason = "恭喜！全部主线关卡已全部通关！"
                    break

                current_sec = sec_map[current_sec.next_section_id]

            # 汇总结果
            final_highest_id = (
                cleared_stages[-1] if cleared_stages else highest_passed_id
            )
            final_highest_desc = (
                sec_map[final_highest_id].friendly_name
                if final_highest_id and final_highest_id in sec_map
                else "尚未通关任何主线关卡"
            )
            final_next_sec_id = (
                sec_map[final_highest_id].next_section_id
                if final_highest_id and final_highest_id in sec_map
                else None
            )
            final_next_desc = (
                sec_map[final_next_sec_id].friendly_name
                if (final_next_sec_id and final_next_sec_id in sec_map)
                else "已全部通关"
            )
            summary_msg = (
                f"当前已通关最高主线：{final_highest_desc}。"
                f"本次成功通关 {len(cleared_stages)} 个主线关卡"
                f"{f'，领取 {len(claimed_chests)} 个满星宝箱' if claimed_chests else ''}。"
            )
            if stop_reason:
                summary_msg += f" 停机原因：{stop_reason}"

            return TaskResult(
                task=self.name,
                ok=True,
                message=summary_msg,
                data={
                    "highest_passed_id": final_highest_id,
                    "highest_passed_desc": final_highest_desc,
                    "next_section_id": final_next_sec_id,
                    "next_section_desc": final_next_desc,
                    "cleared_count": len(cleared_stages),
                    "cleared_stages": cleared_stages,
                    # 只留计数：逐条"宝箱N（…）"文本已由 message 承载，
                    # data 不再重复一份（2026-10-04「data 一并收敛」）
                    "claimed_chest_count": len(claimed_chests),
                    "stop_reason": stop_reason,
                    "energy_left": self.energy_left,
                    "sent_packets": tuple(self.sent_packet_ids),
                },
            )

    # ------------------------------------------------------------------
    # 演练模式（Dry-Run）
    # ------------------------------------------------------------------
    def _dry_run(self, game: GameClient) -> TaskResult:
        sec_map, area_map, ordered_ids = load_main_stage_index()
        cheat_check_count = sum(1 for s in sec_map.values() if s.is_cheat_check_required)
        normal_count = len(sec_map) - cheat_check_count

        print("=" * 72)
        print("【主线关卡通关主动任务 · 演练模式（不发任何网络请求）】")
        print("=" * 72)
        print(f"主线关卡总数：{len(sec_map)} 关")
        print(f"小章节总数：  {len(area_map)} 章")
        print(f"常规关卡数：  {normal_count} 关（cheatCheck=0，无客户端校验）")
        print(f"敏感校验关卡：{cheat_check_count} 关（cheatCheck=1，遇上必须真机手动通关）")
        print("当前最高关卡：演练模式不发包（正式运行将自动查询 11011 并从其下一关开始推进）")
        print("拟真耗时：    自动分阶拟真（16~75 秒/关）")
        print(f"最大推关限制：{self.max_stages if self.max_stages > 0 else '不限制'}")

        target_id, target_desc = self._resolve_target_section(sec_map, area_map)
        if target_desc:
            print(f"指定目标：    {target_desc}")

        # 示例组装：展示 11011、11015、11017、11007
        p_last = game.build(GetLastSectionPacket(), include_defaults=True)
        p_req = game.build(
            FightingRequestPacket(sectionID=ordered_ids[0], teamID=1),
            include_defaults=True,
        )
        sample_token = make_fight_verify_token(123456, 15, 1)
        sample_verify = make_battle_server_verify(ordered_ids[0])
        p_res = game.build(
            FightingResultPacket(
                fightData=FightDataClass(token=123456, sectionId=ordered_ids[0]),
                winLose=1,
                starCount=[1, 1, 1],
                timeCostSec=15,
                battleResult=BattleResultClass(
                    teamId=1,
                    battleServerVerify=sample_verify,
                ),
                token=sample_token,
            ),
            include_defaults=True,
        )
        p_chest = game.build(
            ReceiveStarRewardPacket(areaID=list(area_map.keys())[0]),
            include_defaults=True,
        )

        print("\n【协议请求报文预演】")
        print(f"1. 11011 GetLastSectionPacket:    {len(p_last.body)} 字节")
        print(f"2. 11015 FightingRequestPacket:    {len(p_req.body)} 字节")
        print(f"3. 11017 FightingResultPacket:     {len(p_res.body)} 字节 (含 MD5 签名 + Gzip 存证)")
        print(f"4. 11007 ReceiveStarRewardPacket:  {len(p_chest.body)} 字节")
        print("=" * 72)

        return TaskResult(
            task=self.name,
            ok=True,
            message=(
                f"演练完成：主线共 {len(sec_map)} 关（常规 {normal_count} 关，"
                f"敏感校验 {cheat_check_count} 关），协议报文结构验证通过"
            ),
            data={
                "total_stages": len(sec_map),
                "normal_stages": normal_count,
                "cheat_check_stages": cheat_check_count,
                "sample_verify_len": len(sample_verify),
            },
        )


@register_task
class MainStageStatusTask(BaseTask):
    """主线关卡进度只读查询任务（main_stage_status）。

    只发 11011 获取当前已通关主线关卡状态并解析最高已通关主线，不消耗任何体力或钻石。
    """

    name = "main_stage_status"
    requires_auth = True
    spends_diamond = False
    opt_in_hint = "主线进度查询：只读拉取当前最高已通关关卡与下一待攻打关卡（零消耗）"

    def __init__(
        self,
        session: GameSession,
        *,
        logger: logging.Logger | None = None,
        dry_run: bool = False,
        spend_policy: SpendPolicy | None = None,
    ) -> None:
        super().__init__(
            session, logger=logger, dry_run=dry_run, spend_policy=spend_policy
        )
        self.sent_packet_ids: list[int] = []

    def execute(self) -> TaskResult:
        with self.open_game_client() as game:
            if self.dry_run:
                return TaskResult(
                    task=self.name,
                    ok=True,
                    message="主线进度（演练）：只读拉取 11011，已演练报文结构。",
                    data={
                        "highest_passed_id": None,
                        "highest_passed_desc": "演练模式（不发包）",
                        "next_section_id": 102000001,
                        "next_section_desc": "1-1 命運的相遇 102000001",
                        "is_all_cleared": False,
                    },
                )

            view = guarded_send(
                game,
                GetLastSectionPacket(),
                GetLastSectionRes,
                allowed=ALLOWED_PACKET_IDS,
                forbidden=FORBIDDEN_PACKET_IDS,
                record=self.sent_packet_ids,
                purpose=PACKET_PURPOSE,
                logger=self.logger,
                include_defaults=True,
            )
            res = view.decode_sub_packet(GetLastSectionRes)
            sec_map, _, ordered_ids = load_main_stage_index()
            progress = resolve_main_stage_progress(
                res.sectionStateList, sec_map, ordered_ids
            )
            msg = f"当前已通关最高主线：{progress['highest_passed_desc']}（下一关：{progress['next_section_desc']}）"
            self.logger.info(msg)
            return TaskResult(
                task=self.name,
                ok=True,
                message=msg,
                data=progress,
            )

