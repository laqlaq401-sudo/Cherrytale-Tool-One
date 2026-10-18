"""每日任务自动执行：补齐六个缺口动作 + 统一领奖 + 缺口报告。

【这个任务做什么（2026-10-16 用户拍板，方案见 notes/daily_task_auto_plan.md）】
线上「日常任务」共 18 条，其中 7 条已由现有独立任务负责
（登录 / 免费召唤 / 市集购买 / 宴席 / 素材关卡×5 / 通关×5 / 消耗体力）——
**一条都不碰**（避免与独立任务重复执行、抢消耗）。本任务只补 6 个缺口动作：

1. **英雄等级提升 1 次** —— 挑"等级最低的 SSR"，喂经验书**只升 1 级**；
2. **英雄技能升级 1 次** —— 同一个英雄，"品质达标且最便宜"的技能位 +1 级；
3. **装备强化 1 次** —— 挑"强化等级最低"的装备强化一次（消耗魔尘+金币）；
4. **英雄赠礼 1 次** —— 同一个英雄，送背包里最低阶的礼物 1 个；
5. **好友赠礼（全送）3 次** —— 游戏「一键全送」（无成本）；
   「领取」是另一个独立任务 ``friend_gift_claim``，两者用同一个 10009 报文；
6. **契约拼图抽取 1 次** —— 挑"可抽取（NewOpen）"的契约区段抽 1 次（消耗 CP）。

流程：拉 6001 → 先一键领掉已达标项 → 逐个补缺口（带门槛与降级，失败跳过并记录）
→ 再一键领奖 → 复查 → 输出"补做 / 领取 / 缺口"三行报告。

【真机实证（2026-10-16，全部核对过，细节见方案文档）】
- 经验口径：升到下一级所需 = ``2 × expSum[LV] − roleExp``（两样本互证）；
- 技能：``skillPlace`` = 技能位序号（1 起）、``targetLV`` = 目标绝对等级；
  技能位有**品质门槛**（``SkillOpenStateData``），未达标服务端回 ``-4``；
- 领奖：``DMReward(misID=-1)`` 一键结算（"无待领→-1"与"真领到→0"两条路径均已实测）。

【纪律】
- 全部是**副作用包**：超时不重试；单项失败跳过继续（最后汇总），不中断整条链；
- 动作之间有间隔与取消检查点（``cancellation.sleep_interruptible``）；
- 钻石零参与（本任务没有任何花钻石的路径）；现成 7 条的动作一律不在这里执行。
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Mapping, Sequence
from typing import Final

from client.exceptions import CherrytaleError
from client.game_client import GameClient
from client.session import GameSession
from models import equip as _equip_mod
from models import friend as _friend_mod
from models import present as _present_mod
from models import role as _role_mod
from models import secret as _secret_mod

# 请求/响应模型（逐类显式 import，便于测试与读者对照抓包）
from models.daily import ItemClass
from models.equip import (
    GetAllEquipPacket,
    GetAllEquipRes,
    StrengthenEquipPacket,
    StrengthenEquipRes,
)
from models.friend import (
    ALL_GIFT_TARGET,
    OPERATION_SEND_ALL_GIFT,
    FriendOperationPacket,
    FriendOperationRes,
    GetFriendListPacket,
    GetFriendListRes,
)
from models.present import GIFT_ITEM_IDS, UsePresentPacket, UsePresentRes
from models.role import (
    GetAllRolePacket,
    GetAllRoleRes,
    RoleClass,
    UpgradeSkillPacket,
    UpgradeSkillRes,
    UpRoleExpPacket,
    UpRoleExpRes,
)
from models.secret import (
    GetSecretSectionPacket,
    GetSecretSectionRes,
    SweepSectionPacket,
    SweepSectionRes,
)
from models.wallet import GetAllItemPacket, GetAllItemRes
from services import cancellation
from tasks.base import BaseTask, TaskResult, register_task
from tasks.daily_box import claim_one_key, fetch_daily_state, plan_pending_boxes
from tasks.packet_guard import guarded_send

_LOGGER: Final[logging.Logger] = logging.getLogger("tasks.daily_task_auto")

#: 六个动作分别对应的日常任务 ID（``DailyMissionData.dmID``）
MISSION_HERO_LEVEL: Final[int] = 249000044  # 聚沙成塔：英雄等级提升 1 次
MISSION_SKILL_UP: Final[int] = 249000045  # 自我鍛鍊：英雄技能等级提升 1 次
MISSION_EQUIP_STRENGTHEN: Final[int] = 249000046  # 一日一強化：装备强化 1 次
MISSION_HERO_GIFT: Final[int] = 249000047  # 濃情愛意：英雄赠礼 1 次
MISSION_CONTRACT_DRAW: Final[int] = 249000048  # 窺探契約：抽取契约拼图 1 次
MISSION_FRIEND_GIFT: Final[int] = 249000051  # 禮尚往來：好友赠礼 3 次

#: 经验书道具 ID（数值从 ``ExpBooksData`` 读，这里只是"哪些 ID 算经验书"的白名单）
EXP_BOOK_ITEM_IDS: Final[tuple[int, ...]] = (
    200000003,
    200000004,
    200000005,
    200000006,
    200000007,
    200000008,
    200000009,
    200000010,
)

#: 技能位序号（1 起）→ 中文名（真机实证的顺序，与 ``SkillClass.LEVEL_FIELDS`` 同序）
SLOT_NAMES: Final[dict[int, str]] = {
    1: "主动技",
    2: "循环技",
    3: "被动技1",
    4: "被动技2",
    5: "觉醒技",
}

#: 角色等级上限（``RoleExpData`` 到 50 为止）
MAX_ROLE_LEVEL: Final[int] = 50

#: 动作之间的间隔（秒）——模拟真人节奏；可被"停止执行"打断
ACTION_INTERVAL_SECONDS: Final[float] = 1.2

#: "自动顺延"上限（2026-10-16 用户拍板 B）：各英雄向动作从"等级最低的 SSR"起顺序取用，
#: **静态跳过**算得出不可行的候选（不计次），向服务端**最多真发**这么多次。
MAX_FALLBACK_ATTEMPTS: Final[int] = 3

#: 发包白名单 = 各模型模块白名单的**并集**（唯一来源在模型层，别在这里抄一份）
ALLOWED_PACKET_IDS: Final[dict[str, int]] = {
    **_role_mod.ALLOWED_PACKET_IDS,  # 5011 / 4001 / 5003
    **_equip_mod.ALLOWED_PACKET_IDS,  # 3003 / 3005
    **_present_mod.ALLOWED_PACKET_IDS,  # 5015
    **_friend_mod.ALLOWED_PACKET_IDS,  # 10001 / 10009
    **_secret_mod.ALLOWED_PACKET_IDS,  # 11029 / 11009
    "GetAllItemPacket": 2007,  # 读背包（经验书/礼物从哪来）
}

#: 禁发清单 = 各模块禁发清单的并集（命中即抛错，绝不发送）
FORBIDDEN_PACKET_IDS: Final[dict[str, int]] = {
    **_role_mod.FORBIDDEN_PACKET_IDS,
    **_equip_mod.FORBIDDEN_PACKET_IDS,
    **_present_mod.FORBIDDEN_PACKET_IDS,
    **_friend_mod.FORBIDDEN_PACKET_IDS,
    **_secret_mod.FORBIDDEN_PACKET_IDS,
}

#: ``{消息号: 说明}``（DEBUG 日志与演练输出用）
PACKET_PURPOSE: Final[dict[int, str]] = {
    **_role_mod.PACKET_PURPOSE,
    **_equip_mod.PACKET_PURPOSE,
    **_present_mod.PACKET_PURPOSE,
    **_friend_mod.PACKET_PURPOSE,
    **_secret_mod.PACKET_PURPOSE,
    2007: "拉取背包全量列表（只读；经验书/礼物都在里面）",
    11009: "契约拼图抽取（消耗 CP，按步阶 5~20）",
    11010: "契约拼图抽取响应",
}


# ---------------------------------------------------------------------------
# 纯函数：挑书 / 挑技能位（放在模块级是为了可单测、可离线演练）
# ---------------------------------------------------------------------------
def pick_books(
    books: Sequence[tuple[int, int, int, int]], need: int
) -> tuple[list[tuple[int, int, int]], int] | None:
    """在"每本书带背包实例"的列表里挑一组总经验 ≥ ``need`` 的经验书。

    :param books: ``[(itemSid, itemID, 单本经验, 库存数量), ...]``
    :param need: 还差的经验（必须 > 0）
    :return: ``([(itemSid, itemID, 本数), ...], 总经验)``；
        库存怎么都凑不够时返回 ``None``。

    【策略与"不越级"的关系】
    大步优先（大书向下取整 → 少发条目），随后按从小到大补齐尾差 ——
    尾差溢出最多 = 一本最小书的经验。调用方（``_step_hero_level_up``）负责
    检查溢出是否可能触发"一次升多级"，超出就整个跳过（宁可不做，不越级）。
    """
    remaining = int(need)
    if remaining <= 0:
        return None
    info = {sid: (iid, value, amount) for sid, iid, value, amount in books}
    picked: dict[int, int] = {}

    # ① 大步优先：从大到小向下取整
    for sid, iid, value, amount in sorted(books, key=lambda b: b[2], reverse=True):
        if remaining <= 0:
            break
        if value <= 0:
            continue
        take = min(amount, remaining // value)
        if take > 0:
            picked[sid] = picked.get(sid, 0) + take
            remaining -= take * value

    # ② 尾差：从小到大补齐（允许最后一本溢出）
    if remaining > 0:
        for sid, iid, value, amount in sorted(books, key=lambda b: b[2]):
            if remaining <= 0:
                break
            if value <= 0:
                continue
            room = amount - picked.get(sid, 0)
            if room <= 0:
                continue
            take = min(room, math.ceil(remaining / value))
            picked[sid] = picked.get(sid, 0) + take
            remaining -= take * value

    if remaining > 0:
        return None

    picks = [(sid, info[sid][0], count) for sid, count in picked.items()]
    total = sum(info[sid][1] * count for sid, count in picked.items())
    return picks, int(total)


def choose_skill_slot(
    levels: Sequence[int],
    costs_by_level: Mapping[int, object],
    requirement: Sequence[int],
    quality: int,
) -> tuple[int, int] | None:
    """挑"品质达标且最便宜"的技能位。

    :param levels: 五个技能位的当前等级（顺序 = ``SkillClass.LEVEL_FIELDS``）
    :param costs_by_level: ``{当前等级: SkillUpgradeCost}``（来自配置表）
    :param requirement: 五个技能位的品质门槛（``RoleSkillGates.requirement``）
    :param quality: 英雄当前品质
    :return: ``(技能位序号 1 起, 当前等级)``；没有可升的位时返回 ``None``。
    """
    best: tuple[tuple[int, int], int, int] | None = None
    for index, level in enumerate(levels):
        place = index + 1
        if place > len(requirement):
            break
        if int(quality) < int(requirement[index]):
            continue
        row = costs_by_level.get(int(level))
        if row is None:
            continue
        cost = int(row.cost_of(place))  # type: ignore[attr-defined]
        key = (cost, place)
        if best is None or key < best[0]:
            best = (key, place, int(level))
    if best is None:
        return None
    return best[1], best[2]


@register_task
class DailyTaskAutoTask(BaseTask):
    """每日任务自动执行（补 6 个缺口动作 + 统一领奖 + 缺口报告）。"""

    name: Final[str] = "daily_task_auto"

    #: 要带令牌与 playerId 才能发包
    requires_auth: Final[bool] = True

    #: 全部动作零钻石（不碰宴席/市集/购买类）
    spends_diamond: Final[bool] = False

    def __init__(
        self,
        session: GameSession,
        *,
        logger: logging.Logger | None = None,
        dry_run: bool = False,
        spend_policy: object | None = None,
        run_options: object | None = None,
    ) -> None:
        super().__init__(
            session,
            logger=logger,
            dry_run=dry_run,
            spend_policy=spend_policy,  # type: ignore[arg-type]
            run_options=run_options,  # type: ignore[arg-type]
        )
        #: 本次实际发出的消息号（测试用它断言"没有越界发包"）
        self.sent_packet_ids: list[int] = []

    # ------------------------------------------------------------------
    # 发送出口（白名单 + 记录）
    # ------------------------------------------------------------------
    def _send(
        self,
        game: GameClient,
        packet: object,
        expect: type,
        *,
        include_defaults: bool = False,
    ) -> object:
        """本任务唯一的发包出口（白名单校验 + 消息号记录）。"""
        return guarded_send(
            game,
            packet,  # type: ignore[arg-type]
            expect,
            allowed=ALLOWED_PACKET_IDS,
            forbidden=FORBIDDEN_PACKET_IDS,
            record=self.sent_packet_ids,
            purpose=PACKET_PURPOSE,
            logger=self.logger,
            include_defaults=include_defaults,
        )

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def execute(self) -> TaskResult:
        """执行主体（见模块文档的流程）。"""
        with self.open_game_client() as game:
            if self.dry_run:
                return self._dry_run(game)
            return self._run(game)

    def _run(self, game: GameClient) -> TaskResult:
        from models.game_config import (
            load_daily_missions,
            load_daily_reward_boxes,
            mission_index,
        )

        done: list[str] = []
        issues: list[str] = []
        claimed: list[str] = []
        already_done = 0

        # ① 拉状态 + 先一键领掉已达标项（含宝箱）
        state = fetch_daily_state(game)
        start_claims, start_failed = self._claim_all(game)
        claimed.extend(start_claims)
        issues.extend(start_failed)

        mission_conf = mission_index(load_daily_missions())

        # ② 名册 / 背包：只在"有需要的动作还没完成"时才去读（省流量）
        need_roles = any(
            not self._mission_done(mission_conf, state, mid)
            for mid in (MISSION_HERO_LEVEL, MISSION_SKILL_UP, MISSION_HERO_GIFT)
        )
        candidates: list[RoleClass] = []
        hero_error = ""
        if need_roles:
            try:
                candidates = self._pick_target_candidates(self._fetch_roles(game))
            except CherrytaleError as exc:
                hero_error = f"名册不可用（5011 失败：{exc}）"
                self.logger.warning("✘ %s", hero_error)

        need_bag = not self._mission_done(mission_conf, state, MISSION_HERO_LEVEL) or not (
            self._mission_done(mission_conf, state, MISSION_HERO_GIFT)
        )
        bag: GetAllItemRes | None = None
        bag_error = ""
        if need_bag:
            try:
                bag = self._fetch_bag(game)
            except CherrytaleError as exc:
                bag_error = f"背包不可用（2007 失败：{exc}）"
                self.logger.warning("✘ %s", bag_error)

        # ③ 六个动作（顺序 = 方案文档；失败跳过、继续后面的）
        def run_step(label: str, mission_id: int, fn: Callable[[], tuple[bool, str]]) -> None:
            nonlocal already_done
            if self._mission_done(mission_conf, state, mission_id):
                already_done += 1
                return
            cancellation.raise_if_cancelled()
            try:
                ok, text = fn()
            except CherrytaleError as exc:
                issues.append(f"{label}未完成（{exc}）")
                self.logger.warning("✘ %s 失败：%s", label, exc)
            else:
                if ok:
                    done.append(text)
                    self.logger.info("✔ %s", text)
                else:
                    issues.append(text)
                    self.logger.warning("✘ %s", text)
            cancellation.sleep_interruptible(ACTION_INTERVAL_SECONDS)

        run_step(
            "英雄升级",
            MISSION_HERO_LEVEL,
            lambda: self._step_hero_level_up(game, candidates, hero_error, bag, bag_error),
        )
        run_step(
            "技能升级",
            MISSION_SKILL_UP,
            lambda: self._step_skill_up(game, candidates, hero_error),
        )
        run_step(
            "装备强化",
            MISSION_EQUIP_STRENGTHEN,
            lambda: self._step_equip_strengthen(game),
        )
        run_step(
            "英雄赠礼",
            MISSION_HERO_GIFT,
            lambda: self._step_hero_gift(game, candidates, hero_error, bag, bag_error),
        )
        run_step(
            "好友赠礼（全送）",
            MISSION_FRIEND_GIFT,
            lambda: self._step_friend_gift(game),
        )
        run_step(
            "契约拼图抽取",
            MISSION_CONTRACT_DRAW,
            lambda: self._step_contract_draw(game),
        )

        # ④ 再一键领奖（新完成的任务奖励 + 新达标的宝箱），随后复查
        end_claims, end_failed = self._claim_all(game)
        claimed.extend(end_claims)
        issues.extend(end_failed)

        final_state = fetch_daily_state(game)
        pending_d, _pending_w, daily_point, _weekly_point = plan_pending_boxes(final_state)
        max_point = max((box.required_point for box in load_daily_reward_boxes()), default=9)

        missing: list[str] = []
        for item in final_state.dmInitList:
            conf = mission_conf.get(item.misID)
            if conf is not None and item.completedTimes < conf.required_count:
                missing.append(conf.title or "（未命名任务）")

        # ⑤ 报告
        parts: list[str] = []
        if done:
            parts.append(f"补做 {len(done)} 项：" + "；".join(done))
        if already_done:
            parts.append(f"另有 {already_done} 项今日已完成，未重复")
        if issues:
            parts.append(f"未完成 {len(issues)} 项：" + "；".join(issues))
        if claimed:
            # ★ 2026-10-18：去掉"N 项"计数 —— 一行里已经有"补做 N 项 / 未完成 N 项"，
            # 再来一个"领奖 N 项"只是让句子更长，列表本身已经说明领了什么。
            parts.append("一键领奖：" + "；".join(claimed))
        gap = f"当前日常活跃点 {daily_point}/{max_point}（宝箱待领 {len(pending_d)} 档）"
        if missing:
            shown = "、".join(missing[:6])
            more = f" 等 {len(missing)} 项" if len(missing) > 6 else ""
            gap += f"；未完成日常任务：{shown}{more}"
        else:
            gap += "；日常任务已全部完成"
        parts.append(gap)
        message = "；".join(parts) if parts else "没有可补做的动作，也没有可领的奖励"

        return TaskResult(
            task=self.name,
            ok=not issues,
            message=message,
            data={
                "done": tuple(done),
                "issues": tuple(issues),
                "already_done": already_done,
                "claimed": tuple(claimed),
                "daily_point": daily_point,
                "pending_boxes": tuple(pending_d),
                "missing": tuple(missing),
                "sent_packets": tuple(self.sent_packet_ids),
            },
        )

    # ------------------------------------------------------------------
    # 数据读取
    # ------------------------------------------------------------------
    def _claim_all(self, game: GameClient) -> tuple[list[str], list[str]]:
        """一键领取四类奖励（日常/周常任务 + 日常/周常宝箱）。"""
        from models.daily import MissionType

        claimed: list[str] = []
        failed: list[str] = []
        for mtype, label in (
            (MissionType.DAILY, "日常任务"),
            (MissionType.WEEKLY, "周常任务"),
            (MissionType.DAILY_REWARD_BOX, "日常宝箱"),
            (MissionType.WEEKLY_REWARD_BOX, "周常宝箱"),
        ):
            ok, text = claim_one_key(game, mtype)
            if ok:
                if text:
                    claimed.append(f"{label}：{text}")
                    self.logger.info("✔ 已领取 %s：%s", label, text)
            else:
                failed.append(f"{label}：{text}")
                self.logger.warning("✘ 领取失败 %s：%s", label, text)
        return claimed, failed

    def _fetch_roles(self, game: GameClient) -> list[RoleClass]:
        """5011 → 5012：拉实时名册（只读）。"""
        view = self._send(game, GetAllRolePacket(), GetAllRoleRes)
        res = view.decode_sub_packet(GetAllRoleRes)  # type: ignore[attr-defined]
        return list(res.roleList)

    def _fetch_bag(self, game: GameClient) -> GetAllItemRes:
        """2007 → 2008：拉背包全量（只读；找经验书与礼物）。"""
        view = self._send(game, GetAllItemPacket(), GetAllItemRes)
        return view.decode_sub_packet(GetAllItemRes)  # type: ignore[attr-defined]

    def _pick_target_candidates(self, roles: Sequence[RoleClass]) -> list[RoleClass]:
        """全部 SSR 按 ``(等级, roleSid)`` 升序 —— "等级最低的 SSR"在最前。

        各动作从这里**顺序取用（自动顺延）**：先跳过自己算得出不可行的候选，
        真正发包最多 :data:`MAX_FALLBACK_ATTEMPTS` 次
        （2026-10-16 用户拍板 B；动机：1 级新英雄等边缘会让"最低 SSR"做不成动作）。

        SSR 判定走配置表（``qualificationPrint ≥ 23``，含 Pick UP / Wish 变体）；
        读不到属性表的角色直接跳过（fail-closed，不乱挑）。
        """
        from models.game_config import is_ssr, load_role_attributes

        attrs = load_role_attributes()
        found: list[tuple[int, int, RoleClass]] = []
        for role in roles:
            attr = attrs.get(role.roleID)
            if attr is None or not is_ssr(attr.qualification_print):
                continue
            found.append((role.roleLV, role.roleSid, role))
        found.sort(key=lambda item: (item[0], item[1]))
        ordered = [item[2] for item in found]
        if not ordered:
            self.logger.warning("名册里没有可用的 SSR 目标")
            return []
        head = ordered[0]
        self.logger.info(
            "目标英雄候选 %d 个；首位 roleSid=%d（等级 %d，全名册最低的 SSR）",
            len(ordered),
            head.roleSid,
            head.roleLV,
        )
        return ordered

    def _pick_target_hero(self, roles: Sequence[RoleClass]) -> RoleClass | None:
        """（兼容入口）取排序后的第一个候选 —— 即"等级最低的 SSR"。"""
        ordered = self._pick_target_candidates(roles)
        return ordered[0] if ordered else None

    @staticmethod
    def _mission_done(
        mission_conf: Mapping[int, object], state: object, mission_id: int
    ) -> bool:
        """某条日常任务今天是否已完成（``completedTimes ≥ required_count``）。"""
        conf = mission_conf.get(mission_id)
        required = int(getattr(conf, "required_count", 1) or 1) if conf else 1
        for item in getattr(state, "dmInitList"):
            if item.misID == mission_id:
                return int(item.completedTimes) >= required
        return False

    # ------------------------------------------------------------------
    # 六个动作
    # ------------------------------------------------------------------
    def _step_hero_level_up(
        self,
        game: GameClient,
        candidates: Sequence[RoleClass],
        hero_error: str,
        bag: GetAllItemRes | None,
        bag_error: str,
    ) -> tuple[bool, str]:
        """喂经验书升 1 级（从"等级最低的 SSR"起自动顺延；只升一级、不越级）。"""
        from models.game_config import (
            load_exp_books,
            load_role_exp_table,
        )

        if not candidates:
            return False, f"英雄升级跳过（{hero_error or '没有可用的目标英雄'}）"
        if bag is None:
            return False, f"英雄升级跳过（{bag_error or '背包不可用'}）"

        exp_table = load_role_exp_table()
        book_values = load_exp_books()
        stock = [
            (item.itemSid, item.itemID, book_values[item.itemID], item.itemAmount)
            for item in bag.itemList
            if item.itemID in book_values and item.itemAmount > 0
        ]

        def plan(hero: RoleClass) -> tuple[UpRoleExpPacket | None, str]:
            """为某个候选算出书单；静态不可行（算得出）时返回原因。"""
            level = int(hero.roleLV)
            if level >= MAX_ROLE_LEVEL:
                return None, "候选已满级"
            current_sum = exp_table.get(level)
            next_sum = exp_table.get(level + 1)  # 仅用于"不越级"（可能是 -1 / 缺省）
            if current_sum is None or current_sum <= 0:
                return None, "经验表缺行"

            need = 2 * current_sum - int(hero.roleExp)
            if need <= 0:
                return None, "经验值异常"
            picked = pick_books(stock, need)
            if picked is None:
                return None, f"经验书不足：还差 {need} 经验"
            picks, total = picked

            # 越级保护：溢出经验不得达到"再升一级"的量，避免一次升多级
            overshoot = total - need
            if level < MAX_ROLE_LEVEL - 1 and next_sum is not None and next_sum > 0:
                allowed = 2 * (next_sum - current_sum)
                if overshoot >= allowed:
                    return None, f"经验书粒度过大：溢出 {overshoot} ≥ 下一级所需 {allowed}"

            packet = UpRoleExpPacket(
                roleSid=hero.roleSid,
                itemClass=[
                    ItemClass(itemSid=item_sid, itemID=item_id, itemAmount=count)
                    for item_sid, item_id, count in picks
                ],
            )
            return packet, f"经验书 {len(picks)} 种"

        attempts = 0
        last_reason = ""
        for index, hero in enumerate(candidates):
            packet, note = plan(hero)
            if packet is None:
                last_reason = note  # 静态跳过：不占发包次数
                continue
            if attempts >= MAX_FALLBACK_ATTEMPTS:
                break
            attempts += 1
            view = self._send(game, packet, UpRoleExpRes)
            res = view.decode_sub_packet(UpRoleExpRes)  # type: ignore[attr-defined]
            if res.errorCode != 0:
                last_reason = f"服务端 errorCode={res.errorCode}"
                self.logger.warning(
                    "✘ 英雄升级候选（roleSid=%d）被拒：%s", hero.roleSid, last_reason
                )
                continue
            new_level = (
                res.roleClass.roleLV if res.roleClass is not None else int(hero.roleLV) + 1
            )
            suffix = "；已自动顺延" if index > 0 else ""
            return True, f"英雄升级 {hero.roleLV}→{new_level} 级（{note}{suffix}）"

        tail = f"；已尝试 {attempts} 个候选" if attempts else ""
        return False, f"英雄升级跳过（{last_reason or '没有可用候选'}{tail}）"

    def _step_skill_up(
        self,
        game: GameClient,
        candidates: Sequence[RoleClass],
        hero_error: str,
    ) -> tuple[bool, str]:
        """升 1 级技能（"品质达标且最便宜"的技能位；从最低 SSR 起自动顺延）。"""
        from models.game_config import (
            load_role_attributes,
            load_role_skill_gates,
            load_skill_upgrade_costs,
        )

        if not candidates:
            return False, f"技能升级跳过（{hero_error or '没有可用的目标英雄'}）"

        gates_map = load_role_skill_gates()
        attr_map = load_role_attributes()
        costs = load_skill_upgrade_costs()

        attempts = 0
        last_reason = ""
        for index, hero in enumerate(candidates):
            # 1 级新英雄的技能尚未开放（2026-10-16 实测服务端回 -3）——静态跳过
            if int(hero.roleLV) < 2:
                last_reason = "候选为 1 级新英雄（技能未开放）"
                continue
            gates = gates_map.get(hero.roleID)
            attr = attr_map.get(hero.roleID)
            if gates is None or attr is None:
                last_reason = "配置表缺该英雄的门槛/品质行"
                continue
            chosen = choose_skill_slot(
                hero.skillsLV.levels(), costs, gates.requirement, attr.quality
            )
            if chosen is None:
                last_reason = "没有可升级的技能位"
                continue
            if attempts >= MAX_FALLBACK_ATTEMPTS:
                break
            attempts += 1
            place, level = chosen
            packet = UpgradeSkillPacket(
                roleSid=hero.roleSid, skillPlace=place, targetLV=[level + 1]
            )
            view = self._send(game, packet, UpgradeSkillRes)
            res = view.decode_sub_packet(UpgradeSkillRes)  # type: ignore[attr-defined]
            slot = SLOT_NAMES.get(place, f"技能位{place}")
            if res.errorCode == 0:
                suffix = "；已自动顺延" if index > 0 else ""
                return True, f"技能升级成功（{slot} {level}→{level + 1} 级{suffix}）"
            if res.errorCode == -4:
                last_reason = f"{slot} 品质门槛未达标（errorCode=-4）"
            else:
                last_reason = f"errorCode={res.errorCode}"
            self.logger.warning(
                "✘ 技能升级候选（roleSid=%d）被拒：%s", hero.roleSid, last_reason
            )

        tail = f"；已尝试 {attempts} 个候选" if attempts else ""
        return False, f"技能升级跳过（{last_reason or '没有可用候选'}{tail}）"

    def _step_equip_strengthen(self, game: GameClient) -> tuple[bool, str]:
        """强化 1 次"强化等级最低"的装备。"""
        view = self._send(game, GetAllEquipPacket(), GetAllEquipRes)
        res = view.decode_sub_packet(GetAllEquipRes)  # type: ignore[attr-defined]
        if not res.equipList:
            return False, "装备强化跳过（装备列表为空）"
        target = min(res.equipList, key=lambda e: (e.equipLV, e.equipSid))

        packet = StrengthenEquipPacket(equipSid=target.equipSid, count=1)
        view = self._send(game, packet, StrengthenEquipRes)
        res2 = view.decode_sub_packet(StrengthenEquipRes)  # type: ignore[attr-defined]
        if res2.errorCode == 0:
            return True, "装备强化成功（最低强化等级装备 ×1）"
        return False, f"装备强化失败（errorCode={res2.errorCode}）"

    def _step_hero_gift(
        self,
        game: GameClient,
        candidates: Sequence[RoleClass],
        hero_error: str,
        bag: GetAllItemRes | None,
        bag_error: str,
    ) -> tuple[bool, str]:
        """给目标英雄送 1 个礼物（低阶优先；从最低 SSR 起自动顺延；礼物不适用换下一个）。"""
        if not candidates:
            return False, f"英雄赠礼跳过（{hero_error or '没有可用的目标英雄'}）"
        if bag is None:
            return False, f"英雄赠礼跳过（{bag_error or '背包不可用'}）"

        owned = {item.itemID for item in bag.itemList if item.itemAmount > 0}
        gifts = [gift for gift in GIFT_ITEM_IDS if gift in owned]
        if not gifts:
            return False, "英雄赠礼跳过（背包里没有可送的礼物）"

        attempts = 0
        last_reason = ""
        for index, hero in enumerate(candidates):
            if attempts >= MAX_FALLBACK_ATTEMPTS:
                break
            for gift_id in gifts:
                if attempts >= MAX_FALLBACK_ATTEMPTS:
                    break
                attempts += 1
                packet = UsePresentPacket(
                    roleSid=hero.roleSid,
                    itemId=0,
                    useTime=0,
                    itemClass=[ItemClass(itemSid=-1, itemID=gift_id, itemAmount=1)],
                )
                # ★ 0 值字段（itemId/useTime）必须显式发送（抓包实证）
                view = self._send(game, packet, UsePresentRes, include_defaults=True)
                res = view.decode_sub_packet(UsePresentRes)  # type: ignore[attr-defined]
                if res.errorCode == 0:
                    exp_text = f"好感 +{res.addExp}" if res.addExp else "已送达"
                    suffix = "；已自动顺延" if index > 0 else ""
                    return True, f"英雄赠礼成功（{exp_text}{suffix}）"
                if res.errorCode == -3:
                    last_reason = "礼物不适用（errorCode=-3）"
                    continue  # 换下一个礼物再试
                last_reason = f"errorCode={res.errorCode}"
                break  # 非礼物问题 → 换下一个候选英雄

        # ⚠️ 这里 attempts 计的是"赠送次数"（同一候选可能换过多个礼物），别写成"个候选"
        tail = f"；已尝试 {attempts} 次赠送" if attempts else ""
        return False, f"英雄赠礼跳过（{last_reason or '没有可用候选'}{tail}）"

    def _step_friend_gift(self, game: GameClient) -> tuple[bool, str]:
        """「一键全送」（actionType=4，playerID=-1；无成本）。"""
        # 先只读好友列表（报告用；失败了不阻断发包）
        sendable: int | None = None
        try:
            view = self._send(
                game, GetFriendListPacket(actionType=0), GetFriendListRes, include_defaults=True
            )
            res = view.decode_sub_packet(GetFriendListRes)  # type: ignore[attr-defined]
            sendable = res.sendable_count()
        except CherrytaleError:
            pass

        packet = FriendOperationPacket(
            actionType=OPERATION_SEND_ALL_GIFT, playerID=ALL_GIFT_TARGET
        )
        view = self._send(game, packet, FriendOperationRes)
        res2 = view.decode_sub_packet(FriendOperationRes)  # type: ignore[attr-defined]
        if res2.errorCode != 0:
            return False, f"好友赠礼失败（errorCode={res2.errorCode}）"
        extra = f"可送好友 {sendable} 位" if sendable is not None else "已全部送出"
        return True, f"好友赠礼（一键全送）成功（{extra}）"

    def _step_contract_draw(self, game: GameClient) -> tuple[bool, str]:
        """契约拼图抽取 1 次（挑 NewOpen 的区段、`times=1`）。"""
        from models.game_config import load_secret_sections
        from models.secret import SECTION_STATE_NEW_OPEN

        view = self._send(game, GetSecretSectionPacket(), GetSecretSectionRes)
        res = view.decode_sub_packet(GetSecretSectionRes)  # type: ignore[attr-defined]
        opens = res.new_open_sections()
        if not opens:
            return False, "契约拼图抽取跳过（没有可抽取的区段）"

        sections = load_secret_sections()
        opens.sort(
            key=lambda s: (
                sections[s.sectionID].order if s.sectionID in sections else 99,
                s.sectionID,
            )
        )
        target = opens[0]
        conf = sections.get(target.sectionID)
        label = conf.name if conf is not None else "契约区段"

        packet = SweepSectionPacket(
            sectionID=target.sectionID, times=1, useSweepTicket=0
        )
        # ★ useSweepTicket=0 也必须照发（与素材扫荡同一字节纪律）
        view = self._send(game, packet, SweepSectionRes, include_defaults=True)
        res2 = view.decode_sub_packet(SweepSectionRes)  # type: ignore[attr-defined]
        if getattr(res2, "is_success", False):
            return True, f"契约拼图抽取成功（{label} ×1）"
        return False, f"契约拼图抽取失败（errorCode={res2.errorCode}）"

    # ------------------------------------------------------------------
    # 演练
    # ------------------------------------------------------------------
    def _dry_run(self, game: GameClient) -> TaskResult:
        """演练：打印本任务的动作清单与首发报文（不发送任何请求）。"""
        from models.daily import DailyMission

        print("本任务会按服务端状态依次执行（真跑时逐项决定是否发送）：")
        print("  1. 拉 6001 任务状态（只读）")
        print("  2. DMReward(misID=-1) ×4 一键领奖（无待领时服务端回 -1，属正常）")
        print("  3. 英雄升级：喂经验书只升 1 级（UpRoleExpPacket 5003）")
        print("  4. 技能升级：品质达标的最便宜技能位 +1 级（UpgradeSkillPacket 4001）")
        print("  5. 装备强化：强化等级最低的装备 ×1（StrengthenEquipPacket 3003）")
        print("  6. 英雄赠礼：低阶礼物 ×1（UsePresentPacket 5015）")
        print(
            "     ↑ 3/4/6 的目标英雄 = 等级最低的 SSR；不可行（满级/书不够/技能未开放…）"
            "则按 (等级, roleSid) 顺序自动顺延，最多真发 3 次"
        )
        print("  7. 好友赠礼（全送）：一键全送（FriendOperationPacket 10009，无成本）")
        print("  8. 契约拼图抽取：NewOpen 区段 ×1（SweepSectionPacket 11009）")
        print()
        request = game.build(DailyMission(type=[]), include_defaults=True)
        print("首发包（6001）的组装结果如下，其余包在真跑时按状态组装：")
        print(request.describe())
        print()
        print("—— 以上为演练输出，未发送任何请求 ——")
        return TaskResult(
            task=self.name,
            ok=True,
            message=f"演练完成：6001 请求体 {len(request.body)} 字节，未发送",
            data={"body_size": len(request.body)},
        )
