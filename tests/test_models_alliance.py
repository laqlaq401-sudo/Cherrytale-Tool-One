"""工会（Alliance）报文模型（``models/alliance.py`` / ``models/role.py``）的单元测试。

运行方式：

    python -m pytest tests/test_models_alliance.py -q
    python -m tests.test_models_alliance

【本文件要钉死的三条规则】
1. **模型字段 = 字段表字段**：``ProtoMessage.encode`` 遇到"字段表里有、模型里
   没有"会直接报错 —— 这里用集合比对把每个模型都核一遍，新增字段漏声明时
   先红在这里，而不是运行到一半。
2. **捐献只有金币档**：``DonatePacket.for_gold_tier()`` 是唯一构造途径，
   编码出的 ``actionType`` 恒为 693000001；钻石档 ID 永远不该出现在任何
   可构造的请求字节里。
3. **矿点状态判读**：``endTime`` / ``isReceived`` 的语义（空闲/挖掘中/可收）
   是任务层收矿与开矿的判断依据，这里用边界值钉死。

手拼字节用 ``models/protobuf_wire`` 的原语，字段编号以
``models/proto_fields.py`` 的生成表为准（编号顺序 = dump.cs 声明顺序）。
"""

from __future__ import annotations

import pytest

from models.alliance import (
    DIAMOND_TIER_DONATE_IDS,
    DONATE_ALLOWED_PACKET_IDS,
    ERROR_CODE_NAMES,
    GOLD_TIER_COST,
    GOLD_TIER_DONATE_ID,
    MINING_ALLOWED_PACKET_IDS,
    SIGN_IN_ALLOWED_PACKET_IDS,
    SUCCESS_ERROR_CODES,
    AllianceBingoClass,
    AllianceBingoHomeRes,
    AllianceBingoSignInPacket,
    AllianceClass,
    AllianceHomeRes,
    AllianceNewPitOneKeyStartPacket,
    AllianceNewPitPacket,
    AllianceNewPitRes,
    AlliancePersonalPitClass,
    AllianceTeamPitClass,
    AllianceTeamPitDetailClass,
    DonatePacket,
    DonateRes,
)
from models.game_packet import ProtoMessage
from models.protobuf_wire import encode_int, encode_message, encode_packed_ints, encode_string
from models.role import GetAllRolePacket, GetAllRoleRes, RoleClass

# ---------------------------------------------------------------------------
# 字段表一致性：模型必须声明字段表里的每一个字段
# ---------------------------------------------------------------------------
ALLIANCE_MODEL_CLASSES: tuple[type[ProtoMessage], ...] = (
    AllianceHomeRes,
    AllianceClass,
    DonatePacket,
    DonateRes,
    AllianceBingoClass,
    AllianceBingoHomeRes,
    AllianceBingoSignInPacket,
    AllianceNewPitPacket,
    AllianceNewPitRes,
    AlliancePersonalPitClass,
    AllianceTeamPitClass,
    AllianceTeamPitDetailClass,
    AllianceNewPitOneKeyStartPacket,
    GetAllRolePacket,
    GetAllRoleRes,
    RoleClass,
)


class TestFieldsMatchGeneratedTable:
    @pytest.mark.parametrize("model", ALLIANCE_MODEL_CLASSES, ids=lambda m: m.__name__)
    def test_model_declares_every_tab_field(self, model: type[ProtoMessage]) -> None:
        """字段表里的字段在模型里必须全部存在（少一个 encode 就会炸）。"""
        tab_keys = set(model.field_tag_map())
        declared = set(model.model_fields) - {"proto_class_name"}
        missing = tab_keys - declared
        assert not missing, f"{model.__name__} 缺少字段：{sorted(missing)}"

    def test_proto_class_names_are_exact(self) -> None:
        """``proto_class_name`` 是查表唯一依据，抄错一个字母查的就是别的类。"""
        for model in ALLIANCE_MODEL_CLASSES:
            assert model.proto_class_name, model.__name__


# ---------------------------------------------------------------------------
# 捐献：只有金币档
# ---------------------------------------------------------------------------
class TestDonateGoldTierOnly:
    def test_gold_tier_action_type(self) -> None:
        """金币档请求的 ``actionType`` 恒为 693000001（= 配置表初級档 donateID）。"""
        packet = DonatePacket.for_gold_tier()
        assert packet.actionType == GOLD_TIER_DONATE_ID == 693000001
        assert packet.encode() == encode_int(1, GOLD_TIER_DONATE_ID)

    def test_direct_construction_defaults_to_gold(self) -> None:
        """直接实例化的默认值也是金币档（不给"忘填=发钻石"留门）。"""
        assert DonatePacket().actionType == GOLD_TIER_DONATE_ID

    def test_diamond_tiers_not_constructible(self) -> None:
        """钻石档 ID 只存在于文档常量里；任何默认构造都碰不到它们。"""
        assert DIAMOND_TIER_DONATE_IDS == (693000002, 693000003)
        for tier in DIAMOND_TIER_DONATE_IDS:
            assert DonatePacket().actionType != tier
            assert DonatePacket.for_gold_tier().actionType != tier

    def test_gold_constants_match_config_table(self) -> None:
        """5000 金币 → 10 贡献来自 ``AllianceDonateData`` 693000001 行。"""
        assert GOLD_TIER_COST == 5000
        assert GOLD_TIER_DONATE_ID == 693000001

    def test_donate_res_gold_spent(self) -> None:
        """``gold_spent`` 按响应 costList 对账（itemID=2、itemAmount=3）。"""
        cost = encode_message(6, encode_message(1, encode_int(2, 200000050) + encode_int(3, 5000)))
        res = DonateRes.decode(encode_int(1, 1) + cost)
        assert res.gold_spent(200000050) == 5000
        assert res.gold_spent(200000001) == 0  # 钻石不计入

    def test_donate_whitelist_has_no_diamond_packets(self) -> None:
        """捐献白名单里只有主页与 21017 —— 钻石捐献不存在独立通道可开。"""
        assert set(DONATE_ALLOWED_PACKET_IDS.values()) == {21009, 21017}


# ---------------------------------------------------------------------------
# 签到：格子与位置
# ---------------------------------------------------------------------------
def cell(pos: int, *, signed: bool) -> bytes:
    """``AllianceBingoClass``：1 SignInID、2 pos、3 iconData（有值=已签）。"""
    payload = encode_int(1, 940000000 + pos) + encode_int(2, pos)
    if signed:
        payload += encode_message(3, b"\x01\x02")  # iconData 有值
    return payload


class TestBingo:
    def test_next_sign_position_skips_occupied(self) -> None:
        """★ 2026-10-03：位置 = max(已占 pos) + 1（bingoData 只含已占格子）。"""
        res = AllianceBingoHomeRes.decode(
            encode_int(1, 1)
            + encode_message(2, cell(0, signed=True))
            + encode_message(2, cell(1, signed=True))
            + encode_message(2, cell(13, signed=True))
        )
        assert res.next_sign_position() == 14
        assert res.all_signed() is False

    def test_next_sign_position_matches_1003_capture(self) -> None:
        """10.3 抓包：已占 {0..7,13,19} 时客户端签 pos=25（服务端接受）。

        我们的推断（max+1=20）落在同一"未占用"集合内；棋盘全集没有静态
        来源，最终由服务端裁决（该待验条目见 notes/open_questions.md）。
        """
        occupied_cells = [cell(p, signed=True) for p in (0, 1, 2, 3, 4, 5, 6, 7, 13, 19)]
        res = AllianceBingoHomeRes.decode(
            encode_int(1, 1) + b"".join(encode_message(2, c) for c in occupied_cells)
        )
        assert res.next_sign_position() == 20

    def test_next_sign_position_empty_board(self) -> None:
        """空面板（新周期 / 数据未下发）从 0 号格子开始。"""
        res = AllianceBingoHomeRes()
        assert res.next_sign_position() == 0
        assert res.all_signed() is False

    def test_sign_packet_encoding(self) -> None:
        """``pos`` 是字段 1；位置 0 也要能显式发出去。"""
        assert AllianceBingoSignInPacket.at_position(3).encode() == encode_int(1, 3)
        assert AllianceBingoSignInPacket.at_position(0).encode() == encode_int(1, 0)

    def test_bingo_round_trip(self) -> None:
        """格子模型编码后再解码，pos / isBingo 不丢。"""
        original = AllianceBingoClass(SignInID=940000005, pos=5, isBingo=True)
        decoded = AllianceBingoClass.decode(original.encode())
        assert decoded.pos == 5
        assert decoded.isBingo is True


# ---------------------------------------------------------------------------
# 挖矿：状态判读与一键开矿
# ---------------------------------------------------------------------------
def personal_pit(
    sid: int,
    *,
    pit_id: int = 935000001,
    roles: tuple[int, ...] = (),
    end_time: int = 0,
    received: int = 0,
) -> bytes:
    """``AlliancePersonalPitClass``：1 sid、2 id、3 roleSidList（packed）、4 endTime、5 isReceived。"""
    payload = encode_int(1, sid) + encode_int(2, pit_id)
    if roles:
        payload += encode_packed_ints(3, list(roles))
    payload += encode_int(4, end_time) + encode_int(5, received)
    return payload


NOW = 1_700_000_000


class TestPitStateHelpers:
    def test_state_boundaries(self) -> None:
        idle = AlliancePersonalPitClass.decode(personal_pit(11))
        mining = AlliancePersonalPitClass.decode(
            personal_pit(12, end_time=NOW + 3600, roles=(501, 502))
        )
        done = AlliancePersonalPitClass.decode(personal_pit(13, end_time=NOW - 1))
        received = AlliancePersonalPitClass.decode(personal_pit(14, end_time=NOW - 1, received=1))

        assert idle.is_idle() and not idle.needs_harvest(NOW)
        assert mining.is_mining(NOW) and not mining.needs_harvest(NOW)
        assert done.needs_harvest(NOW)
        assert not received.needs_harvest(NOW)  # 已收过的不再收

    def test_done_at_exact_boundary(self) -> None:
        """``endTime == now`` 视为已完成（含边界，宁多收一次也不漏）。"""
        done = AlliancePersonalPitClass.decode(personal_pit(15, end_time=NOW))
        assert done.needs_harvest(NOW)

    def test_team_pit_slots_for_member(self) -> None:
        """团体矿按 ``memberPid`` 找自己的槽位（detail：1 needRoleInfoID、4 memberPid）。"""
        slot_a = encode_int(1, 133000001) + encode_int(2, 30) + encode_int(4, 1)  # 我的
        slot_b = encode_int(1, 133000002) + encode_int(2, 30)  # 待认领
        pit = AllianceTeamPitClass.decode(
            encode_int(1, 21)
            + encode_int(2, 936000001)
            + encode_message(3, slot_a)
            + encode_message(3, slot_b)
            + encode_int(4, 0)
        )
        assert pit.slots_for_member(1) == [0]
        assert pit.slots_for_member(2) == []
        assert pit.is_idle()

    def test_slots_for_member_does_not_treat_empty_as_mine(self) -> None:
        """★ ``player_id == 0``（未登录态）不该把空槽位误判成"自己的"。

        判据用 ``==`` 而非 ``<=``；这条测试锁住这个选择 —— 否则未登录时
        ``already_mine`` 会把所有空槽都算成自己的。
        """
        slot = encode_int(1, 133000001) + encode_int(2, 30) + encode_int(4, -1)
        pit = AllianceTeamPitClass.decode(encode_int(1, 21) + encode_message(3, slot))
        assert pit.empty_slot_indices() == [0]
        assert pit.slots_for_member(0) == []
        assert self._already_mine_guard(pit, 0) == 0
        assert self._already_mine_guard(pit, -1) == 0, "哨兵 -1 不该算成'我占的'"

    @staticmethod
    def _already_mine_guard(pit: AllianceTeamPitClass, my_pid: int) -> int:
        """复刻 ``tasks.alliance_mining`` 里 "我已占几个" 的统计口径。

        关键是 ``my_pid > 0`` 前缀 —— 未登录（0）与哨兵（-1）都不算数。
        这里刻意**不复用** ``slots_for_member``（那个是 ``==``，会命中 -1）。
        """
        return sum(
            1
            for slot in pit.detaliClassList
            if my_pid > 0 and slot.memberPid == my_pid
        )

    def test_team_pit_empty_slot_sentinel_is_minus_one(self) -> None:
        """★ 2026-10-05 核心修正：空槽哨兵是 ``-1``（不是 0）。

        ``is_empty`` 用 ``<= 0`` 兼容两种约定；``-1`` 是抓包实证的那一种。
        """
        from models.alliance import NO_MEMBER_PID

        assert NO_MEMBER_PID == -1

        def slot(pid: int) -> bytes:
            return encode_int(1, 133000001) + encode_int(2, 30) + encode_int(4, pid)

        # 三态：-1 / 0 都算空，正数算已占
        pit = AllianceTeamPitClass.decode(
            encode_int(1, 21)
            + encode_message(3, slot(NO_MEMBER_PID))
            + encode_message(3, slot(0))
            + encode_message(3, slot(10000006))
        )
        assert [s.is_empty() for s in pit.detaliClassList] == [True, True, False]
        assert pit.empty_slot_indices() == [0, 1]

    def test_old_zero_only_judgement_would_have_missed_slots(self) -> None:
        """★ 回归钉子：旧判据 ``not memberPid`` 在 -1 上恒为假。

        这条测试**故意表达旧的错误逻辑**，证明它确实什么都识别不出来 ——
        防止将来有人"简化"回 ``== 0``。
        """
        slot_bytes = (
            encode_int(1, 133000001) + encode_int(2, 30) + encode_int(4, -1)
        )
        pit = AllianceTeamPitClass.decode(
            encode_int(1, 21) + encode_message(3, slot_bytes)
        )
        old_style = [
            index
            for index, s in enumerate(pit.detaliClassList)
            if not s.memberPid  # 旧实现：等价 == 0
        ]
        assert old_style == [], "旧判据在 -1 上确实什么都识别不出（这就是 bug）"
        assert pit.empty_slot_indices() == [0], "新判据正确识别"

    def test_new_pit_res_round_trip(self) -> None:
        """21040 同时带个人/团体两张表 —— 解码后各归各位。"""
        payload = (
            encode_int(1, 1)
            + encode_message(2, personal_pit(31, roles=(1, 2), end_time=NOW + 60))
            + encode_int(4, 3)
            + encode_int(5, 2)
        )
        res = AllianceNewPitRes.decode(payload)
        assert res.is_success()
        assert [pit.sid for pit in res.personalList] == [31]
        assert res.personalList[0].roleSidList == [1, 2]
        assert (res.personalLimit, res.teamLimit) == (3, 2)

    def test_one_key_start_carries_pits(self) -> None:
        """一键开矿把矿点装进字段 1（本模块发空角色列表，等真机定案）。"""
        pits = [AlliancePersonalPitClass(sid=41, id=935000001)]
        packet = AllianceNewPitOneKeyStartPacket.for_pits(pits)
        decoded = AllianceNewPitOneKeyStartPacket.decode(packet.encode())
        assert [pit.sid for pit in decoded.oneKeyPersonalList] == [41]
        assert decoded.oneKeyPersonalList[0].roleSidList == []
        assert decoded.updateList == []


# ---------------------------------------------------------------------------
# 名册与其它
# ---------------------------------------------------------------------------
class TestRoleRoster:
    def test_roster_round_trip_and_by_role_id(self) -> None:
        """5012 名册：1 roleSid、2 roleID、3 roleLV；``by_role_id`` 按模板挑。

        .. warning::
           **形状是 dump.cs 推定，不是抓包实测** —— 5012 在 ``captures/`` 的
           19 份 SAZ 里 0 次出现。这条用例守的是"**我们自己的编解码自洽**"
           （能原样往返 + 按模板 id 筛得对），**不能**证明字段编号与服务端一致。
           首次真机看到 5012 时要用 ``describe_message()`` 对照，见
           ``models/role.py`` 头部。
        """
        role_a = encode_int(1, 701) + encode_int(2, 133000001) + encode_int(3, 40)
        role_b = encode_int(1, 702) + encode_int(2, 133000002) + encode_int(3, 10)
        res = GetAllRoleRes.decode(encode_message(1, role_a) + encode_message(1, role_b))
        assert len(res.roleList) == 2
        found = res.by_role_id(133000001)
        assert [role.roleSid for role in found] == [701]

    def test_roster_request_is_empty_packet(self) -> None:
        assert GetAllRolePacket().encode() == b""

    def test_roster_is_the_only_source_of_a_role_instance_sid(self) -> None:
        """★ 名册**不是**死代码：21043 的 ``roleSidList`` 只能从它来。

        团体占坑（21043）要填**角色实例 sid**，而 21040 的槽位只带
        ``needRoleInfoID``/``needRoleLv``（要什么模板、要多少级），
        从不说"你有哪个 sid 可用"。这里用实测 raw/84 的请求体作对照 ——
        它的 ``roleSidList=[155242087]`` 在 21040 里**根本不存在**。
        """
        # raw/84（21043）实测请求体逐字段解码结果：
        # {1: type=1, 2: pitSid=205294527, 3: roleSidList=[155242087], 4: teamPitIndex=2}
        CAPTURED_ROLE_SID = 155242087

        # 一个典型的 21040 槽位：只有"需要什么"，没有"你有什么"
        slot = AllianceTeamPitDetailClass(
            memberPid=-1,
            needRoleInfoID=133003000,
            needRoleLv=38,
        )
        assert not hasattr(slot, "roleSid")
        assert slot.needRoleInfoID != CAPTURED_ROLE_SID
        assert slot.needRoleLv != CAPTURED_ROLE_SID

        # 只有 5012 名册能把模板 id 映射成可填进请求的实例 sid
        role = RoleClass(roleSid=CAPTURED_ROLE_SID, roleID=slot.needRoleInfoID, roleLV=50)
        roster = GetAllRoleRes(roleList=[role])
        matched = roster.by_role_id(slot.needRoleInfoID)
        assert [r.roleSid for r in matched] == [CAPTURED_ROLE_SID]
        assert matched[0].roleLV >= slot.needRoleLv


class TestErrorCodes:
    def test_success_codes(self) -> None:
        """Alliance 的成功族按客户端枚举取 {0, 1}（与其它系统的纯 0 不同）。"""
        assert SUCCESS_ERROR_CODES == frozenset({0, 1})
        assert AllianceHomeRes(errorCode=1).is_success()
        assert AllianceHomeRes(errorCode=0).is_success()
        assert not AllianceHomeRes(errorCode=19).is_success()

    def test_error_code_names_cover_key_cases(self) -> None:
        """任务层报错要可读：入会/捐献上限/金币不足这几个必须有名字。"""
        from models.alliance import error_code_name

        for code in (7, 10, 19, 31, 34):
            assert code in ERROR_CODE_NAMES
        assert error_code_name(19).startswith("HaveNotBeen")
        assert error_code_name(12345).startswith("未知错误码")


class TestMiningWhitelist:
    def test_mining_whitelist(self) -> None:
        """挖矿白名单 = 状态 + 开矿三件套 + 收矿；刷新/邀请明确不在。"""
        assert set(MINING_ALLOWED_PACKET_IDS.values()) == {21039, 21043, 21045, 21047, 21049}

    def test_sign_in_whitelist(self) -> None:
        """签到白名单 = 拉面板 + 签到 + 连线奖励（★ 21061 已按 10.3 抓包接入）。"""
        assert set(SIGN_IN_ALLOWED_PACKET_IDS.values()) == {21055, 21057, 21061}


class TestHome:
    def test_home_in_alliance(self) -> None:
        """``myAlliance`` 有值 = 已入会（1 allianceSid、2 name、4 level）。"""
        payload = encode_int(1, 1) + encode_message(
            2, encode_int(1, 555) + encode_string(2, "测试工会") + encode_int(4, 3)
        )
        res = AllianceHomeRes.decode(payload)
        assert res.in_alliance()
        assert res.myAlliance is not None
        assert res.myAlliance.name == "测试工会"
        assert res.myAlliance.level == 3

    def test_home_not_in_alliance(self) -> None:
        """没有 ``myAlliance`` 字段 = 未入会（解码为 None）。"""
        res = AllianceHomeRes.decode(encode_int(1, 1))
        assert not res.in_alliance()
        assert res.myAlliance is None


if __name__ == "__main__":  # 支持 `python -m tests.test_models_alliance`
    raise SystemExit(pytest.main([__file__, "-v", "--no-header"]))
