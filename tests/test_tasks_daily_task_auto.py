"""「每日任务自动执行」（``tasks/daily_task_auto.py``）的单元测试。

运行方式：

    python -m pytest tests/test_tasks_daily_task_auto.py -q
    python -m tests.test_tasks_daily_task_auto

【本文件要钉死的几条】
1. **报文字节**：技能升级包的 9 字节与非 packed 形态、5015/11009 的
   "0 值字段显式发送"纪律（``include_defaults=True``）—— 全部来自 2026-10-16 真机抓包/探针；
2. **目标选择的纯逻辑**：选书（凑够且少溢出）、技能位挑选（品质门槛过滤 + 最便宜）；
3. **流程降级**：单步失败只记一笔、不中断整条链；报告里含"补做 / 未完成 / 缺口"。

全部离线：不读网络、不需要真实素材以外的任何环境。
"""

from __future__ import annotations

from types import SimpleNamespace

from client.session import GameSession
from models.bbq import FriendClass
from models.daily import ItemClass, MissionType
from models.equip import GetAllEquipRes, StrengthenEquipPacket, StrengthenEquipRes
from models.friend import (
    FriendOperationPacket,
    FriendOperationRes,
    GetFriendListPacket,
    GetFriendListRes,
)
from models.game_config import RoleAttributes, RoleSkillGates, SkillUpgradeCost
from models.present import UsePresentPacket, UsePresentRes
from models.protobuf_wire import WIRE_VARINT, fields_by_number
from models.role import (
    RoleClass,
    SkillClass,
    UpgradeSkillPacket,
    UpgradeSkillRes,
    UpRoleExpPacket,
    UpRoleExpRes,
)
from models.secret import (
    GetSecretSectionRes,
    SecretSectionClass,
    SweepSectionPacket,
    SweepSectionRes,
)
from models.server import EquipClass
from models.session_state import GameSessionState
from models.wallet import GetAllItemRes
from tasks.base import create_task
from tests.reward_guard import assert_no_bare_identifiers

import tasks.daily_task_auto as daily_auto  # 导入即注册，同时便于 monkeypatch 模块内引用


def make_session() -> GameSession:
    """构造一个**不会联网**的会话（域名是保留域名）。"""
    session = GameSession("https://example.invalid", timeout=1.0)
    session.token_store.update(uid="u1", token="tok")
    session.set_game_state(GameSessionState(token="tok", player_id=1, server_id=1))
    return session


class FakeSend:
    """把 ``DailyTaskAutoTask._send`` 换成"记录 + 回放"的替身。

    以**类属性**替换 ``_send``：Python 调用 ``self._send(...)`` 时，
    对这个可调用对象执行的是 ``type(sent).__call__(sent, ...)`` ——
    也就是 "__call__ 的第一个参数就是 sent 自己"，所以下面没有额外的 task 参数。
    """

    def __init__(self, responses: dict[type, object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, object, bool]] = []

    def __call__(
        self,
        game: object,
        packet: object,
        expect: type,
        *,
        include_defaults: bool = False,
    ) -> object:
        self.calls.append((type(packet).__name__, packet, include_defaults))
        response = self.responses[expect]
        if isinstance(response, list):
            # 顺延类用例：同一种响应按调用先后依次回放（例如先 -3 再 0）
            response = response.pop(0)
        return SimpleNamespace(decode_sub_packet=lambda cls, _r=response: _r)


# ---------------------------------------------------------------------------
# 报文编码（字节对齐抓包）
# ---------------------------------------------------------------------------
class TestPacketEncoders:
    def test_upgrade_skill_bytes_match_probe(self) -> None:
        """与 2026-10-16 探针逐字节一致：roleSid=155530747, place=2, target=[3]。"""
        packet = UpgradeSkillPacket(roleSid=155530747, skillPlace=2, targetLV=[3])
        assert packet.encode(include_defaults=False).hex() == "08fbeb944a10021803"

    def test_upgrade_skill_target_list_is_non_packed(self) -> None:
        """targetLV 线上是非 packed：单元素应是一个裸 varint（线类型 0）。"""
        fields = fields_by_number(
            UpgradeSkillPacket(roleSid=1, skillPlace=1, targetLV=[2]).encode()
        )
        assert fields[3][0].wire_type == WIRE_VARINT
        assert fields[3][0].as_int32() == 2

    def test_use_present_zero_fields_follow_include_defaults(self) -> None:
        """5015 的 itemId=0 / useTime=0 必须靠 include_defaults=True 才会发出。"""
        packet = UsePresentPacket(
            roleSid=5,
            itemId=0,
            useTime=0,
            itemClass=[ItemClass(itemSid=-1, itemID=200600001, itemAmount=1)],
        )
        with_zeros = fields_by_number(packet.encode(include_defaults=True))
        without_zeros = fields_by_number(packet.encode(include_defaults=False))
        assert 2 in with_zeros and 3 in with_zeros  # itemId / useTime 照发
        assert 2 not in without_zeros and 3 not in without_zeros
        # 礼物本体（itemSid=-1 是负数的 10 字节补码形态，由编码器统一处理）
        assert with_zeros[4][0].as_fields()[0].as_int32() == -1

    def test_sweep_section_ticket_zero_follows_include_defaults(self) -> None:
        """11009 的 useSweepTicket=0 必须照发（否则服务端判字节形状不符）。"""
        packet = SweepSectionPacket(sectionID=909179101, times=1, useSweepTicket=0)
        assert 3 in fields_by_number(packet.encode(include_defaults=True))
        assert 3 not in fields_by_number(packet.encode(include_defaults=False))

    def test_strengthen_equip_roundtrip(self) -> None:
        fields = fields_by_number(StrengthenEquipPacket(equipSid=670138098, count=1).encode())
        assert fields[1][0].as_int32() == 670138098
        assert fields[2][0].as_int32() == 1

    def test_skill_class_levels(self) -> None:
        skills = SkillClass(activeLV=3, loopLV=2, passive1LV=1)
        assert skills.level_of(1) == 3
        assert skills.level_of(2) == 2
        assert skills.level_of(9) == 0
        assert skills.levels() == (3, 2, 1, 0, 0)


# ---------------------------------------------------------------------------
# 纯函数：选书 / 选技能位
# ---------------------------------------------------------------------------
class TestPickBooks:
    def test_exact_cover_prefers_big_then_tail(self) -> None:
        books = [(11, 200000007, 2600, 10), (12, 200000003, 30, 10)]
        picked = daily_auto.pick_books(books, 2630)
        assert picked is not None
        picks, total = picked
        assert total == 2630  # 2600×1 + 30×1，零溢出
        assert sorted(picks) == [(11, 200000007, 1), (12, 200000003, 1)]

    def test_insufficient_stock_returns_none(self) -> None:
        books = [(1, 200000003, 30, 2)]  # 只有 60 经验
        assert daily_auto.pick_books(books, 100) is None

    def test_tail_overshoot_is_bounded_by_smallest_book(self) -> None:
        books = [(1, 200000008, 15000, 10), (2, 200000003, 30, 10)]
        picked = daily_auto.pick_books(books, 15010)
        assert picked is not None
        _, total = picked
        assert 15010 <= total < 15010 + 30


class TestChooseSkillSlot:
    def make_costs(self) -> dict[int, SkillUpgradeCost]:
        # 1 级时：主动 10 / 循环 6 / 被动1 7 / 被动2 7 / 觉醒 8
        return {
            1: SkillUpgradeCost(
                level=1, active=10, loop=6, passive1=7, passive2=7, awakening=8
            )
        }

    def test_quality_gate_filters_slots(self) -> None:
        """品质 0：门槛 (0,1,4,6,2) 只放行主动位（序号 1）。"""
        chosen = daily_auto.choose_skill_slot(
            levels=(1, 1, 1, 1, 1),
            costs_by_level=self.make_costs(),
            requirement=(0, 1, 4, 6, 2),
            quality=0,
        )
        assert chosen == (1, 1)

    def test_cheapest_wins_among_open_slots(self) -> None:
        """品质 4：主动(10) / 循环(6) / 被动1(7) / 觉醒(8) 开放 → 循环最便宜。"""
        chosen = daily_auto.choose_skill_slot(
            levels=(1, 1, 1, 1, 1),
            costs_by_level=self.make_costs(),
            requirement=(0, 1, 4, 6, 2),
            quality=4,
        )
        assert chosen == (2, 1)

    def test_no_candidate_returns_none(self) -> None:
        chosen = daily_auto.choose_skill_slot(
            levels=(1, 1, 1, 1, 1),
            costs_by_level={},  # 等级不在花费表里
            requirement=(0, 1, 4, 6, 2),
            quality=0,
        )
        assert chosen is None


# ---------------------------------------------------------------------------
# 动作步骤（假发包替身）
# ---------------------------------------------------------------------------
class TestSteps:
    def test_hero_level_up_packet_and_text(self, monkeypatch) -> None:
        import models.game_config as gc

        task = create_task("daily_task_auto", make_session())
        sent = FakeSend({UpRoleExpRes: UpRoleExpRes(errorCode=0, roleClass=RoleClass(roleLV=32))})
        monkeypatch.setattr(daily_auto.DailyTaskAutoTask, "_send", sent)
        # 经验表：31→32 阈值 = 2×expSum[31]；roleExp=1950 → 需 50
        monkeypatch.setattr(gc, "load_role_exp_table", lambda: {31: 1000, 32: 1010})
        monkeypatch.setattr(gc, "load_exp_books", lambda: {200000003: 30})

        hero = RoleClass(roleSid=7, roleLV=31, roleExp=1950)
        bag = GetAllItemRes(
            itemList=[ItemClass(itemSid=1, itemID=200000003, itemAmount=10)]
        )
        ok, text = task._step_hero_level_up(None, [hero], "", bag, "")  # type: ignore[arg-type]

        assert ok is True and "31→32" in text
        name, packet, _ = sent.calls[0]
        assert name == "UpRoleExpPacket"
        assert packet.roleSid == 7  # type: ignore[attr-defined]
        # 60 ≥ 50，溢出 10 < 下一级阈值 2×(1010−1000)=20 → 2 本小书
        assert packet.itemClass[0].itemSid == 1  # type: ignore[attr-defined]
        assert packet.itemClass[0].itemAmount == 2  # type: ignore[attr-defined]

    def test_overshoot_guard_skips(self, monkeypatch) -> None:
        import models.game_config as gc

        task = create_task("daily_task_auto", make_session())
        sent = FakeSend({})
        monkeypatch.setattr(daily_auto.DailyTaskAutoTask, "_send", sent)
        monkeypatch.setattr(gc, "load_role_exp_table", lambda: {31: 1000, 32: 1010})
        monkeypatch.setattr(gc, "load_exp_books", lambda: {200000010: 380000})

        hero = RoleClass(roleSid=7, roleLV=31, roleExp=1900)
        bag = GetAllItemRes(itemList=[ItemClass(itemSid=1, itemID=200000010, itemAmount=5)])
        ok, text = task._step_hero_level_up(None, [hero], "", bag, "")  # type: ignore[arg-type]

        assert ok is False and "跳过" in text
        assert sent.calls == []  # 一个包都没发

    def test_insufficient_books_skips(self, monkeypatch) -> None:
        import models.game_config as gc

        task = create_task("daily_task_auto", make_session())
        sent = FakeSend({})
        monkeypatch.setattr(daily_auto.DailyTaskAutoTask, "_send", sent)
        monkeypatch.setattr(gc, "load_role_exp_table", lambda: {31: 1000, 32: 1010})
        monkeypatch.setattr(gc, "load_exp_books", lambda: {200000003: 30})

        hero = RoleClass(roleSid=7, roleLV=31, roleExp=1900)
        bag = GetAllItemRes(itemList=[ItemClass(itemSid=1, itemID=200000003, itemAmount=1)])
        ok, text = task._step_hero_level_up(None, [hero], "", bag, "")  # type: ignore[arg-type]

        assert ok is False and "经验书不足" in text
        assert sent.calls == []

    def test_skill_up_packet_and_text(self, monkeypatch) -> None:
        import models.game_config as gc

        task = create_task("daily_task_auto", make_session())
        sent = FakeSend({UpgradeSkillRes: UpgradeSkillRes(errorCode=0)})
        monkeypatch.setattr(daily_auto.DailyTaskAutoTask, "_send", sent)
        monkeypatch.setattr(
            gc, "load_role_skill_gates",
            lambda: {130005204: RoleSkillGates(role_main_id=130005204, requirement=(0, 1, 4, 6, 2))},
        )
        monkeypatch.setattr(
            gc, "load_role_attributes",
            lambda: {130005204: RoleAttributes(role_main_id=130005204, quality=4)},
        )
        monkeypatch.setattr(
            gc, "load_skill_upgrade_costs",
            lambda: {1: SkillUpgradeCost(level=1, active=10, loop=6, passive1=7, passive2=7, awakening=8)},
        )

        hero = RoleClass(roleSid=7, roleID=130005204, roleLV=31, skillsLV=SkillClass(activeLV=1, loopLV=1))
        ok, text = task._step_skill_up(None, [hero], "")  # type: ignore[arg-type]

        assert ok is True and "循环技 1→2" in text
        name, packet, _ = sent.calls[0]
        assert name == "UpgradeSkillPacket"
        assert packet.skillPlace == 2  # type: ignore[attr-defined]
        assert packet.targetLV == [2]  # type: ignore[attr-defined]

    def test_skill_up_reports_quality_gate(self, monkeypatch) -> None:
        """服务端回 -4（品质门槛）时如实上报可读原因（按选中位给出）。"""
        import models.game_config as gc

        task = create_task("daily_task_auto", make_session())
        sent = FakeSend({UpgradeSkillRes: UpgradeSkillRes(errorCode=-4)})
        monkeypatch.setattr(daily_auto.DailyTaskAutoTask, "_send", sent)
        monkeypatch.setattr(
            gc, "load_role_skill_gates",
            lambda: {130005204: RoleSkillGates(role_main_id=130005204, requirement=(0, 1, 4, 6, 2))},
        )
        monkeypatch.setattr(
            gc, "load_role_attributes",
            lambda: {130005204: RoleAttributes(role_main_id=130005204, quality=0)},
        )
        monkeypatch.setattr(
            gc, "load_skill_upgrade_costs",
            lambda: {1: SkillUpgradeCost(level=1, active=6, loop=6, passive1=7, passive2=7, awakening=8)},
        )

        hero = RoleClass(roleSid=7, roleID=130005204, roleLV=31, skillsLV=SkillClass(activeLV=1))
        ok, text = task._step_skill_up(None, [hero], "")  # type: ignore[arg-type]

        assert ok is False and "-4" in text and "品质门槛" in text
        # 品质 0 时只有主动位可选（门槛 0），发出去的应是 place=1
        _, packet, _ = sent.calls[0]
        assert packet.skillPlace == 1  # type: ignore[attr-defined]

    def test_equip_strengthen_picks_lowest_level(self, monkeypatch) -> None:
        task = create_task("daily_task_auto", make_session())
        equips = [
            EquipClass(equipSid=2, equipLV=9),
            EquipClass(equipSid=1, equipLV=3),
            EquipClass(equipSid=3, equipLV=3),
        ]
        sent = FakeSend(
            {
                GetAllEquipRes: GetAllEquipRes(equipList=equips),
                StrengthenEquipRes: StrengthenEquipRes(errorCode=0),
            }
        )
        monkeypatch.setattr(daily_auto.DailyTaskAutoTask, "_send", sent)

        ok, text = task._step_equip_strengthen(None)  # type: ignore[arg-type]
        assert ok is True and "装备强化成功" in text
        _, packet, _ = sent.calls[1]
        assert packet.equipSid == 1 and packet.count == 1  # type: ignore[attr-defined]

    def test_friend_gift_sends_all_gift(self, monkeypatch) -> None:
        task = create_task("daily_task_auto", make_session())
        friends = [
            FriendClass(send_state=0),
            FriendClass(send_state=1),
            FriendClass(send_state=0),
        ]
        sent = FakeSend(
            {
                GetFriendListRes: GetFriendListRes(friendClass=friends),
                FriendOperationRes: FriendOperationRes(errorCode=0),
            }
        )
        monkeypatch.setattr(daily_auto.DailyTaskAutoTask, "_send", sent)

        ok, text = task._step_friend_gift(None)  # type: ignore[arg-type]
        assert ok is True and "一键全送" in text and "2" in text  # 可送 2 位
        # 好友列表包必须 include_defaults=True（0 值照发）
        name1, _, include1 = sent.calls[0]
        assert name1 == "GetFriendListPacket" and include1 is True
        name2, packet2, include2 = sent.calls[1]
        assert name2 == "FriendOperationPacket"
        assert packet2.actionType == 4 and packet2.playerID == -1  # type: ignore[attr-defined]
        assert include2 is False

    def test_contract_draw_picks_new_open_smallest_order(self, monkeypatch) -> None:
        import models.game_config as gc

        task = create_task("daily_task_auto", make_session())
        sections = [
            SecretSectionClass(sectionID=909179104, sectionState=0),
            SecretSectionClass(sectionID=909179101, sectionState=0),
            SecretSectionClass(sectionID=909001101, sectionState=2),
        ]
        sent = FakeSend(
            {
                GetSecretSectionRes: GetSecretSectionRes(SecretSectionList=sections),
                SweepSectionRes: SweepSectionRes(errorCode=0),
            }
        )
        monkeypatch.setattr(daily_auto.DailyTaskAutoTask, "_send", sent)
        monkeypatch.setattr(
            gc, "load_secret_sections",
            lambda: {
                909179104: SimpleNamespace(order=4, name="拼圖召喚Step 4"),
                909179101: SimpleNamespace(order=1, name="拼圖召喚Step 1"),
            },
        )

        ok, text = task._step_contract_draw(None)  # type: ignore[arg-type]
        assert ok is True and "拼圖召喚Step 1" in text
        name, packet, include = sent.calls[1]
        assert name == "SweepSectionPacket"
        assert packet.sectionID == 909179101  # type: ignore[attr-defined]
        assert packet.times == 1 and packet.useSweepTicket == 0  # type: ignore[attr-defined]
        assert include is True  # 0 值字段照发


# ---------------------------------------------------------------------------
# 流程与报告（端到端替身）
# ---------------------------------------------------------------------------
class TestRunFlow:
    def build_fake_state(self) -> SimpleNamespace:
        """构造 6002 的替身：技能/强化/好友已完成；升级/赠礼/契约未完成。"""
        completed = {
            daily_auto.MISSION_SKILL_UP: 1,
            daily_auto.MISSION_EQUIP_STRENGTHEN: 1,
            daily_auto.MISSION_FRIEND_GIFT: 3,  # 好友任务的完成要求是 3 次
        }
        todo = [
            daily_auto.MISSION_HERO_LEVEL,
            daily_auto.MISSION_HERO_GIFT,
            daily_auto.MISSION_CONTRACT_DRAW,
        ]
        items = [
            SimpleNamespace(misID=mid, completedTimes=count, isGotReward=1)
            for mid, count in completed.items()
        ]
        items += [SimpleNamespace(misID=mid, completedTimes=0, isGotReward=0) for mid in todo]
        return SimpleNamespace(dmInitList=items)

    def test_run_reports_done_issues_and_gaps(self, monkeypatch) -> None:
        import models.game_config as gc

        task = create_task("daily_task_auto", make_session())
        state = self.build_fake_state()

        monkeypatch.setattr(daily_auto, "fetch_daily_state", lambda game: state)
        monkeypatch.setattr(
            daily_auto, "plan_pending_boxes", lambda s: ([], [], 10, 11)
        )
        fakes = [
            SimpleNamespace(mission_id=daily_auto.MISSION_HERO_LEVEL, required_count=1, title="測試升級", add_point=1),
            SimpleNamespace(mission_id=daily_auto.MISSION_SKILL_UP, required_count=1, title="測試技能", add_point=1),
            SimpleNamespace(mission_id=daily_auto.MISSION_EQUIP_STRENGTHEN, required_count=1, title="測試強化", add_point=1),
            SimpleNamespace(mission_id=daily_auto.MISSION_HERO_GIFT, required_count=1, title="測試贈禮", add_point=1),
            SimpleNamespace(mission_id=daily_auto.MISSION_FRIEND_GIFT, required_count=3, title="測試好友", add_point=1),
            SimpleNamespace(mission_id=daily_auto.MISSION_CONTRACT_DRAW, required_count=1, title="測試契約", add_point=1),
        ]
        monkeypatch.setattr(gc, "load_daily_missions", lambda: fakes)
        monkeypatch.setattr(
            gc, "load_daily_reward_boxes",
            lambda: [SimpleNamespace(required_point=9)],
        )
        monkeypatch.setattr(
            daily_auto, "claim_one_key",
            lambda game, mtype: (True, "奖励已入账（金币×1）" if int(mtype) == int(MissionType.DAILY) else ""),
        )
        # 动作之间的真实 sleep 换成空操作（测试不真的等 1.2 秒）
        monkeypatch.setattr(daily_auto.cancellation, "sleep_interruptible", lambda seconds: None)

        monkeypatch.setattr(
            daily_auto.DailyTaskAutoTask, "_fetch_roles", lambda self, game: []
        )
        monkeypatch.setattr(
            daily_auto.DailyTaskAutoTask, "_fetch_bag",
            lambda self, game: GetAllItemRes(),
        )
        monkeypatch.setattr(
            daily_auto.DailyTaskAutoTask, "_pick_target_candidates",
            lambda self, roles: [RoleClass(roleSid=1, roleLV=31)],
        )
        monkeypatch.setattr(
            daily_auto.DailyTaskAutoTask, "_step_hero_level_up",
            lambda self, *a, **k: (True, "英雄升级 31→32 级（经验书 2 种）"),
        )
        monkeypatch.setattr(
            daily_auto.DailyTaskAutoTask, "_step_hero_gift",
            lambda self, *a, **k: (False, "英雄赠礼跳过（背包里没有可送的礼物）"),
        )
        monkeypatch.setattr(
            daily_auto.DailyTaskAutoTask, "_step_contract_draw",
            lambda self, *a, **k: (False, "契约拼图抽取跳过（没有可抽取的区段）"),
        )

        result = task._run(None)  # type: ignore[arg-type]

        assert result.ok is False  # 有未完成项
        message = result.message
        assert "补做 1 项" in message
        assert "另有 3 项今日已完成" in message
        assert "未完成 2 项" in message
        assert "活跃点 10/9" in message
        assert "測試升級" in message  # 缺口清单带标题
        assert result.data["already_done"] == 3
        assert len(result.data["done"]) == 1
        assert len(result.data["issues"]) == 2
        assert result.data["claimed"]  # 起始与结尾各领了一次
        assert_no_bare_identifiers(message)

    def test_mission_done_helper(self) -> None:
        task = create_task("daily_task_auto", make_session())
        conf = {1: SimpleNamespace(required_count=3)}
        state = SimpleNamespace(dmInitList=[SimpleNamespace(misID=1, completedTimes=2)])
        assert task._mission_done(conf, state, 1) is False
        state2 = SimpleNamespace(dmInitList=[SimpleNamespace(misID=1, completedTimes=3)])
        assert task._mission_done(conf, state2, 1) is True
        # 服务端没下发这条任务 → 视为未完成（尝试会让服务端来裁决）
        assert task._mission_done(conf, SimpleNamespace(dmInitList=[]), 1) is False


# ---------------------------------------------------------------------------
# 自动顺延（2026-10-16 用户拍板 B）：静态跳过不可行候选，向服务端最多真发 3 次
# ---------------------------------------------------------------------------
class TestFallback:
    def test_level_up_falls_back_past_static_skip(self, monkeypatch) -> None:
        import models.game_config as gc

        task = create_task("daily_task_auto", make_session())
        sent = FakeSend({UpRoleExpRes: UpRoleExpRes(errorCode=0, roleClass=RoleClass(roleLV=12))})
        monkeypatch.setattr(daily_auto.DailyTaskAutoTask, "_send", sent)
        monkeypatch.setattr(gc, "load_role_exp_table", lambda: {1: 10, 2: 40, 11: 1000, 12: 1010})
        monkeypatch.setattr(gc, "load_exp_books", lambda: {200000004: 140, 200000003: 30})

        # 候选 1：LV1 且经验值异常（静态跳过，不发包）；
        # 候选 2：LV11（需要 50，30×2 刚好）→ 顺延成功
        hero1 = RoleClass(roleSid=1, roleLV=1, roleExp=25)
        hero2 = RoleClass(roleSid=2, roleLV=11, roleExp=1950)
        bag = GetAllItemRes(
            itemList=[
                ItemClass(itemSid=11, itemID=200000004, itemAmount=10),
                ItemClass(itemSid=12, itemID=200000003, itemAmount=10),
            ]
        )
        ok, text = task._step_hero_level_up(None, [hero1, hero2], "", bag, "")  # type: ignore[arg-type]

        assert ok is True and "顺延" in text
        assert len(sent.calls) == 1  # 静态跳过不占发包次数
        _, packet, _ = sent.calls[0]
        assert packet.roleSid == 2  # type: ignore[attr-defined]

    def test_skill_up_skips_level1_and_falls_back(self, monkeypatch) -> None:
        import models.game_config as gc

        task = create_task("daily_task_auto", make_session())
        sent = FakeSend({UpgradeSkillRes: [UpgradeSkillRes(errorCode=0)]})
        monkeypatch.setattr(daily_auto.DailyTaskAutoTask, "_send", sent)
        monkeypatch.setattr(
            gc, "load_role_skill_gates",
            lambda: {130003200: RoleSkillGates(role_main_id=130003200, requirement=(0, 1, 4, 6, 2))},
        )
        monkeypatch.setattr(
            gc, "load_role_attributes",
            lambda: {130003200: RoleAttributes(role_main_id=130003200, quality=0)},
        )
        monkeypatch.setattr(
            gc, "load_skill_upgrade_costs",
            lambda: {1: SkillUpgradeCost(level=1, active=6, loop=6, passive1=7, passive2=7, awakening=8)},
        )

        hero1 = RoleClass(roleSid=1, roleID=130003200, roleLV=1, skillsLV=SkillClass(activeLV=1))
        hero2 = RoleClass(roleSid=2, roleID=130003200, roleLV=11, skillsLV=SkillClass(activeLV=1))
        ok, text = task._step_skill_up(None, [hero1, hero2], "")  # type: ignore[arg-type]

        assert ok is True and "顺延" in text
        # LV1 被静态跳过（实测 -3）→ 只对候选 2 真实发了一次
        assert len(sent.calls) == 1
        _, packet, _ = sent.calls[0]
        assert packet.roleSid == 2  # type: ignore[attr-defined]

    def test_skill_up_falls_back_on_server_reject(self, monkeypatch) -> None:
        import models.game_config as gc

        task = create_task("daily_task_auto", make_session())
        # 第一个候选被服务端回 -3（未知门槛），第二个成功
        sent = FakeSend(
            {UpgradeSkillRes: [UpgradeSkillRes(errorCode=-3), UpgradeSkillRes(errorCode=0)]}
        )
        monkeypatch.setattr(daily_auto.DailyTaskAutoTask, "_send", sent)
        monkeypatch.setattr(
            gc, "load_role_skill_gates",
            lambda: {130003200: RoleSkillGates(role_main_id=130003200, requirement=(0, 1, 4, 6, 2))},
        )
        monkeypatch.setattr(
            gc, "load_role_attributes",
            lambda: {130003200: RoleAttributes(role_main_id=130003200, quality=0)},
        )
        monkeypatch.setattr(
            gc, "load_skill_upgrade_costs",
            lambda: {1: SkillUpgradeCost(level=1, active=6, loop=6, passive1=7, passive2=7, awakening=8)},
        )

        hero1 = RoleClass(roleSid=1, roleID=130003200, roleLV=5, skillsLV=SkillClass(activeLV=1))
        hero2 = RoleClass(roleSid=2, roleID=130003200, roleLV=11, skillsLV=SkillClass(activeLV=1))
        ok, text = task._step_skill_up(None, [hero1, hero2], "")  # type: ignore[arg-type]

        assert ok is True and "顺延" in text
        assert len(sent.calls) == 2  # 两次都真的发了（-3 不是静态可判的）
        assert sent.calls[1][1].roleSid == 2  # type: ignore[attr-defined]

    def test_hero_gift_prefers_lowest_gift(self, monkeypatch) -> None:
        task = create_task("daily_task_auto", make_session())
        sent = FakeSend({UsePresentRes: UsePresentRes(errorCode=0, addExp=50)})
        monkeypatch.setattr(daily_auto.DailyTaskAutoTask, "_send", sent)

        hero = RoleClass(roleSid=7, roleLV=11)
        bag = GetAllItemRes(
            itemList=[
                ItemClass(itemSid=91, itemID=200600091, itemAmount=1),
                ItemClass(itemSid=1, itemID=200600001, itemAmount=2),
            ]
        )
        ok, text = task._step_hero_gift(None, [hero], "", bag, "")  # type: ignore[arg-type]

        assert ok is True and "好感 +50" in text
        name, packet, include = sent.calls[0]
        assert name == "UsePresentPacket"
        # 白名单里 200600001 在前 → 优先送它
        assert packet.itemClass[0].itemID == 200600001  # type: ignore[attr-defined]
        assert include is True
