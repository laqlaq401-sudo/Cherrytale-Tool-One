"""宴席任务（``tasks/bbq.py``）的单测 —— 重点验证**两步、两个开关**的边界。

运行方式：

    python -m pytest tests/test_tasks_bbq.py -q
    python -m tests.test_tasks_bbq

【本文件最重要的两条断言】

1. **默认（什么都不开）不发 15003**：只读跑完、``ok=True``，
   记录到的请求里只有探路用的 32007；
2. **没授权就一定发不出"花钱的那一发"**：判据是 15003 载荷里的
   ``bbqFriendList`` 是否为空 —— 免费档不带好友（字段 2 为空），
   邀请档才带好友。这样即使将来有人把分支写错，本文件也会立刻变红。

为此把 ``GameClient.post_raw`` 换成"按请求消息号回放响应"的替身，
请求体全部记录下来（与 ``tests/test_mail.py`` 同一套做法）。
"""

from __future__ import annotations

import time
from datetime import datetime

import pytest

import config
from client.exceptions import SpendBlockedError
from client.game_client import GameClient
from client.session import GameSession
from client.spending import SpendKind, SpendPolicy, SpendPurpose
from models.activity_stage import DISPLAY_TZ
from models.bbq import (
    ActivityIntervalDataClass,
    BBQFriendsPacket,
    GetBBQDataRes,
)
from models.envelope import parse_envelope
from models.game_config import (
    TABLE_EXTRA_ENERGY,
    ExtraEnergyTier,
)
from models.game_config import table_path as config_table_path
from models.protobuf_wire import encode_int, encode_message, encode_string
from models.session_state import GameSessionState
from tasks.base import create_task
from tasks.bbq import BBQEnergyTask

# 复用 game_client 测试里的假响应对象，避免两份"手工构造响应"的代码各写各的
from tests.test_game_client import FakeResponse

import tasks.bbq  # noqa: F401  导入即注册（注册发生在模块导入时）

#: 消息号写死在测试里（而不是 import 生产常量）：
#: 这样"协议编号被改动"会在本文件里直接暴露，而不是悄悄跟着一起变。
PACKET_GET_BBQ_DATA = 32007
PACKET_BBQ_DATA_RES = 32008
PACKET_BBQ_FRIENDS = 15003
PACKET_BBQ_FRIENDS_RES = 15004

#: 实测的档位编号（``ExtraEnergyData``，**仅用于估算费用**）：第 N 档的钻石数
# ★ 2026-10-04 语义修订：``number`` 是**含自己**的参与人数 —— 好友数 N 对应
# number=N+1 档；服务端上限 4 位好友（10.3 抓包 = 4 位；发 5 位被拒 -6）。
# 免费吃（0 好友）= number 1 档（0 钻），不在此表（走 _eat_free 分支）。
TIER_BY_FRIEND_COUNT = {1: 7, 2: 15, 3: 24, 4: 31}

#: 服务端真实下发的活动期间 ``groupID``（2026-09-21 真机抓到的实例值）。
#:
#: ⚠ 它和配置表 ``menuID``（500000001~5）**不是同一个编号体系** ——
#: 所以测试里必须把两者写清楚，否则"用 menuID 当 groupID 发出去"这种错会悄悄溜过。
SERVER_GROUP_IDS = [501000004, 501000005]


def make_session() -> GameSession:
    """构造一个**不会联网**的会话（域名是保留域名）。

    ``game_state`` 是业务任务发包的硬要求（``open_game_client()`` 靠它注入令牌）。
    """
    session = GameSession("https://example.invalid", timeout=1.0)
    session.token_store.update(uid="u1", token="tok")
    session.set_game_state(GameSessionState(token="tok", player_id=1, server_id=1))
    return session


# ---------------------------------------------------------------------------
# 报文构造（字段编号见 models/proto_fields.py）
# ---------------------------------------------------------------------------
def friend_bytes(player_id: int, name: str = "好友") -> bytes:
    """``FriendClass``：1 playerID、3 name（本测试只用得上这两个字段）。"""
    return encode_int(1, player_id) + encode_string(3, name)


def bbq_data_payload(group_ids: list[int], friend_ids: list[int]) -> bytes:
    """32008 的载荷。

    ``GetBBQDataRes``：1 eventDeadlineID、2 activityIntervalDataList、3 bbqFriendList；
    ``ActivityIntervalDataClass``：1 dataNummber、2 groupID。
    """
    intervals = b"".join(encode_message(2, encode_int(2, gid)) for gid in group_ids)
    friends = b"".join(encode_message(3, friend_bytes(pid)) for pid in friend_ids)
    return intervals + friends


def interval_bytes(
    *,
    group_id: int,
    weeks: int = 0,
    start_ms: int = 0,
    end_ms: int = 0,
) -> bytes:
    """``ActivityIntervalDataClass`` 的载荷（含窗口与 ``weeks``，供指定顿用例）。

    字段号（见 ``models/bbq.py``）：2 groupID、3 startDateTime、4 endDateTime、5 weeks。
    """
    payload = encode_int(2, group_id)
    if start_ms:
        payload += encode_int(3, start_ms)
    if end_ms:
        payload += encode_int(4, end_ms)
    payload += encode_int(5, weeks)
    return encode_message(2, payload)


def bbq_data_payload_full(
    intervals: list[bytes], friend_ids: list[int]
) -> bytes:
    """32008 载荷（``intervals`` 已是字段 2 的完整子消息）。

    即使没有任何活动期间 / 好友，也返回**非空**字节：空载荷会让 envelope
    的 ``sub_packet_bytes`` 为空而抛 ``EnvelopeError``（生产代码里"一期都没下发"
    是合法响应，形如只有其它字段的消息）。这里用一个显式字段 1 撑住子包。
    """
    friends = b"".join(encode_message(3, friend_bytes(pid)) for pid in friend_ids)
    return encode_int(1, 0) + b"".join(intervals) + friends


def bbq_friends_payload(error_code: int = 0) -> bytes:
    """15004 的载荷：1 errorCode（实测成功码 = 0）。"""
    return encode_int(1, error_code)


def envelope_bytes(packet_id: int, payload: bytes) -> bytes:
    """拼响应字节：字段 1 = 消息号，业务子包槽位编号 = 消息号本身。"""
    return encode_int(1, packet_id) + encode_message(packet_id, payload)


def patch_transport(
    monkeypatch: pytest.MonkeyPatch,
    *,
    group_ids: list[int] | None = None,
    friend_ids: list[int] | None = None,
    error_code: int = 0,
) -> list[bytes]:
    """把 ``GameClient.post_raw`` 换成"记录请求 + 按消息号回放响应"的替身。

    :return: 记录到的请求体列表（断言"到底发了什么"用）。
    """
    recorded: list[bytes] = []
    resolved_groups = list(SERVER_GROUP_IDS) if group_ids is None else group_ids
    resolved_friends = [] if friend_ids is None else friend_ids

    def fake_post_raw(self: GameClient, body: bytes) -> FakeResponse:
        recorded.append(body)
        packet_id = parse_envelope(body).packet_id
        if packet_id == PACKET_GET_BBQ_DATA:
            return FakeResponse(
                envelope_bytes(
                    PACKET_BBQ_DATA_RES,
                    bbq_data_payload(resolved_groups, resolved_friends),
                )
            )
        if packet_id == PACKET_BBQ_FRIENDS:
            return FakeResponse(
                envelope_bytes(PACKET_BBQ_FRIENDS_RES, bbq_friends_payload(error_code))
            )
        raise AssertionError(f"测试替身收到了意料之外的请求：{packet_id}")

    monkeypatch.setattr(GameClient, "post_raw", fake_post_raw)
    return recorded


def sent_eat_packets(recorded: list[bytes]) -> list[BBQFriendsPacket]:
    """从记录到的请求体里解出所有 15003 的载荷（忽略探路用的 32007）。

    直接复用生产模型解码，顺带证明"字段编号仍然对得上"。
    """
    packets: list[BBQFriendsPacket] = []
    for body in recorded:
        view = parse_envelope(body)
        if view.packet_id == PACKET_BBQ_FRIENDS:
            packets.append(BBQFriendsPacket.decode(view.sub_packet_bytes))
    return packets


def paid_policy() -> SpendPolicy:
    """两个开关**都开**的策略：总闸 + 用途白名单。"""
    return SpendPolicy(
        allow_diamond=True, allowed_purposes=frozenset({SpendPurpose.BBQ_INVITE})
    )


def has_extra_energy_table() -> bool:
    """本地是否可读到 ``ExtraEnergyData``（缺素材时跳过与档位有关的用例）。"""
    try:
        return config_table_path(TABLE_EXTRA_ENERGY).is_file()
    except (FileNotFoundError, OSError):
        return False


#: 与档位表有关的用例统一挂这个标记：换电脑（没有素材）时自动跳过而不是变红。
require_table = pytest.mark.skipif(
    not has_extra_energy_table(), reason="本地没有 ExtraEnergyData 配置表"
)


def make_tier(menu_id: int, number: int, amount: int, use_item_id: int | None = None) -> ExtraEnergyTier:
    """造一个档位对象（不依赖本地配置表）。"""
    return ExtraEnergyTier(
        menu_id=menu_id,
        number=number,
        asset_id=f"Activity_Eat_food{number}",
        use_item_id=use_item_id,
        item_amount=amount,
        raw={},  # type: ignore[arg-type]  本测试不读原始行
    )


# ---------------------------------------------------------------------------
# ① 默认（什么都不开）：只读
# ---------------------------------------------------------------------------
class TestReadOnly:
    """默认什么开关都不开时：只读跑完，**一个 15003 都不发**。"""

    def test_only_probe_request_is_sent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded = patch_transport(monkeypatch, friend_ids=[11, 22, 33])
        task = create_task("bbq_energy", make_session(), spend_policy=SpendPolicy())
        result = task.run()

        assert result.ok is True
        assert result.data["mode"] == "read_only"
        assert sent_eat_packets(recorded) == []  # ← 本文件的核心断言
        assert [parse_envelope(body).packet_id for body in recorded] == [
            PACKET_GET_BBQ_DATA
        ]

    @require_table
    def test_reports_planned_invite(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """只读也要说清"如果开邀请会发生什么"（金额来自配置表，不是猜的）。"""
        patch_transport(monkeypatch, group_ids=SERVER_GROUP_IDS, friend_ids=list(range(1, 8)))
        task = create_task("bbq_energy", make_session(), spend_policy=SpendPolicy())
        result = task.run()

        assert result.ok is True
        assert result.data["planned_diamond"] == TIER_BY_FRIEND_COUNT[4]  # 最大档 31（4 位好友）
        assert result.data["friend_count"] == 7
        # groupID 必须来自服务端（真机实证：和配置表 menuID 是两套编号）
        assert result.data["group_id"] == SERVER_GROUP_IDS[-1]
        assert "31" in result.message


# ---------------------------------------------------------------------------
# ② 只开 --eat：免费档（**与消费闸门无关**）
# ---------------------------------------------------------------------------
class TestFreeTier:
    """步骤②：吃宴席本身不花钱，因此不该受闸门管辖。"""

    @require_table
    def test_sends_empty_friend_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded = patch_transport(monkeypatch, group_ids=SERVER_GROUP_IDS, friend_ids=[11, 22])
        task = create_task(
            "bbq_energy", make_session(), spend_policy=SpendPolicy(), eat=True
        )
        result = task.run()

        assert result.ok is True
        packets = sent_eat_packets(recorded)
        assert len(packets) == 1
        # groupID 取自服务端下发的活动期间（不是配置表 menuID —— 真机实证）
        # ★ 2026-10-03：免费吃锚定 weeks==0 的第一期（10.3 抓包 = 501000004）
        assert packets[0].groupID == SERVER_GROUP_IDS[0]
        assert packets[0].bbqFriendList == []  # 不带好友 → 不可能花钱
        assert result.data["diamond_spent"] == 0
        assert task.spend_policy.spent_so_far == 0

    @require_table
    def test_ignores_spend_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """总闸关着也照样能吃 —— 这正是"步骤②与闸门无关"的证据。"""
        patch_transport(monkeypatch, group_ids=SERVER_GROUP_IDS, friend_ids=[])
        task = create_task(
            "bbq_energy",
            make_session(),
            spend_policy=SpendPolicy(allow_diamond=False),
            eat=True,
        )
        result = task.run()
        assert result.ok is True
        assert result.data["mode"] == "free"
        # ★ 2026-10-03：免费吃锚定 weeks==0 的第一期（10.3 抓包）
        assert result.data["group_id"] == SERVER_GROUP_IDS[0]

    def test_no_activity_interval_refuses_to_guess(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """服务端没下发活动期间（groupID）→ **拒绝发送**（宁可什么都不做，也不猜编号）。"""
        recorded = patch_transport(monkeypatch, group_ids=[], friend_ids=[1, 2])
        task = create_task(
            "bbq_energy", make_session(), spend_policy=SpendPolicy(), eat=True
        )
        result = task.run()

        assert result.ok is False
        assert sent_eat_packets(recorded) == []


# ---------------------------------------------------------------------------
# ③ 邀请好友：两道门**都开**才放行
# ---------------------------------------------------------------------------
class _LiePolicy(SpendPolicy):
    """测试替身：先说"已授权"（让任务进入邀请分支），真正申请时却拒绝。

    用来验证"闸门在计划之后才拒绝"这种意外情况下，任务既不崩溃、也不发包。
    """

    def allows_purpose(self, purpose: SpendPurpose) -> bool:  # type: ignore[override]
        return True

    def check(self, **kwargs: object) -> "SpendPolicy":  # type: ignore[override]
        raise SpendBlockedError("测试替身：闸门拒绝")


class TestInviteNeedsBothSwitches:
    @require_table
    def test_purpose_without_master_switch_stays_free(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """只授权用途、不开总闸 → 仍走免费档（与关系）。"""
        recorded = patch_transport(
            monkeypatch, group_ids=SERVER_GROUP_IDS, friend_ids=list(range(1, 8))
        )
        policy = SpendPolicy(
            allow_diamond=False,
            allowed_purposes=frozenset({SpendPurpose.BBQ_INVITE}),
        )
        task = create_task("bbq_energy", make_session(), spend_policy=policy, eat=True)
        result = task.run()

        assert result.ok is True
        assert sent_eat_packets(recorded)[0].bbqFriendList == []
        assert task.spend_policy.spent_so_far == 0

    @require_table
    def test_master_switch_without_purpose_stays_free(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """"允许花钻石"≠"允许这个用途"：只开总闸也只能吃免费档。"""
        recorded = patch_transport(
            monkeypatch, group_ids=SERVER_GROUP_IDS, friend_ids=list(range(1, 8))
        )
        task = create_task(
            "bbq_energy",
            make_session(),
            spend_policy=SpendPolicy(allow_diamond=True),
            eat=True,
        )
        result = task.run()

        assert result.ok is True
        assert sent_eat_packets(recorded)[0].bbqFriendList == []
        assert task.spend_policy.spent_so_far == 0

    @require_table
    def test_both_switches_invite_max_tier_with_cap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """两道门都开 → 最高档、最多 4 位好友（★ 实测上限，7 位也不多请）。"""
        recorded = patch_transport(
            monkeypatch, group_ids=SERVER_GROUP_IDS, friend_ids=list(range(1, 8))
        )
        task = create_task(
            "bbq_energy", make_session(), spend_policy=paid_policy(), eat=True
        )
        result = task.run()

        packets = sent_eat_packets(recorded)
        assert len(packets) == 1
        assert packets[0].groupID == SERVER_GROUP_IDS[-1]
        assert packets[0].bbqFriendList == [1, 2, 3, 4]  # ★ 上限 4 位（number=5 档）
        assert task.spend_policy.spent_so_far == TIER_BY_FRIEND_COUNT[4]
        assert result.data["mode"] == "invite"

    @require_table
    def test_fewer_friends_uses_smaller_tier(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """好友只有 3 位 → number=4 档（24 钻；★ number 含自己，不再错位一档）。"""
        patch_transport(monkeypatch, group_ids=SERVER_GROUP_IDS, friend_ids=[7, 8, 9])
        task = create_task(
            "bbq_energy", make_session(), spend_policy=paid_policy(), eat=True
        )
        task.run()
        assert task.spend_policy.spent_so_far == TIER_BY_FRIEND_COUNT[3]

    @require_table
    def test_unexpected_gate_refusal_is_graceful(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """闸门在计划之后才拒绝 → 跳过邀请（花钱那一发从未被构造），任务不崩。"""
        recorded = patch_transport(
            monkeypatch, group_ids=SERVER_GROUP_IDS, friend_ids=list(range(1, 8))
        )
        task = create_task(
            "bbq_energy", make_session(), spend_policy=_LiePolicy(), eat=True
        )
        result = task.run()

        assert result.ok is True
        assert result.data["mode"] == "invite_blocked"
        assert sent_eat_packets(recorded) == []


# ---------------------------------------------------------------------------
# ④ 档位策略（纯逻辑，不依赖本地配置表）
# ---------------------------------------------------------------------------
class TestTierPolicy:
    """只走最大档；好友不足时退到人数对应的档；逃生门优先。"""

    TIERS = [
        make_tier(500000001, 1, 0),
        make_tier(500000002, 2, 7),
        make_tier(500000003, 3, 15),
        make_tier(500000004, 4, 24),
        make_tier(500000005, 5, 31),
    ]

    def test_picks_exact_tier_by_friend_count(self) -> None:
        """★ number 含自己：好友数 N → number=N+1 档（2026-10-04 实测语义）。"""
        for count, expected in TIER_BY_FRIEND_COUNT.items():
            tier = BBQEnergyTask._pick_tier(self.TIERS, count)
            assert tier is not None
            assert tier.item_amount == expected
            assert tier.number == count + 1

    def test_falls_back_to_lower_tier(self) -> None:
        """配置表缺少正好对应人数的档时，退到 ≤ 目标 number 的最大档。"""
        tiers = [make_tier(500000001, 1, 0), make_tier(500000003, 3, 15)]
        tier = BBQEnergyTask._pick_tier(tiers, 4)  # 目标 number=5，表里最大 3
        assert tier is not None
        assert tier.number == 3

    def test_no_tiers_returns_none(self) -> None:
        assert BBQEnergyTask._pick_tier([], 3) is None

    def test_escape_hatch_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``config.BBQ_MENU_ID`` 是逃生门：显式指定后不再按人数挑（只影响费用估算）。"""
        monkeypatch.setattr(config, "BBQ_MENU_ID", 500000002)
        tier = BBQEnergyTask._pick_tier(self.TIERS, 5)
        assert tier is not None
        assert tier.menu_id == 500000002


# ---------------------------------------------------------------------------
# ④.5 本期活动 groupID 的选择（真机实证：它与配置表 menuID 是两套编号）
# ---------------------------------------------------------------------------
class TestCurrentGroupId:
    """``15003.groupID`` 只能来自 32008 的活动期间，**不能**用配置表 menuID。

    ★ 2026-10-03 规则修订（10.3 双抓包）：时间戳为毫秒；免费吃锚定
    ``weeks == 0`` 的那一期，付费邀请取覆盖当前时刻的那一期。
    """

    def test_picks_interval_covering_now_ms(self) -> None:
        """付费邀请：时间窗按**毫秒**比较（旧代码用秒比较永远匹配不上）。"""
        now_ms = int(time.time() * 1000)
        data = GetBBQDataRes(
            activityIntervalDataList=[
                ActivityIntervalDataClass(
                    groupID=501000004,
                    startDateTime=now_ms - 86_400_000,
                    endDateTime=now_ms + 86_400_000,
                ),
                ActivityIntervalDataClass(
                    groupID=501000005,
                    startDateTime=now_ms + 7_200_000,
                    endDateTime=now_ms + 10_800_000,
                ),
            ]
        )
        assert BBQEnergyTask._current_group_id(data) == 501000004

    def test_free_eat_anchors_to_weeks_zero(self) -> None:
        """免费吃：取 ``weeks == 0`` 的第一期 —— 即使它尚未开始（10.3 抓包实况）。

        抓包里 501000004（weeks=0）在捕获时刻还没开始（06:10 才开始），
        真实客户端照样对它发免费吃并成功；501000005（weeks=2）才是当期。
        """
        now_ms = int(time.time() * 1000)
        data = GetBBQDataRes(
            activityIntervalDataList=[
                ActivityIntervalDataClass(
                    groupID=501000004,
                    startDateTime=now_ms + 20_000_000,  # 还没开始
                    endDateTime=now_ms + 50_000_000,
                    weeks=0,
                ),
                ActivityIntervalDataClass(
                    groupID=501000005,
                    startDateTime=now_ms - 40_000_000,
                    endDateTime=now_ms + 20_000_000,
                    weeks=2,
                ),
            ]
        )
        assert BBQEnergyTask._current_group_id(data, for_free=True) == 501000004
        # 同一份数据下，付费邀请仍落在覆盖当前的那一期
        assert BBQEnergyTask._current_group_id(data) == 501000005

    def test_free_eat_falls_back_to_first_without_weeks_zero(self) -> None:
        """没有 weeks==0 的期时，免费吃退回第一期（不猜编号）。"""
        data = GetBBQDataRes(
            activityIntervalDataList=[
                ActivityIntervalDataClass(groupID=501000004, weeks=1),
                ActivityIntervalDataClass(groupID=501000005, weeks=2),
            ]
        )
        assert BBQEnergyTask._current_group_id(data, for_free=True) == 501000004

    def test_falls_back_to_last_interval(self) -> None:
        """时间窗未下发（值为 0）时退回服务端顺序里的最后一个。"""
        data = GetBBQDataRes(
            activityIntervalDataList=[
                ActivityIntervalDataClass(groupID=501000004),
                ActivityIntervalDataClass(groupID=501000005),
            ]
        )
        assert BBQEnergyTask._current_group_id(data) == 501000005

    def test_returns_none_without_intervals(self) -> None:
        assert BBQEnergyTask._current_group_id(GetBBQDataRes()) is None
        assert BBQEnergyTask._current_group_id(GetBBQDataRes(), for_free=True) is None

    def test_never_uses_menu_id(self) -> None:
        """把"协议 groupID ≠ 配置表 menuID"这件事钉死（真机 5010000xx vs 5000000x）。"""
        data = GetBBQDataRes(
            activityIntervalDataList=[ActivityIntervalDataClass(groupID=501000005)]
        )
        assert BBQEnergyTask._current_group_id(data) not in {
            tier.menu_id for tier in TestTierPolicy.TIERS
        }


# ---------------------------------------------------------------------------
# ⑤ 消费闸门的"用途"语义（client/spending.py）
# ---------------------------------------------------------------------------
class TestSpendPurposeGate:
    """总闸与用途白名单是**与**关系，且旧调用（不带 purpose）行为不变。"""

    def test_purpose_without_master_switch_is_denied(self) -> None:
        policy = SpendPolicy(allowed_purposes=frozenset({SpendPurpose.BBQ_INVITE}))
        assert policy.allows_purpose(SpendPurpose.BBQ_INVITE) is False
        with pytest.raises(SpendBlockedError):
            policy.check(
                kind=SpendKind.DIAMOND,
                amount=31,
                reason="测试",
                purpose=SpendPurpose.BBQ_INVITE,
            )

    def test_master_switch_alone_is_denied(self) -> None:
        policy = SpendPolicy(allow_diamond=True)
        assert policy.allows_purpose(SpendPurpose.BBQ_INVITE) is False
        with pytest.raises(SpendBlockedError):
            policy.check(
                kind=SpendKind.DIAMOND,
                amount=31,
                reason="测试",
                purpose=SpendPurpose.BBQ_INVITE,
            )

    def test_both_switches_allow_and_charge(self) -> None:
        charged = paid_policy().check(
            kind=SpendKind.DIAMOND,
            amount=31,
            reason="测试",
            purpose=SpendPurpose.BBQ_INVITE,
        )
        assert charged.spent_so_far == 31
        assert charged.allows_purpose(SpendPurpose.BBQ_INVITE) is True

    def test_legacy_call_without_purpose_keeps_old_behaviour(self) -> None:
        """不带 purpose 的旧调用只受总闸管辖 —— 保证既有任务不被本次改造误伤。"""
        assert (
            SpendPolicy(allow_diamond=True)
            .check(kind=SpendKind.DIAMOND, amount=7, reason="旧调用")
            .spent_so_far
            == 7
        )
        with pytest.raises(SpendBlockedError):
            SpendPolicy().check(kind=SpendKind.DIAMOND, amount=7, reason="旧调用")


# ---------------------------------------------------------------------------
# ⑥ 指定顿入口（``meal``）：窗口判定 + 只吃该顿 + 服务端记账
# ---------------------------------------------------------------------------
def patch_transport_meal(
    monkeypatch: pytest.MonkeyPatch,
    *,
    now_ms: int,
    morning: tuple[int, int, int] | None = None,
    evening: tuple[int, int, int] | None = None,
    friend_ids: list[int] | None = None,
    error_code: int = 0,
) -> list[bytes]:
    """指定顿用例的传输替身：下发带 ``weeks`` / 窗口的两期活动。

    :param morning: ``(group_id, start_ms, end_ms)`` 早宴席（``weeks=0``）。
    :param evening: ``(group_id, start_ms, end_ms)`` 晚宴席（``weeks=2``）。
    :param now_ms: 用来把 ``time.time()`` 固定到测试"现在"（窗口判定靠它）。
    """
    recorded: list[bytes] = []
    intervals: list[bytes] = []
    if morning is not None:
        gid, start, end = morning
        intervals.append(interval_bytes(group_id=gid, weeks=0, start_ms=start, end_ms=end))
    if evening is not None:
        gid, start, end = evening
        intervals.append(interval_bytes(group_id=gid, weeks=2, start_ms=start, end_ms=end))
    payload = bbq_data_payload_full(intervals, [] if friend_ids is None else friend_ids)

    def fake_post_raw(self: GameClient, body: bytes) -> FakeResponse:
        recorded.append(body)
        packet_id = parse_envelope(body).packet_id
        if packet_id == PACKET_GET_BBQ_DATA:
            return FakeResponse(envelope_bytes(PACKET_BBQ_DATA_RES, payload))
        if packet_id == PACKET_BBQ_FRIENDS:
            return FakeResponse(
                envelope_bytes(PACKET_BBQ_FRIENDS_RES, bbq_friends_payload(error_code))
            )
        raise AssertionError(f"测试替身收到了意料之外的请求：{packet_id}")

    monkeypatch.setattr(GameClient, "post_raw", fake_post_raw)
    monkeypatch.setattr(tasks.bbq.time, "time", lambda: now_ms / 1000)
    return recorded


@pytest.fixture
def banquet_file(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """把宴席记账写到临时文件，并要求"有真实身份"（server_id / player_id 都 > 0）。

    ``identity_key`` 在两者都 <= 0 时退化成 ``<no-session>``，那样写入会被
    ``mark_meal_done`` 主动跳过 —— 记账类用例必须给一个真身份。
    """
    path = tmp_path / "banquet.json"
    monkeypatch.setattr(config, "BANQUET_STATE_FILE", path)
    monkeypatch.setattr(
        tasks.bbq.BBQEnergyTask,
        "_session_info",
        staticmethod(lambda: {"server_id": 12, "player_id": 345678}),
    )
    return path


HOUR_MS = 3_600_000
SERVER_GID_MORNING = 501000004
SERVER_GID_EVENING = 501000005

#: 空档期内的一个时刻（2026-10-03 06:05，UTC+8）。
#: 空档期 = 每天 06:00–06:10（晚宴席已结束、早宴席 06:10 才刷新），两顿都不可吃。
GAP_MS = int(datetime(2026, 10, 3, 6, 5, tzinfo=DISPLAY_TZ).timestamp() * 1000)


class TestMealMode:
    """「早宴席 / 晚宴席」两个按钮背后的逻辑。"""

    # —— 窗口判定（★ 2026-10-06 第七版：早宴席不看窗口，窗口只约束邀请）——
    def test_morning_before_window_still_eats(
        self, monkeypatch: pytest.MonkeyPatch, banquet_file
    ) -> None:
        """早宴席**窗口未到**也照吃（第七版：吃不由窗口管）。

        真实场景：凌晨 00:39（仍属前一宴席日）点早宴席 = **补吃**前一宴席日那一顿；
        而服务端此时下发的是"下一次"窗口，看起来就是 ``not_started``。
        抓包实证（``过时间无法用钻石邀请的宴席.saz``）：该场景下发
        ``15003{501000004, 无好友}`` → ``errorCode=0``。
        """
        now = 1_000_000_000_000
        recorded = patch_transport_meal(
            monkeypatch,
            now_ms=now,
            morning=(SERVER_GID_MORNING, now + HOUR_MS, now + 2 * HOUR_MS),
            evening=(SERVER_GID_EVENING, now + 3 * HOUR_MS, now + 4 * HOUR_MS),
        )
        task = create_task("bbq_energy", make_session(), eat=True, meal="morning")
        result = task.run()

        assert result.ok is True
        assert result.data["mode"] == "meal_free"
        packets = sent_eat_packets(recorded)
        assert len(packets) == 1
        assert packets[0].groupID == SERVER_GID_MORNING
        assert packets[0].bbqFriendList == []

    def test_morning_after_window_still_eats(
        self, monkeypatch: pytest.MonkeyPatch, banquet_file
    ) -> None:
        """早宴席**窗口已过**仍照吃（补吃）—— 这正是用户 2026-10-06 更正的场景。"""
        now = 1_000_000_000_000
        recorded = patch_transport_meal(
            monkeypatch,
            now_ms=now,
            morning=(SERVER_GID_MORNING, now - 2 * HOUR_MS, now - HOUR_MS),
            evening=(SERVER_GID_EVENING, now + HOUR_MS, now + 2 * HOUR_MS),
        )
        task = create_task("bbq_energy", make_session(), eat=True, meal="morning")
        result = task.run()

        assert result.ok is True
        assert result.data["mode"] == "meal_free"
        assert len(sent_eat_packets(recorded)) == 1
        assert banquet_file.is_file(), "补吃也要记账（否则会重复发包）"

    @require_table
    def test_morning_after_window_forces_free_despite_invite_switch(
        self, monkeypatch: pytest.MonkeyPatch, banquet_file
    ) -> None:
        """★ 过点后**即使邀请开关开着**也强制不邀请（用户 2026-10-06 拍板）。

        断言两件事：① 仍然照吃（发出包）；② 包里**一个好友都没有**（邀请被压掉）。
        """
        now = 1_000_000_000_000
        recorded = patch_transport_meal(
            monkeypatch,
            now_ms=now,
            morning=(SERVER_GID_MORNING, now - 2 * HOUR_MS, now - HOUR_MS),
            evening=(SERVER_GID_EVENING, now + HOUR_MS, now + 2 * HOUR_MS),
            friend_ids=list(range(1, 8)),
        )
        task = create_task(
            "bbq_energy",
            make_session(),
            spend_policy=paid_policy(),
            eat=True,
            meal="morning",
            invite=True,  # ← 用户把邀请开关打开了
        )
        result = task.run()

        packets = sent_eat_packets(recorded)
        assert len(packets) == 1
        assert packets[0].groupID == SERVER_GID_MORNING
        assert packets[0].bbqFriendList == []  # ← 强制不邀请
        assert result.data["mode"] == "meal_free"
        assert result.data["invite_blocked_by_window"] is True
        assert task.spend_policy.spent_so_far == 0  # 一分钱没花

    def test_evening_before_window_sends_nothing(
        self, monkeypatch: pytest.MonkeyPatch, banquet_file
    ) -> None:
        """★ 晚宴席**不放宽**：窗口未到就是一个包都不发。"""
        now = 1_000_000_000_000
        recorded = patch_transport_meal(
            monkeypatch,
            now_ms=now,
            morning=(SERVER_GID_MORNING, now - 3 * HOUR_MS, now - 2 * HOUR_MS),
            evening=(SERVER_GID_EVENING, now + HOUR_MS, now + 2 * HOUR_MS),
        )
        result = create_task("bbq_energy", make_session(), eat=True, meal="evening").run()

        assert result.ok is True
        assert result.data["mode"] == "meal_window_closed"
        assert result.data["window_state"] == "not_started"
        assert sent_eat_packets(recorded) == []

    def test_evening_after_window_sends_nothing(
        self, monkeypatch: pytest.MonkeyPatch, banquet_file
    ) -> None:
        """★ 晚宴席**不放宽**：窗口已过也是一个包都不发（防"顺手放宽"）。"""
        now = 1_000_000_000_000
        recorded = patch_transport_meal(
            monkeypatch,
            now_ms=now,
            morning=(SERVER_GID_MORNING, now - 3 * HOUR_MS, now - 2 * HOUR_MS),
            evening=(SERVER_GID_EVENING, now - 2 * HOUR_MS, now - HOUR_MS),
        )
        result = create_task("bbq_energy", make_session(), eat=True, meal="evening").run()

        assert result.data["mode"] == "meal_window_closed"
        assert result.data["window_state"] == "ended"
        assert sent_eat_packets(recorded) == []

    # —— 空档期 06:00–06:10：两顿都不可吃（用户 2026-10-06 确认）——
    def test_gap_sends_nothing_for_morning(
        self, monkeypatch: pytest.MonkeyPatch, banquet_file
    ) -> None:
        """★ 空档期连早宴席也不发 —— 哪怕服务端把窗口列成"覆盖中"。"""
        recorded = patch_transport_meal(
            monkeypatch,
            now_ms=GAP_MS,
            morning=(SERVER_GID_MORNING, GAP_MS - HOUR_MS, GAP_MS + HOUR_MS),
            evening=(SERVER_GID_EVENING, GAP_MS - HOUR_MS, GAP_MS + HOUR_MS),
        )
        result = create_task("bbq_energy", make_session(), eat=True, meal="morning").run()

        assert result.ok is True
        assert result.data["mode"] == "banquet_gap"
        assert sent_eat_packets(recorded) == []

    def test_gap_sends_nothing_for_evening(
        self, monkeypatch: pytest.MonkeyPatch, banquet_file
    ) -> None:
        recorded = patch_transport_meal(
            monkeypatch,
            now_ms=GAP_MS,
            morning=(SERVER_GID_MORNING, GAP_MS - HOUR_MS, GAP_MS + HOUR_MS),
            evening=(SERVER_GID_EVENING, GAP_MS - HOUR_MS, GAP_MS + HOUR_MS),
        )
        result = create_task("bbq_energy", make_session(), eat=True, meal="evening").run()

        assert result.data["mode"] == "banquet_gap"
        assert sent_eat_packets(recorded) == []

    def test_just_after_gap_morning_is_eatable(
        self, monkeypatch: pytest.MonkeyPatch, banquet_file
    ) -> None:
        """空档边界另一侧：06:10（早宴席刷新）起就不再是空档。"""
        after = GAP_MS + 5 * 60_000  # 06:10
        recorded = patch_transport_meal(
            monkeypatch,
            now_ms=after,
            morning=(SERVER_GID_MORNING, after - HOUR_MS, after + HOUR_MS),
            evening=(SERVER_GID_EVENING, after + HOUR_MS, after + 2 * HOUR_MS),
        )
        result = create_task("bbq_energy", make_session(), eat=True, meal="morning").run()

        assert result.data["mode"] == "meal_free"
        assert len(sent_eat_packets(recorded)) == 1

    def test_covering_window_sends_that_meal_group(
        self, monkeypatch: pytest.MonkeyPatch, banquet_file
    ) -> None:
        """窗口覆盖 → 发**该顿自己的** groupID（早宴席发 501000004）。"""
        now = 1_000_000_000_000
        recorded = patch_transport_meal(
            monkeypatch,
            now_ms=now,
            morning=(SERVER_GID_MORNING, now - HOUR_MS, now + HOUR_MS),
            evening=(SERVER_GID_EVENING, now + 2 * HOUR_MS, now + 3 * HOUR_MS),
        )
        task = create_task("bbq_energy", make_session(), eat=True, meal="morning")
        result = task.run()

        packets = sent_eat_packets(recorded)
        assert len(packets) == 1
        assert packets[0].groupID == SERVER_GID_MORNING
        assert packets[0].bbqFriendList == []
        assert result.data["mode"] == "meal_free"
        assert banquet_file.is_file()

    def test_evening_uses_evening_group(
        self, monkeypatch: pytest.MonkeyPatch, banquet_file
    ) -> None:
        now = 1_000_000_000_000
        recorded = patch_transport_meal(
            monkeypatch,
            now_ms=now,
            morning=(SERVER_GID_MORNING, now - 3 * HOUR_MS, now - 2 * HOUR_MS),
            evening=(SERVER_GID_EVENING, now - HOUR_MS, now + HOUR_MS),
        )
        task = create_task("bbq_energy", make_session(), eat=True, meal="evening")
        result = task.run()
        assert sent_eat_packets(recorded)[0].groupID == SERVER_GID_EVENING
        assert result.data["mode"] == "meal_free"

    # —— 具体顿的邀请授权 ——
    @require_table
    def test_invite_in_covering_window_uses_that_meal_group(
        self, monkeypatch: pytest.MonkeyPatch, banquet_file
    ) -> None:
        """早宴席在自己窗口内**可以**邀请（用户明确纠正过的一点）。"""
        now = 1_000_000_000_000
        recorded = patch_transport_meal(
            monkeypatch,
            now_ms=now,
            morning=(SERVER_GID_MORNING, now - HOUR_MS, now + HOUR_MS),
            evening=(SERVER_GID_EVENING, now + HOUR_MS, now + 2 * HOUR_MS),
            friend_ids=list(range(1, 8)),
        )
        task = create_task(
            "bbq_energy", make_session(), spend_policy=paid_policy(), eat=True, meal="morning"
        )
        result = task.run()

        packets = sent_eat_packets(recorded)
        assert len(packets) == 1
        assert packets[0].groupID == SERVER_GID_MORNING  # 用它自己的期，不是覆盖期
        assert packets[0].bbqFriendList == [1, 2, 3, 4]
        assert result.data["mode"] == "meal_invite"

    @require_table
    def test_no_invite_auth_in_window_falls_to_free(
        self, monkeypatch: pytest.MonkeyPatch, banquet_file
    ) -> None:
        """窗口开着但邀请未授权 → 免费档（用户要的"第二顿默认不邀请"）。"""
        now = 1_000_000_000_000
        recorded = patch_transport_meal(
            monkeypatch,
            now_ms=now,
            morning=(SERVER_GID_MORNING, now - 2 * HOUR_MS, now - HOUR_MS),
            evening=(SERVER_GID_EVENING, now - HOUR_MS, now + HOUR_MS),
            friend_ids=list(range(1, 8)),
        )
        task = create_task(
            "bbq_energy",
            make_session(),
            spend_policy=SpendPolicy(allow_diamond=True),  # 总闸开、用途未授权
            eat=True,
            meal="evening",
        )
        result = task.run()

        assert sent_eat_packets(recorded)[0].bbqFriendList == []
        assert result.data["mode"] == "meal_free"
        assert task.spend_policy.spent_so_far == 0

    # —— 记账：吃过就不再发 ——
    def test_second_call_same_day_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch, banquet_file
    ) -> None:
        now = 1_000_000_000_000
        patch_transport_meal(
            monkeypatch,
            now_ms=now,
            morning=(SERVER_GID_MORNING, now - HOUR_MS, now + HOUR_MS),
            evening=(SERVER_GID_EVENING, now + HOUR_MS, now + 2 * HOUR_MS),
        )
        task = create_task("bbq_energy", make_session(), eat=True, meal="morning")
        first = task.run()
        assert first.data["mode"] == "meal_free"

        recorded = patch_transport_meal(
            monkeypatch,
            now_ms=now,
            morning=(SERVER_GID_MORNING, now - HOUR_MS, now + HOUR_MS),
            evening=(SERVER_GID_EVENING, now + HOUR_MS, now + 2 * HOUR_MS),
        )
        again = create_task("bbq_energy", make_session(), eat=True, meal="morning").run()
        assert again.data["mode"] == "meal_already_done"
        assert sent_eat_packets(recorded) == []  # ← 记账生效：零发包

    def test_two_meals_are_recorded_independently(
        self, monkeypatch: pytest.MonkeyPatch, banquet_file
    ) -> None:
        """吃完早宴席，晚宴席**不受影响**（两顿各自独立）。

        两段窗口都覆盖 ``now``：这是真实世界的常态 —— 晚宴席 14:00–次日06:00
        横跨整天大部分时间，早宴席 06:10–13:59 与其并不重叠于"同一时刻"，
        但这里只验证**记账隔离**，所以让两段都覆盖 now 更严格
        （若实现错误地"吃过一顿就锁死两顿"，本用例会直接变红）。
        """
        now = 1_000_000_000_000
        windows = dict(
            morning=(SERVER_GID_MORNING, now - HOUR_MS, now + HOUR_MS),
            evening=(SERVER_GID_EVENING, now - HOUR_MS, now + HOUR_MS),
        )
        patch_transport_meal(monkeypatch, now_ms=now, **windows)
        create_task("bbq_energy", make_session(), eat=True, meal="morning").run()

        recorded = patch_transport_meal(monkeypatch, now_ms=now, **windows)
        evening = create_task("bbq_energy", make_session(), eat=True, meal="evening").run()
        assert evening.data["mode"] == "meal_free"
        assert evening.data["group_id"] == SERVER_GID_EVENING
        assert len(sent_eat_packets(recorded)) == 1

    # —— 只读与非法值 ——
    def test_eat_false_still_read_only(
        self, monkeypatch: pytest.MonkeyPatch, banquet_file
    ) -> None:
        """指定顿 + ``eat=False``：窗口开着也不发包（保住"开关模块"的本意）。"""
        now = 1_000_000_000_000
        recorded = patch_transport_meal(
            monkeypatch,
            now_ms=now,
            morning=(SERVER_GID_MORNING, now - HOUR_MS, now + HOUR_MS),
            evening=(SERVER_GID_EVENING, now + HOUR_MS, now + 2 * HOUR_MS),
        )
        result = create_task("bbq_energy", make_session(), meal="morning").run()
        assert result.data["mode"] == "meal_read_only"
        assert sent_eat_packets(recorded) == []

    def test_invalid_meal_behaves_like_whole_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """非法 ``meal`` 一律当作"没给"（不静默塞成某一顿）。"""
        task = create_task("bbq_energy", make_session(), eat=False, meal="lunch")
        assert task.meal is None

    def test_no_interval_refuses_to_guess(
        self, monkeypatch: pytest.MonkeyPatch, banquet_file
    ) -> None:
        recorded = patch_transport_meal(monkeypatch, now_ms=1_000_000_000_000)
        task = create_task("bbq_energy", make_session(), eat=True, meal="evening")
        result = task.run()
        assert result.ok is False
        assert result.data["mode"] == "meal_skipped"
        assert sent_eat_packets(recorded) == []

    # —— 纯函数：week 映射与窗口判定 ——
    def test_meal_interval_matches_by_weeks(self) -> None:
        data = GetBBQDataRes(
            activityIntervalDataList=[
                ActivityIntervalDataClass(groupID=1, weeks=0),
                ActivityIntervalDataClass(groupID=2, weeks=2),
            ]
        )
        assert BBQEnergyTask._meal_interval(data, "morning").groupID == 1
        assert BBQEnergyTask._meal_interval(data, "evening").groupID == 2

    def test_meal_interval_falls_back_by_order(self) -> None:
        """weeks 编码变了时按顺序兜底（并留 WARNING）。"""
        data = GetBBQDataRes(
            activityIntervalDataList=[
                ActivityIntervalDataClass(groupID=7, weeks=9),
                ActivityIntervalDataClass(groupID=8, weeks=9),
            ]
        )
        assert BBQEnergyTask._meal_interval(data, "morning").groupID == 7
        assert BBQEnergyTask._meal_interval(data, "evening").groupID == 8

    def test_window_state_boundaries(self) -> None:
        item = ActivityIntervalDataClass(startDateTime=1000, endDateTime=2000)
        assert BBQEnergyTask._window_state(item, 999) == "not_started"
        assert BBQEnergyTask._window_state(item, 1000) == "covering"
        assert BBQEnergyTask._window_state(item, 2000) == "covering"
        assert BBQEnergyTask._window_state(item, 2001) == "ended"


# ---------------------------------------------------------------------------
# ⑦ 只读状态任务（``bbq_status``）：网页双按钮的"两顿现在什么状态"
# ---------------------------------------------------------------------------
class TestBBQStatusTask:
    """``/api/banquet/status`` 背后的只读任务 —— **结构性不可能发出 15003**。

    【这条测试防的是什么】
    曾经的写法是跑 ``bbq_energy`` 并传 ``params={"eat": False}``，但 ``eat``
    不在规格参数里、会被 ``normalize_params`` 丢弃，runner 对缺失的 ``eat``
    默认 **True** —— "只读"端点会真的吃掉一顿。本组用例把新任务的两顿视图、
    以及"零 15003"钉死。
    """

    def test_read_only_returns_meals_view_and_sends_no_15003(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = 1_000_000_000_000
        recorded = patch_transport_meal(
            monkeypatch,
            now_ms=now,
            morning=(SERVER_GID_MORNING, now - HOUR_MS, now + HOUR_MS),
            evening=(SERVER_GID_EVENING, now + HOUR_MS, now + 2 * HOUR_MS),
            friend_ids=[11, 22],
        )
        # ★ 故意给一个"会吃"的策略与开关：只读任务必须对它们免疫
        task = create_task(
            "bbq_status", make_session(), spend_policy=paid_policy(), eat=True
        )
        result = task.run()

        assert result.ok is True
        # ← 本组用例的核心断言：无论如何都不发 15003
        assert sent_eat_packets(recorded) == []
        assert [parse_envelope(body).packet_id for body in recorded] == [
            PACKET_GET_BBQ_DATA
        ]
        meals = {item["meal"]: item for item in result.data["meals"]}
        assert meals["morning"]["window_state"] == "covering"
        assert meals["morning"]["group_id"] == SERVER_GID_MORNING
        assert meals["evening"]["window_state"] == "not_started"
        assert meals["evening"]["group_id"] == SERVER_GID_EVENING
        assert result.data["friend_count"] == 2

    def test_meals_view_marks_makeup_state_for_morning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """★ 早宴席窗口外 → ``state="makeup"``、``edible=True`` / ``invitable=False``。

        这是第七版交给前端的**唯一依据**：早宴席按钮要保持可点（补吃），
        同时邀请开关必须置灰。晚宴席在同一时刻仍在窗口内 → ``ready`` 且两者皆真。
        """
        now = 1_000_000_000_000
        patch_transport_meal(
            monkeypatch,
            now_ms=now,
            morning=(SERVER_GID_MORNING, now - 2 * HOUR_MS, now - HOUR_MS),
            evening=(SERVER_GID_EVENING, now - HOUR_MS, now + HOUR_MS),
        )
        result = create_task("bbq_status", make_session(), eat=False).run()
        meals = {item["meal"]: item for item in result.data["meals"]}

        assert meals["morning"]["window_state"] == "ended"
        assert meals["morning"]["state"] == "makeup"
        assert meals["morning"]["edible"] is True
        assert meals["morning"]["invitable"] is False

        assert meals["evening"]["state"] == "ready"
        assert meals["evening"]["edible"] is True
        assert meals["evening"]["invitable"] is True

    def test_meals_view_marks_gap_for_both(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """空档期：两顿都 ``state="gap"`` 且都不可吃、不可邀请。"""
        patch_transport_meal(
            monkeypatch,
            now_ms=GAP_MS,
            morning=(SERVER_GID_MORNING, GAP_MS - HOUR_MS, GAP_MS + HOUR_MS),
            evening=(SERVER_GID_EVENING, GAP_MS - HOUR_MS, GAP_MS + HOUR_MS),
        )
        result = create_task("bbq_status", make_session(), eat=False).run()

        for item in result.data["meals"]:
            assert item["state"] == "gap"
            assert item["edible"] is False
            assert item["invitable"] is False
            assert item["gap"] is True

    def test_meals_view_keeps_morning_edible_when_window_not_started(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """★ 对照用例：窗口"未到"时，晚宴席不可吃、**早宴席仍可吃**。

        真实场景就是凌晨补吃 —— 服务端把早宴席列成"下一次"（``not_started``），
        但按第七版口径它照吃。这条把"两顿用不同规则"写死在测试里。
        """
        now = 1_000_000_000_000
        patch_transport_meal(
            monkeypatch,
            now_ms=now,
            morning=(SERVER_GID_MORNING, now + HOUR_MS, now + 2 * HOUR_MS),
            evening=(SERVER_GID_EVENING, now + 3 * HOUR_MS, now + 4 * HOUR_MS),
        )
        result = create_task("bbq_status", make_session(), eat=False).run()
        meals = {item["meal"]: item for item in result.data["meals"]}

        assert meals["morning"]["state"] == "makeup"
        assert meals["morning"]["edible"] is True
        assert meals["morning"]["invitable"] is False

        assert meals["evening"]["state"] == "not_started"
        assert meals["evening"]["edible"] is False
        assert meals["evening"]["invitable"] is False

    def test_dry_run_does_not_crash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """演练模式（CLI ``--dry-run``）也要能跑：只组 32007 的包，不联网。"""
        from tasks.bbq import BBQStatusTask

        task = BBQStatusTask(make_session(), dry_run=True)
        result = task.execute()
        assert result.ok is True
        assert result.data["meals"] == []


if __name__ == "__main__":  # 支持 `python -m tests.test_tasks_bbq`
    raise SystemExit(pytest.main([__file__, "-v", "--no-header"]))
