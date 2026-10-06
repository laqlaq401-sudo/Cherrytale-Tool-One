"""2026-10-03 抓包的字节级回归（排错修复的最终钉子）。

【这份测试守什么】
10.3 用 8 份新抓包对照实现，定位出六个功能的根因并修复。本文件把修复点
对应的**官方客户端请求字节**钉死 —— 单元测试证明"逻辑对"，这里证明
**我们发出去的字节与官方客户端一模一样**（防字段编号/编码方式走偏）。

========================  =====================================  ====================
功能                      抓包                                   修复点
========================  =====================================  ====================
每日免费钻石/扫荡券        每日免费钻石和扫荡券领取.saz            37019 的 giftId
素材关卡扫荡              素材关卡扫荡.saz                        useSweepTicket=0
辉煌航迹                  辉煌航迹奖励领取.saz                    34019 查询 + 34021 领取
宴席（免费/付费）          免费+付费宴席.saz                       免费/付费各用一期 groupID
工会签到 + 连线奖励        工会签到+个人挖矿+…saz                  pos 推断 + 21061 接入
工会个人挖矿              同上                                    21047 roleSidList 非空
========================  =====================================  ====================

【数据来源与再生成】
``protocol_samples/{gift,material_sweep,grand_line,bbq,alliance}_samples.py``，
由 ``tools/export_saz_samples.py`` 生成（文件头里写着原样重跑的命令）。
"""

from __future__ import annotations

from typing import Final

from models.alliance import (
    AllianceBingoRewardPacket,
    AllianceBingoRewardRes,
    AllianceBingoSignInPacket,
    AllianceNewPitOneKeyStartPacket,
    AllianceNewPitPacket,
    AllianceNewPitRes,
    AllianceNewPitSetRolePacket,
    AlliancePersonalPitClass,
)
from models.activity_stage import SWEEP_TIME_CHOICES
from models.bbq import BBQFriendsPacket, BBQFriendsRes
from models.grand_line import (
    GrandLineCountRewardPacket,
    GrandLineCountRewardRes,
    GrandLineGetCountRewardPacket,
    GrandLineGetCountRewardRes,
)
from models.gift_package import GetFreeGiftPacket, GetFreeGiftPacketRes
from models.material_stage import SweepSectionPacket, SweepSectionRes
from models.protobuf_wire import fields_by_number
from protocol_samples.alliance_samples import (
    ALLY19_REQUEST_HEX,
    ALLY21_REQUEST_HEX,
    ALLY21_RESPONSE_HEX,
    ALLY23_REQUEST_HEX,
    ALLY23_RESPONSE_HEX,
    ALLY30_REQUEST_HEX,
    ALLY30_RESPONSE_HEX,
    ALLY84_REQUEST_HEX,
    ALLY86_REQUEST_HEX,
)
from protocol_samples.bbq_samples import BBQ09_REQUEST_HEX, BBQ11_REQUEST_HEX
from protocol_samples.gift_samples import GIFT6_REQUEST_HEX, GIFT8_REQUEST_HEX
from protocol_samples.grand_line_samples import (
    GLINE6_REQUEST_HEX,
    GLINE8_REQUEST_HEX,
)
from protocol_samples.material_sweep_samples import MSWEEP08_REQUEST_HEX


def sub(hex_text: str) -> bytes:
    """夹具里的十六进制字符串 → 字节。"""
    return bytes.fromhex(hex_text)


# ---------------------------------------------------------------------------
# 每日免费钻石 / 扫荡券（37019）
# ---------------------------------------------------------------------------
class TestFreeGiftCapture:
    def test_free_gift_request_bytes(self) -> None:
        """两个免费礼包的领取请求：giftId 796012481 / 796010186，逐字节一致。"""
        assert GetFreeGiftPacket.for_gift(796012481).encode() == sub(GIFT6_REQUEST_HEX)
        assert GetFreeGiftPacket.for_gift(796010186).encode() == sub(GIFT8_REQUEST_HEX)

    def test_free_gift_response_parses(self) -> None:
        """响应解码：errorCode=0，gotList 里是扫荡券（200000016）×904。"""
        from protocol_samples.gift_samples import GIFT8_RESPONSE_HEX

        res = GetFreeGiftPacketRes.decode(sub(GIFT8_RESPONSE_HEX))
        assert res.errorCode == 0
        items = [(obj.itemID, obj.itemAmount) for obj in res.gotList for obj in obj.itemList]
        assert (200000016, 904) in items, "每日免费扫荡券礼包"


# ---------------------------------------------------------------------------
# 素材关卡扫荡（11009）
# ---------------------------------------------------------------------------
class TestMaterialSweepCapture:
    def test_sweep_request_matches_client_bytes(self) -> None:
        """★ 核心修复钉子：useSweepTicket=0（素材关不用券）+ 档位 times=5。

        .. note::
           这里断言的是 ``encode()`` 的**默认** ``include_defaults=True``（"全字段"
           形态）。**上线口径**（``tasks/material_stage.py`` 的 11009 发送点必须显式
           ``include_defaults=True``）由
           ``tests/test_material_stage.py::test_sweep_material_include_defaults_per_packet``
           守卫 —— 2026-10-04 的事故正是"模型字节对、上线参数漏传"（实发 7 B）。
        """
        req = SweepSectionPacket(sectionID=129010056, times=5, useSweepTicket=0)
        assert req.encode() == sub(MSWEEP08_REQUEST_HEX)

    def test_captured_request_decodes_to_expected_values(self) -> None:
        """从抓包字节反向解出字段值，防止夹具本身张冠李戴。"""
        req = SweepSectionPacket.decode(sub(MSWEEP08_REQUEST_HEX))
        assert req.sectionID == 129010056
        assert req.times == 5
        assert req.times in SWEEP_TIME_CHOICES
        assert req.useSweepTicket == 0

    def test_sweep_response_rewards_parse(self) -> None:
        """11010 结算：errorCode=0，rewardList 的道具明细能解出来。"""
        from protocol_samples.material_sweep_samples import MSWEEP08_RESPONSE_HEX

        res = SweepSectionRes.decode(sub(MSWEEP08_RESPONSE_HEX))
        assert res.errorCode == 0
        assert len(res.settlement_items()) > 0


# ---------------------------------------------------------------------------
# 辉煌航迹（34019 / 34021）
# ---------------------------------------------------------------------------
class TestGrandLineCapture:
    def test_count_reward_query_is_empty_packet(self) -> None:
        """★ 修复钉子：领取前的查询是空包 34019（不是 34015 主页查询）。"""
        assert GrandLineCountRewardPacket().encode() == b""
        assert sub(GLINE6_REQUEST_HEX) == b""

    def test_claim_request_matches_client_bytes(self) -> None:
        """领取请求 {type=1, rewardID=-1} 与客户端逐字节一致。"""
        req = GrandLineGetCountRewardPacket(type=1, rewardID=-1)
        assert req.encode() == sub(GLINE8_REQUEST_HEX)

    def test_claim_response_parses_rewards(self) -> None:
        """34022：errorCode=0，getObjList 的道具明细可解（GetObjClass.itemList）。"""
        from protocol_samples.grand_line_samples import GLINE8_RESPONSE_HEX

        res = GrandLineGetCountRewardRes.decode(sub(GLINE8_RESPONSE_HEX))
        assert res.errorCode == 0
        amounts = [o.itemAmount for obj in res.getObjList for o in obj.itemList]
        assert len(amounts) >= 2, "抓包里领到了多种道具"

    def test_count_reward_response_catalog(self) -> None:
        """34020 目录：rewardItems 元素 {itemID, amount, isBonus} 可解。"""
        from protocol_samples.grand_line_samples import GLINE6_RESPONSE_HEX

        res = GrandLineCountRewardRes.decode(sub(GLINE6_RESPONSE_HEX))
        assert len(res.rewardItems) == 9
        first = res.rewardItems[0]
        assert first.itemID > 0 and first.amount > 0


# ---------------------------------------------------------------------------
# 宴席（15003 免费 / 付费）
# ---------------------------------------------------------------------------
class TestBBQCapture:
    def test_free_eat_matches_client_bytes(self) -> None:
        """★ 免费吃：groupID=501000004、不带好友 —— 与客户端逐字节一致。"""
        req = BBQFriendsPacket.for_friends(501000004, [])
        assert req.encode() == sub(BBQ11_REQUEST_HEX)

    def test_paid_invite_matches_client_bytes(self) -> None:
        """★ 付费邀请：groupID=501000005 + 4 个好友 playerID，顺序一致。"""
        req = BBQFriendsPacket.for_friends(
            501000005, [6190630, 6314890, 6201009, 6136276]
        )
        assert req.encode() == sub(BBQ09_REQUEST_HEX)

    def test_captured_free_eat_decodes_back(self) -> None:
        """免费吃请求反向解码：groupID=501000004、好友列表为空。"""
        req = BBQFriendsPacket.decode(sub(BBQ11_REQUEST_HEX))
        assert req.groupID == 501000004
        assert req.bbqFriendList == []

    def test_paid_response_add_and_cost(self) -> None:
        """付费邀请响应：addList 给能量、costList 记钻石。"""
        from protocol_samples.bbq_samples import BBQ09_RESPONSE_HEX

        res = BBQFriendsRes.decode(sub(BBQ09_RESPONSE_HEX))
        assert res.errorCode == 0
        added = [o.itemID for obj in res.addList for o in obj.itemList]
        cost = [o.itemID for obj in res.costList for o in obj.itemList]
        assert 200000002 in added, "吃宴席加体力"
        assert 200000001 in cost, "付费邀请扣钻石"


# ---------------------------------------------------------------------------
# 工会签到 + 连线奖励（21057 / 21061 / 21062）
# ---------------------------------------------------------------------------
class TestAllianceSignInCapture:
    def test_sign_in_request_pos_25(self) -> None:
        """★ 修复依据：已占 {0..7,13,19} 时客户端签 pos=25（未占位置可自由选）。"""
        req = AllianceBingoSignInPacket.decode(sub(ALLY19_REQUEST_HEX))
        assert req.pos == 25

    def test_bingo_reward_request_is_empty(self) -> None:
        """★ 接入钉子：连线奖励领取是空包 21061。"""
        assert AllianceBingoRewardPacket().encode() == sub(ALLY21_REQUEST_HEX) == b""

    def test_bingo_reward_response_parses_coins(self) -> None:
        """21062：errorCode=0，奖励明细里有金币（200000050）。"""
        res = AllianceBingoRewardRes.decode(sub(ALLY21_RESPONSE_HEX))
        assert res.errorCode == 0
        item_ids = [o.itemID for obj in res.getObjClass for o in obj.itemList]
        assert 200000050 in item_ids


# ---------------------------------------------------------------------------
# 工会个人挖矿（21039 teachingID=-1 / 21047 roleSidList 非空）
# ---------------------------------------------------------------------------
class TestAllianceMiningCapture:
    def test_new_pit_request_teaching_id_minus_one(self) -> None:
        """★ 修复钉子：21039 非引导请求 teachingID=-1（10.3 抓包实测）。"""
        assert AllianceNewPitPacket().encode() == b"\x08" + b"\xff" * 9 + b"\x01"

    def test_one_key_request_role_lists_are_filled(self) -> None:
        """★ 核心修复钉子：8 个矿点的 roleSidList 全部非空（2/2/4/5/6/6/6/6）。

        人数与 ``AlliancePersonalPitData.roleMaxCondition`` 一一相等
        （935000113/114→2、124→4、127→5、131/132/133/140→6），各坑不重叠。
        """
        packet = AllianceNewPitOneKeyStartPacket.decode(sub(ALLY30_REQUEST_HEX))
        assert packet.updateList == []
        pits = packet.oneKeyPersonalList
        assert [pit.sid for pit in pits] == [
            216252216, 216252218, 216252219, 216252213,
            216252212, 216252214, 216252217, 216252215,
        ]
        sizes = [len(pit.roleSidList) for pit in pits]
        assert sizes == [6, 6, 6, 6, 5, 4, 2, 2]
        all_sids = [sid for pit in pits for sid in pit.roleSidList]
        assert len(all_sids) == len(set(all_sids)), "各坑角色互不重叠"

    def test_one_key_elements_carry_no_end_time(self) -> None:
        """请求元素只带 sid/id/roleSidList（endTime 字段在请求里全为 0）。"""
        packet = AllianceNewPitOneKeyStartPacket.decode(sub(ALLY30_REQUEST_HEX))
        assert all(pit.endTime == 0 for pit in packet.oneKeyPersonalList)
        assert all(pit.isReceived == 0 for pit in packet.oneKeyPersonalList)

    def test_one_key_response_pits_are_mining(self) -> None:
        """21048 响应：errorCode=0，开完后 endTime 变成未来毫秒（挖掘中）。"""
        from protocol_samples.alliance_samples import ALLY30_RESPONSE_HEX

        from models.alliance import AllianceNewPitOneKeyStartRes
        res = AllianceNewPitOneKeyStartRes.decode(sub(ALLY30_RESPONSE_HEX))
        assert res.errorCode == 0
        assert len(res.personalList) == 8
        assert all(pit.endTime > 0 for pit in res.personalList), "开矿后 endTime 为毫秒时间戳"

    def test_personal_pit_round_trip(self) -> None:
        """我们构造的请求元素编码后字段编号不漂移（1 sid、2 id、3 roleSidList）。

        注：``ProtoMessage.encode`` 会把默认值（endTime=0/isReceived=0）也显式
        编出来 —— 抓包里部分请求元素同样带显式 ``20 00 28 00``，服务端接受。
        """
        element = AlliancePersonalPitClass(sid=216252216, id=935000131, roleSidList=[1, 2])
        grouped = fields_by_number(element.encode())
        assert set(grouped) == {1, 2, 3, 4, 5}


# ---------------------------------------------------------------------------
# 工会团体挖矿（21040 空槽哨兵 -1 / 21043 占坑指向）
# ---------------------------------------------------------------------------
class TestAllianceTeamMiningCapture:
    """★ 2026-10-05 钉子：空槽位的 ``memberPid`` 哨兵值是 **-1**，不是 0。

    【这份测试守什么】
    旧判据 ``not slot.memberPid``（等价 ``== 0``）因 ``-1`` 是 truthy 而恒假，
    导致"团体矿永远显示没有可占的坑位"（任务零动作）。本类把三条事实钉死：
    ① 空槽的哨兵值是 -1（且**没有任何槽位**是 0）；
    ② 抓包里两次 21043 打的槽位，在 21040 状态里**恰好都是 -1**；
    ③ 21043 请求字节与官方客户端逐字节一致。
    """

    #: raw/84 占的槽位（sid, index）
    ALLY84_TARGET: Final[tuple[int, int]] = (205294527, 2)
    #: raw/86 占的槽位（sid, index）
    ALLY86_TARGET: Final[tuple[int, int]] = (205294519, 1)

    def _state(self) -> AllianceNewPitRes:
        """10.3 抓包 raw/23 的 21040 状态总表。"""
        res = AllianceNewPitRes.decode(sub(ALLY23_RESPONSE_HEX))
        assert res.errorCode == 0
        return res

    def test_team_state_shape(self) -> None:
        """状态总表规模：18 个团体矿点、4 个未开矿。"""
        state = self._state()
        assert len(state.teamList) == 18
        assert state.personalLimit == 8
        assert state.teamLimit == 4
        assert sum(1 for pit in state.teamList if pit.is_idle()) == 4

    def test_empty_slots_use_minus_one_sentinel(self) -> None:
        """★ 核心钉子：空槽位 ``memberPid == -1``，且**没有**槽位是 0。

        18 个坑共 64 个槽位，其中 7 个空槽（全部落在 4 个未开矿的坑里）；
        其余 57 个是 6 位队友的真实 playerID。
        """
        state = self._state()
        all_slots = [slot for pit in state.teamList for slot in pit.detaliClassList]
        assert len(all_slots) == 64

        empty = [slot for slot in all_slots if slot.memberPid == -1]
        assert len(empty) == 7, "抓包里恰好 7 个空槽"
        assert not any(slot.memberPid == 0 for slot in all_slots), (
            "空槽哨兵是 -1 —— 出现 0 说明约定变了，判据要复核"
        )

        # 空槽全部位于未开矿的坑位（客户端显示 ＋ 号的那些）
        idle_empty = sum(
            1 for pit in state.teamList if pit.is_idle()
            for slot in pit.detaliClassList if slot.memberPid == -1
        )
        assert idle_empty == 7, "7 个空槽全在未开矿的坑里"
        for pit in state.teamList:
            if pit.is_idle():
                continue
            assert all(slot.memberPid > 0 for slot in pit.detaliClassList), (
                f"已开矿的坑 {pit.sid} 不该有空格位"
            )

    def test_empty_slot_member_is_306_byte_sentinel(self) -> None:
        """空槽的 ``member`` 字段不是"空缺"，而是 306 B 的默认头像模板。

        这正是旧实现误以为"空槽位根本没有 memberPid"的原因 —— 记录在，
        只是整条被 -1 哨兵填满（``IconClass.playerID`` 同样是 -1）。
        """
        state = self._state()
        for pit in state.teamList:
            for slot in pit.detaliClassList:
                if slot.memberPid == -1:
                    assert len(slot.member) == 306
                else:
                    assert len(slot.member) < 200, "已占槽位是真实头像"

    def test_join_requests_target_slots_that_were_empty(self) -> None:
        """★ 最强钉子：抓包里 21043 打的两个槽位，在 21040 状态里恰是 -1。

        这是**从抓包反推"客户端认为什么算空"** —— 不用猜哨兵值，
        官方客户端的实际行动直接给出了定义。
        """
        state = self._state()
        by_sid = {pit.sid: pit for pit in state.teamList}
        for sid, index in (self.ALLY84_TARGET, self.ALLY86_TARGET):
            pit = by_sid[sid]
            slot = pit.detaliClassList[index]
            assert slot.memberPid == -1, f"抓包占的槽位 {(sid, index)} 应当是空槽"
            assert index in pit.empty_slot_indices()

    def test_team_join_request_matches_client_bytes(self) -> None:
        """21043 占坑请求 {type=1, pitSid, roleSidList=[sid], teamPitIndex} 逐字节一致。

        对照 raw/84（第 2 槽）与 raw/86（第 1 槽）。
        """
        req84 = AllianceNewPitSetRolePacket.decode(sub(ALLY84_REQUEST_HEX))
        assert (req84.type, req84.pitSid, req84.roleSidList, req84.teamPitIndex) == (
            1, self.ALLY84_TARGET[0], [155242087], self.ALLY84_TARGET[1],
        )

        req86 = AllianceNewPitSetRolePacket.decode(sub(ALLY86_REQUEST_HEX))
        assert (req86.type, req86.pitSid, req86.roleSidList, req86.teamPitIndex) == (
            1, self.ALLY86_TARGET[0], [223860075], self.ALLY86_TARGET[1],
        )

        # 构造侧同字节（含默认值形态 —— 与 tasks/_join_slot 的 include_defaults=True 一致）
        assert (
            AllianceNewPitSetRolePacket(
                type=1, pitSid=205294527, roleSidList=[155242087], teamPitIndex=2
            ).encode()
            == sub(ALLY84_REQUEST_HEX)
        )

    def test_team_state_request_is_teaching_id_minus_one(self) -> None:
        """21039 团体矿状态请求与个人矿同形（teachingID=-1）。"""
        assert AllianceNewPitPacket().encode() == sub(ALLY23_REQUEST_HEX)

    def test_team_slot_count_equals_role_max_condition(self) -> None:
        """★ 槽位数 = 模板表 ``AllianceTeamPitData.roleMaxCondition``（18/18）。

        顺带钉死"槽位数只由难度决定"这个前提 —— 它是排序次键改用
        ``difficulty`` 的依据（两者数值等价，改动只为把语义写进代码）。
        """
        from models.game_config import load_team_pit_conditions

        table = load_team_pit_conditions()
        by_difficulty: dict[int, set[int]] = {}
        for pit in self._state().teamList:
            condition = table.get(pit.id)
            assert condition is not None, f"模板 {pit.id} 不在 AllianceTeamPitData"
            assert condition.role_max == len(pit.detaliClassList)
            by_difficulty.setdefault(condition.difficulty, set()).add(
                len(pit.detaliClassList)
            )
        # 同难度 → 同槽位数（难度 1→2、2→3、3→4、4→5、5→6）
        assert by_difficulty == {1: {2}, 2: {3}, 3: {4}, 4: {5}, 5: {6}}
