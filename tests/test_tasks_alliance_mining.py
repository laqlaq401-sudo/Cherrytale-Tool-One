"""工会挖矿任务（``tasks/alliance_mining.py``：个人 + 团体）的单元测试。

运行方式：

    python -m pytest tests/test_tasks_alliance_mining.py -q
    python -m tests.test_tasks_alliance_mining

【本文件要钉死的五条规则】
1. **个人矿**：已完成且未收的矿逐个收（21049）、空闲矿合成**一次**一键开矿（21047）；
2. **一键开矿的载荷**（★ 2026-10-03 按 10.3 抓包修订）：``roleSidList``
   **必须非空** —— 每坑人数取模板表 ``roleMaxCondition``，各坑角色互不重叠；
   分配不足/模板缺失时如实告警，不发"空列表"那种我方发明的形状；
3. **团体矿占坑**（★ 2026-10-05 根因修正）：只占**空槽**（``memberPid == -1``
   哨兵，见 :data:`EMPTY_MEMBER_PID`），且角色必须同时满足模板 ID 与等级要求
   —— 名册里没有匹配角色时**不发包**、如实报告；能占几个**完全听服务端**，
   不再拿 21040 的 ``teamLimit`` 当本地闸门；
4. **演练模式零发包**；
5. **"没入会"是 ok=True 的跳过**。

【★ 夹具来源分级（2026-10-05 记）】
本文件的载荷分两类，混用会误导后来者：
- **A 类·抓包实测**：个人矿 / 团体矿的 21039·21040·21043 元素形状 —— 依据
  ``captures/工会签到+个人挖矿+团队挖矿占坑位全流程.saz`` 的 raw/23·84·86
  （字节级钉子另见 ``tests/test_capture_1003_regression.py``）；
- **B 类·dump.cs 推定**：``roster_payload``（5012 名册）—— 该消息**从未出现在
  任何抓包里**，形状由客户端 ``GameAssembly.dll`` 声明顺序推得。
  ***它删不得***：21043 要填 `roleSidList`（角色**实例** sid），而 21040 只给
  ``needRoleInfoID``/``needRoleLv``（需要什么、要多少级），**从不给**"你有哪个
  sid 可用" —— 唯一来源就是 5012。详见 :func:`roster_payload` 的告警注释。

【★ 时间单位】矿点 ``endTime`` 是**毫秒**（10.3 抓包），本文件的时间基准
与偏移全部用毫秒 —— 旧版用秒会把"已完成可收"判成"挖掘中"。
消息号写死在本文件（不 import 生产常量）：协议编号被改动会在这里直接暴露。
"""

from __future__ import annotations

import time
from collections.abc import Mapping

import pytest

from client.game_client import GameClient
from client.session import GameSession
from models.alliance import (
    AllianceNewPitOneKeyStartPacket,
    AllianceNewPitSetRolePacket,
    AlliancePersonalPitClass,
)
from models.envelope import parse_envelope
from models.game_config import (
    PersonalPitCondition,
    PitConditionTerm,
    RoleAttributes,
    RoleIdMap,
)
from models.protobuf_wire import encode_int, encode_message
from models.session_state import GameSessionState
from tasks.alliance_mining import AllianceMiningPersonalTask
from tasks.base import create_task

from tests.test_game_client import FakeResponse

import tasks.alliance_mining  # noqa: F401  导入即注册（个人 + 团体两个任务）

#: 消息号写死在测试里（依据见 models/packet_ids.py 的 Alliance 段）
PACKET_NEW_PIT = 21039
PACKET_NEW_PIT_RES = 21040
PACKET_SET_ROLE = 21043
PACKET_SET_ROLE_RES = 21044
PACKET_START = 21045
PACKET_START_RES = 21046
PACKET_ONE_KEY_START = 21047
PACKET_ONE_KEY_START_RES = 21048
PACKET_REWARD = 21049
PACKET_REWARD_RES = 21050
PACKET_GET_ALL_ROLE = 5011
PACKET_GET_ALL_ROLE_RES = 5012
#: 名册（5011）的短别名 —— 团队矿用例里读起来更顺
PACKET_ROSTER = PACKET_GET_ALL_ROLE

ALLIANCE_SUCCESS = 1
ERR_NOT_IN_ALLIANCE = 19
ERR_REPEAT_MINING = 34

#: 团体矿**空槽位**的 ``memberPid`` 哨兵值。
#:
#: 【★ 2026-10-05 抓包实证】空槽是 ``-1``，**不是 0**。10.3 抓包 raw/23 的
#: 18 个坑 64 个槽位里，7 个空槽全是 -1、且没有任何槽位是 0；raw/84 / raw/86
#: 两次占坑打的槽位在状态里恰好都是 -1。详见 ``models.alliance.NO_MEMBER_PID``。
#:
#: 用例里统一用这个常量（而不是字面量 -1），将来约定若变只改一处。
EMPTY_MEMBER_PID = -1

# ---------------------------------------------------------------------------
# ★★ 2026-10-05：角色 ID 有**两个空间**，夹具必须用真实取值 ★★
# ---------------------------------------------------------------------------
# 这是本文件最容易被写错、也最要命的一处。两个 ID 段：
#
#   名册 ``RoleClass.roleID``      = **RoleMainID**，130xxxxxx 段
#   槽位 ``needRoleInfoID``        = **RoleInfoID**，133xxxxxx 段
#
# 两者靠 ``RoleMainID →(qualityUpRoleSubID 回溯)→ 基础 MainID
# →(RoleInfoData.initialRoleMainID)→ RoleInfoID`` 换算（见
# ``models/game_config.RoleIdMap``）。
#
# **旧夹具用的是 133000001 / 133000002 之类**，即把 RoleInfoID 直接塞进了
# ``roleID`` 的位置 —— 于是"``role.roleID == slot.needRoleInfoID``"这条**错误**
# 的判据在测试里永远成立，真机上永远不成立：**测试全绿、功能全废**。
# 那几行（133000001 / 133000002 / 133000003 …）的 ``initialRoleMainID``
# 恰好是 **-1**（剧情 / 怪物行），压根换不出 RoleInfoID。
#
# 现在改用**成对**的常量，并 monkeypatch 掉换算表（不依赖本地 TextAsset）：
ROLE_MAIN_KURTIS = 130003000      # 庫爾蒂絲
ROLE_INFO_KURTIS = 133003000
ROLE_MAIN_ZHANGCHEN = 130007400   # 將臣
ROLE_INFO_ZHANGCHEN = 133007400
ROLE_MAIN_SOL = 130008400         # 索爾
ROLE_INFO_SOL = 133008400

# 任务内部用真实时钟（tasks.alliance_mining._now，★ 毫秒），所以这里的基准
# 也用真实时间。相对偏移才是断言的本意，绝对值无关紧要。
#
# ★ 2026-10-04 修：偏移必须**足够大**（这里取 30 天）—— 曾经用 100 秒，
# 全量测试跑超过 100 秒后，"挖掘中"（NOW+100s）就变成了过去时刻、被判成
# "已完成可收"，用例开始莫名其妙地多发 21049。时间敏感夹具不能留小窗口。
_DAY_MS = 86_400_000
NOW = int(time.time() * 1000)
FUTURE_MS = NOW + 30 * _DAY_MS   # 远未来：稳稳的"挖掘中"
PAST_MS = NOW - 30 * _DAY_MS     # 远过去：稳稳的"已完成可收"
MY_PLAYER_ID = 1


def patch_pit_conditions(
    monkeypatch: pytest.MonkeyPatch,
    conditions: dict[int, PersonalPitCondition],
) -> None:
    """把模板表加载换成内存替身（避免测试依赖本地 TextAsset 素材）。"""
    monkeypatch.setattr(
        AllianceMiningPersonalTask,
        "_load_pit_conditions",
        staticmethod(lambda: conditions),
    )


def patch_role_attributes(
    monkeypatch: pytest.MonkeyPatch,
    mapping: dict[int, RoleAttributes],
) -> None:
    """把角色属性表换成内存替身（元素 / 资质 / 职业 / 种族 / 品质）。

    【为什么必须 patch】个人矿挑人要读真实素材里的 ``DataRelative``。用例若依赖
    真实取值，断言就会随游戏更新漂移；这里显式给出"这个角色是什么元素"，
    用例才能精确验证"条件筛对了没有"。
    """
    monkeypatch.setattr(
        tasks.alliance_mining, "load_role_attributes", lambda *a, **k: dict(mapping)
    )


def role_attributes(
    role_main_id: int,
    *,
    element: int = 138000004,
    qualification: int = 23,
    job: int = 900000005,
    race: int = 900001001,
    quality: int = 0,
) -> RoleAttributes:
    """造一条角色属性（默认值取自庫爾蒂絲，便于用例只改关心的那一项）。"""
    return RoleAttributes(
        role_main_id=role_main_id,
        element_id=element,
        qualification_print=qualification,
        job_id=job,
        race_id=race,
        quality=quality,
    )


def patch_role_id_map(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pairs: Mapping[int, int] | None = None,
) -> None:
    """把角色 ID 换算表换成内存替身（避免测试依赖本地 TextAsset 素材）。

    :param pairs: ``{RoleInfoID: RoleMainID}`` —— 测试世界里"存在的角色"。
        默认给三对真实角色（庫爾蒂絲 / 將臣 / 索爾）。
        想要"名册里没有某个角色"时，**别把它放进 pairs** —— 换算表里没有它，
        ``RoleIdMap.role_info_ids()`` 就返回空元组，匹配自然失败。

    .. note::
       与 :func:`patch_pit_conditions` 同一套思路：生产代码走 ``load_role_id_map()``
       读真实配置表，测试里换成内存表，用例就与素材目录解耦。
    """
    default = {
        ROLE_INFO_KURTIS: ROLE_MAIN_KURTIS,
        ROLE_INFO_ZHANGCHEN: ROLE_MAIN_ZHANGCHEN,
        ROLE_INFO_SOL: ROLE_MAIN_SOL,
    }
    resolved = default if pairs is None else dict(pairs)

    info_of_base: dict[int, list[int]] = {}
    name_of_info: dict[int, str] = {}
    for info_id, main_id in resolved.items():
        info_of_base.setdefault(main_id, []).append(info_id)
        name_of_info[info_id] = f"角色{info_id}"
    role_map = RoleIdMap(
        parent={}, info_of_base=info_of_base, name_of_info=name_of_info
    )
    monkeypatch.setattr(
        tasks.alliance_mining, "load_role_id_map", lambda *a, **k: role_map
    )


def make_session() -> GameSession:
    """构造一个**不会联网**的会话（player_id=1 供团体矿槽位匹配）。"""
    session = GameSession("https://example.invalid", timeout=1.0)
    session.token_store.update(uid="u1", token="tok")
    session.set_game_state(
        GameSessionState(token="tok", player_id=MY_PLAYER_ID, server_id=1)
    )
    return session


# ---------------------------------------------------------------------------
# 报文构造（字段编号见 models/proto_fields.py）
# ---------------------------------------------------------------------------
def personal_pit_bytes(
    sid: int,
    *,
    pit_id: int = 935000001,
    end_time: int = 0,
    received: int = 0,
) -> bytes:
    """``AlliancePersonalPitClass``：1 sid、2 id、3 roleSidList、4 endTime、5 isReceived。"""
    return (
        encode_int(1, sid)
        + encode_int(2, pit_id)
        + encode_int(4, end_time)
        + encode_int(5, received)
    )


def new_pit_res_payload(
    *,
    personal: list[bytes] | None = None,
    team: list[bytes] | None = None,
    error_code: int = ALLIANCE_SUCCESS,
    personal_limit: int = 3,
    #: 默认用**真实值 4**（10.3 抓包实测 teamLimit=4；游戏内公会表 LV7+ 就是 4）。
    #: 早先用 99 表示"不设限"，但生产代码现在拿它当配额闸门 —— 夹具若给个假的大数，
    #: 用例就会跑在"现实中不可能出现"的状态里（还会触发防御性上限的告警）。
    team_limit: int = 4,
) -> bytes:
    """21040 载荷：1 errorCode、2 personalList、3 teamList、4 personalLimit、5 teamLimit。"""
    payload = encode_int(1, error_code)
    for item in personal or []:
        payload += encode_message(2, item)
    for item in team or []:
        payload += encode_message(3, item)
    payload += encode_int(4, personal_limit)
    payload += encode_int(5, team_limit)
    return payload


def role_bytes(role_sid: int, role_id: int, level: int) -> bytes:
    """``RoleClass``：1 roleSid、2 roleID、3 roleLV。"""
    return encode_int(1, role_sid) + encode_int(2, role_id) + encode_int(3, level)


def roster_payload(roles: list[bytes]) -> bytes:
    """5012 载荷：1 roleList（repeated）。

    .. warning::
       **这不是抓包样本，是"按 dump.cs 声明顺序推定"的构造载荷。**
       5012 在 ``captures/`` 的 19 份 SAZ 里**一次都没出现**（抓包是从
       "已经站在挖矿界面里"开始录的，客户端早在前面的会话里拉过并缓存了名册）。
       字段形状的依据是客户端侧（``GameAssembly.dll``，见
       ``models/role.py`` 头部的"尚未实测确认"）：
       ``GetAllRoleRes.roleList`` → ``List<RoleClass>``，
       编号 1 由 ``[ProtoMember]`` 声明顺序推得。

       所以**字段编号一旦真机实测有出入，这里会绿而真机崩** —— 首次真机
       看到 5012 时，务必用 ``describe_message()`` 对照一次，再把这里改成
       实测字节（参照 ``protocol_samples/`` 的 `*_samples.py` 做法）。

       它**不能删**：团体占坑（21043）的 ``roleSidList`` 需要一个**角色实例
       sid**，而 21040 只给"需要什么模板 id / 多少级"（``needRoleInfoID`` /
       ``needRoleLv``），从不给"你有哪个 sid 可用" —— 唯一来源就是 5012。
       实测 raw/84 的 21043 请求体逐字段解码：
       ``{1: type=1, 2: pitSid=205294527, 3: roleSidList=[155242087], 4: teamPitIndex=2}``
    """
    payload = b""
    for item in roles:
        payload += encode_message(1, item)
    return payload


def team_pit_bytes(
    sid: int,
    *,
    pit_id: int = 936000001,
    slots: list[bytes] | None = None,
    end_time: int = 0,
) -> bytes:
    """``AllianceTeamPitClass``：1 sid、2 id、3 detaliClassList、4 endTime。"""
    payload = encode_int(1, sid) + encode_int(2, pit_id)
    for slot in slots or []:
        payload += encode_message(3, slot)
    payload += encode_int(4, end_time)
    return payload


def slot_bytes(role_info_id: int, need_lv: int, member_pid: int) -> bytes:
    """``AllianceTeamPitDetailClass``：1 needRoleInfoID、2 needRoleLv、4 memberPid。"""
    return (
        encode_int(1, role_info_id)
        + encode_int(2, need_lv)
        + encode_int(4, member_pid)
    )


def simple_res_payload(*, error_code: int = ALLIANCE_SUCCESS) -> bytes:
    """绝大多数 Alliance 响应的第一个字段都是 errorCode —— 造最小成功载荷。"""
    return encode_int(1, error_code)


def start_res_payload(*, error_code: int = ALLIANCE_SUCCESS, end_time: int = FUTURE_MS) -> bytes:
    """21046 载荷：1 errorCode、2 endTime（★ 毫秒）。"""
    return encode_int(1, error_code) + encode_int(2, end_time)


def envelope_bytes(packet_id: int, payload: bytes) -> bytes:
    """拼响应字节：字段 1 = 消息号，业务子包槽位编号 = 消息号本身。"""
    return encode_int(1, packet_id) + encode_message(packet_id, payload)


class RecordedRequests(list):
    """记录请求体的列表；附带每次 ``post_raw`` 的关键字参数。

    【为什么】``include_defaults`` 只存在于"发送参数"里（Q-050 教训），
    只断言请求字节会让"漏传参数"照样绿灯 —— 断言要钉在参数上。
    """

    def __init__(self) -> None:
        super().__init__()
        self.sent_kwargs: list[dict] = []


def patch_transport(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pit_res: bytes,
    reward_res: bytes | None = None,
    one_key_res: bytes | None = None,
    roster: bytes | None = None,
    set_role_res: bytes | None = None,
    start_res: bytes | None = None,
) -> list[bytes]:
    """把 ``GameClient.post_raw`` 换成"记录请求 + 按消息号回放响应"的替身。"""
    recorded: RecordedRequests = RecordedRequests()

    def fake_post_raw(self: GameClient, body: bytes, **kwargs: object) -> FakeResponse:
        recorded.append(body)
        recorded.sent_kwargs.append(dict(kwargs))
        packet_id = parse_envelope(body).packet_id
        if packet_id == PACKET_NEW_PIT:
            return FakeResponse(envelope_bytes(PACKET_NEW_PIT_RES, pit_res))
        if packet_id == PACKET_REWARD:
            payload = reward_res or simple_res_payload()
            return FakeResponse(envelope_bytes(PACKET_REWARD_RES, payload))
        if packet_id == PACKET_ONE_KEY_START:
            payload = one_key_res or simple_res_payload()
            return FakeResponse(envelope_bytes(PACKET_ONE_KEY_START_RES, payload))
        if packet_id == PACKET_GET_ALL_ROLE:
            payload = roster if roster is not None else roster_payload([])
            return FakeResponse(envelope_bytes(PACKET_GET_ALL_ROLE_RES, payload))
        if packet_id == PACKET_SET_ROLE:
            payload = set_role_res or simple_res_payload()
            return FakeResponse(envelope_bytes(PACKET_SET_ROLE_RES, payload))
        if packet_id == PACKET_START:
            payload = start_res or start_res_payload()
            return FakeResponse(envelope_bytes(PACKET_START_RES, payload))
        raise AssertionError(f"测试替身收到了意料之外的请求：{packet_id}")

    monkeypatch.setattr(GameClient, "post_raw", fake_post_raw)
    return recorded


def sent_packet_ids(recorded: list[bytes]) -> list[int]:
    """从记录的请求体里解出全部消息号（按发送顺序）。"""
    return [parse_envelope(body).packet_id for body in recorded]


def sent_one_key_pits(recorded: list[bytes]) -> list[AlliancePersonalPitClass]:
    """解出 21047 一键开矿请求里的矿点清单（复用生产模型核对字段编号）。"""
    pits: list[AlliancePersonalPitClass] = []
    for body in recorded:
        view = parse_envelope(body)
        if view.packet_id == PACKET_ONE_KEY_START:
            packet = AllianceNewPitOneKeyStartPacket.decode(view.sub_packet_bytes)
            pits.extend(packet.oneKeyPersonalList)
    return pits


# ---------------------------------------------------------------------------
# 个人矿
# ---------------------------------------------------------------------------
class TestPersonalMining:
    def test_harvest_then_one_key_start(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """已完成未收的收掉、空闲矿填好角色后一键开掉、挖掘中的不动。

        ★ 2026-10-03：一键开矿前**先拉名册**（5011），每坑 roleSidList 按模板
        表 roleMaxCondition 填满、等级从高到低、各坑不重叠 —— 不再发空列表。
        """
        patch_pit_conditions(
            monkeypatch,
            {
                935000001: PersonalPitCondition(935000001, role_max=1),
                935000002: PersonalPitCondition(935000002, role_max=2),
                935000003: PersonalPitCondition(935000003, role_max=2),
            },
        )
        pit_res = new_pit_res_payload(
            personal=[
                # sid=11：已完成未收（毫秒）→ 收（21049）
                encode_int(1, 11) + encode_int(2, 935000001) + encode_int(4, PAST_MS) + encode_int(5, 0),
                # sid=12：挖掘中 → 不动
                encode_int(1, 12) + encode_int(2, 935000002) + encode_int(4, FUTURE_MS) + encode_int(5, 0),
                # sid=13：空闲（endTime=-1，10.3 抓包的空闲约定）→ 开（21047）
                encode_int(1, 13) + encode_int(2, 935000003) + encode_int(4, -1) + encode_int(5, 0),
            ],
        )
        roster = roster_payload(
            [
                role_bytes(701, ROLE_MAIN_KURTIS, 10),  # 等级低
                role_bytes(702, ROLE_MAIN_ZHANGCHEN, 20),  # 等级高 → 先被指派
                role_bytes(703, ROLE_MAIN_SOL, 30),
            ]
        )
        recorded = patch_transport(monkeypatch, pit_res=pit_res, roster=roster)
        task = create_task("alliance_mining_personal", make_session())
        result = task.run()

        assert sent_packet_ids(recorded) == [
            PACKET_NEW_PIT,
            PACKET_REWARD,  # 收 sid=11
            PACKET_GET_ALL_ROLE,  # ★ 有空闲矿要先拉名册
            PACKET_ONE_KEY_START,  # 一键开 sid=13
        ]
        pits = sent_one_key_pits(recorded)
        assert [pit.sid for pit in pits] == [13], "一键开矿只应包含空闲矿点"
        assert pits[0].roleSidList == [703, 702], "按模板表人数（2）填、等级从高到低"
        # 请求元素必须是"新鲜构造"的：endTime/isReceived 保持默认 0（抓包形状），
        # 不能把 21040 解码出的 -1 原样带回请求
        assert pits[0].endTime == 0
        assert pits[0].isReceived == 0
        assert result.ok is True
        assert "收矿 1 个" in result.message
        assert "开矿 1 个" in result.message

    def test_one_key_element_byte_shape_needs_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """★ 字节形状钉子：矿点元素 False 编码丢 4 B 的 ``20 00 28 00``。

        10.3 抓包 321 B（8 元素，含 0 值字段）；include_defaults=False 会
        变 289 B —— 2026-10-04 真机 274 B（7 元素）两连败的本体。
        """
        packet = AllianceNewPitOneKeyStartPacket.for_pits(
            [AlliancePersonalPitClass(sid=123, id=456, roleSidList=[111, 222])]
        )
        with_defaults = packet.encode()  # 默认 True
        without = packet.encode(include_defaults=False)
        # 元素体 14 B（08 7b 10 c803 18 6f 18 de01 20 00 28 00）+ 元素头 2 B
        assert len(with_defaults) == 16 and with_defaults.hex().endswith("20002800")
        assert len(without) == 12
        # 抓包整体形状只能用 True 重现（8 元素 321 B 逐字节一致，另见
        # tests/test_capture_1003_regression.py）


    def test_one_key_start_sends_include_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """★ Q-050 定案：21047 必须 include_defaults=True（含 0 值字段）。

        矿点元素的 endTime=0/isReceived=0 省略时每元素少 4 字节
        （8 坑 = 321→289 B），被服务端以 22 字节 blob 硬拒 ——
        2026-10-04 真机两连败的根因，本断言钉死修复。
        """
        patch_pit_conditions(
            monkeypatch,
            {935000001: PersonalPitCondition(935000001, role_max=1)},
        )
        pit_res = new_pit_res_payload(
            personal=[
                encode_int(1, 13) + encode_int(2, 935000001) + encode_int(4, -1) + encode_int(5, 0),
            ],
        )
        recorded = patch_transport(
            monkeypatch,
            pit_res=pit_res,
            roster=roster_payload([role_bytes(721, ROLE_MAIN_KURTIS, 50)]),
        )
        task = create_task("alliance_mining_personal", make_session())
        result = task.run()

        assert result.ok is True
        # ★ ``include_defaults`` 在 build() 阶段就消化进字节了（post_raw 层看不到
        # 该参数）—— 这里用它的**字节等价形式**断言：发出的子包必须能用
        # include_defaults=True（默认）逐字节重编码成功，即每个矿点元素都
        # 显式带了 endTime=0 / isReceived=0（"20 00 28 00"）。
        for body in recorded:
            view = parse_envelope(body)
            if view.packet_id != PACKET_ONE_KEY_START:
                continue
            decoded = AllianceNewPitOneKeyStartPacket.decode(view.sub_packet_bytes)
            assert decoded.encode() == view.sub_packet_bytes, (
                "21047 子包与 True 重编码不一致 —— include_defaults 没传 True，"
                "每个矿点少了 4 字节的 20 00 28 00，会被 22 字节 blob 拒收"
            )
            assert view.sub_packet_bytes.count(bytes((0x20, 0x00, 0x28, 0x00))) == len(
                decoded.oneKeyPersonalList
            )


    def test_one_key_roles_are_disjoint_across_pits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """两个空闲矿各分各的角色，绝不重复（10.3 抓包：8 坑 37 sid 无一重复）。"""
        patch_pit_conditions(
            monkeypatch,
            {
                935000001: PersonalPitCondition(935000001, role_max=2),
                935000002: PersonalPitCondition(935000002, role_max=2),
            },
        )
        pit_res = new_pit_res_payload(
            personal=[
                encode_int(1, 51) + encode_int(2, 935000001) + encode_int(4, -1) + encode_int(5, 0),
                encode_int(1, 52) + encode_int(2, 935000002) + encode_int(4, -1) + encode_int(5, 0),
            ],
        )
        roster = roster_payload(
            [role_bytes(801 + i, ROLE_MAIN_KURTIS + i, 10 + i) for i in range(4)]
        )
        recorded = patch_transport(monkeypatch, pit_res=pit_res, roster=roster)
        task = create_task("alliance_mining_personal", make_session())
        result = task.run()

        pits = sent_one_key_pits(recorded)
        assert [pit.sid for pit in pits] == [51, 52]
        all_sids = [sid for pit in pits for sid in pit.roleSidList]
        assert len(all_sids) == len(set(all_sids)) == 4, "四坑角色互不重叠且各填 2 个"
        assert result.ok is True

    def test_unknown_template_is_reported_and_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """模板表里查不到的矿点 → 跳过并告警，不猜人数。"""
        patch_pit_conditions(
            monkeypatch,
            {935000001: PersonalPitCondition(935000001, role_max=2)},
        )
        pit_res = new_pit_res_payload(
            personal=[
                encode_int(1, 61) + encode_int(2, 935009999) + encode_int(4, -1) + encode_int(5, 0),
            ],
        )
        recorded = patch_transport(
            monkeypatch,
            pit_res=pit_res,
            roster=roster_payload([role_bytes(901, ROLE_MAIN_KURTIS, 50)]),
        )
        task = create_task("alliance_mining_personal", make_session())
        result = task.run()

        assert PACKET_ONE_KEY_START not in sent_packet_ids(recorded)
        assert result.ok is False
        assert "没有可成行的矿点" in result.message

    def test_partial_assignment_is_not_submitted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """★ 2026-10-04：凑不满 roleMaxCondition 的坑**整坑跳过**（不提交残阵）。

        10.3 抓包里客户端每坑都满编；实测提交残阵被服务端以
        22 字节 {"response_code":"OK"} 硬拒（见 recon_findings §21.16）。
        """
        patch_pit_conditions(
            monkeypatch,
            {935000131: PersonalPitCondition(935000131, role_max=6)},
        )
        pit_res = new_pit_res_payload(
            personal=[
                encode_int(1, 71) + encode_int(2, 935000131) + encode_int(4, -1) + encode_int(5, 0),
            ],
        )
        # 名册只有 2 个角色，凑不满 6 → 整坑跳过
        recorded = patch_transport(
            monkeypatch,
            pit_res=pit_res,
            roster=roster_payload([role_bytes(911, ROLE_MAIN_KURTIS, 50), role_bytes(912, ROLE_MAIN_ZHANGCHEN, 40)]),
        )
        task = create_task("alliance_mining_personal", make_session())
        result = task.run()

        assert PACKET_ONE_KEY_START not in sent_packet_ids(recorded)
        assert result.ok is False
        assert "没有可成行的矿点" in result.message
        assert any("不提交残阵" in note for note in result.data["allocation_notes"])


    def test_missing_table_blocks_start(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """模板表不可用 → 不开矿（拿不到人数依据就不猜），如实说明。"""
        monkeypatch.setattr(
            AllianceMiningPersonalTask,
            "_load_pit_conditions",
            staticmethod(lambda: None),
        )
        pit_res = new_pit_res_payload(
            personal=[
                encode_int(1, 62) + encode_int(2, 935000001) + encode_int(4, -1) + encode_int(5, 0),
            ],
        )
        recorded = patch_transport(
            monkeypatch,
            pit_res=pit_res,
            roster=roster_payload([role_bytes(902, ROLE_MAIN_KURTIS, 50)]),
        )
        task = create_task("alliance_mining_personal", make_session())
        result = task.run()

        assert sent_packet_ids(recorded) == [PACKET_NEW_PIT, PACKET_GET_ALL_ROLE]
        assert result.ok is False
        assert "模板表" in result.message

    def test_one_key_rejection_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """一键开矿被服务端拒绝 → ok=False，原样报错误码，不重试。"""
        patch_pit_conditions(
            monkeypatch,
            {935000001: PersonalPitCondition(935000001, role_max=1)},
        )
        pit_res = new_pit_res_payload(
            personal=[
                encode_int(1, 21) + encode_int(2, 935000001) + encode_int(4, -1) + encode_int(5, 0),
            ],
        )
        recorded = patch_transport(
            monkeypatch,
            pit_res=pit_res,
            roster=roster_payload([role_bytes(711, ROLE_MAIN_KURTIS, 50)]),
            one_key_res=simple_res_payload(error_code=ERR_REPEAT_MINING),
        )
        task = create_task("alliance_mining_personal", make_session())
        result = task.run()

        assert sent_packet_ids(recorded).count(PACKET_ONE_KEY_START) == 1, "不重试"
        assert result.ok is False
        assert "Re_Request_MiningPit" in result.message

    def test_nothing_to_do_is_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """全部矿点都在挖掘中 → 无事可做，ok=True，只发一次 21039（不拉名册）。"""
        pit_res = new_pit_res_payload(
            personal=[
                encode_int(1, 31) + encode_int(2, 935000001) + encode_int(4, FUTURE_MS) + encode_int(5, 0),
            ],
        )
        recorded = patch_transport(monkeypatch, pit_res=pit_res)
        task = create_task("alliance_mining_personal", make_session())
        result = task.run()

        assert sent_packet_ids(recorded) == [PACKET_NEW_PIT]
        assert result.ok is True

    def test_not_in_alliance_is_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """21039 回 errorCode=19 → ok=True，如实说明。"""
        recorded = patch_transport(
            monkeypatch,
            pit_res=new_pit_res_payload(error_code=ERR_NOT_IN_ALLIANCE),
        )
        task = create_task("alliance_mining_personal", make_session())
        result = task.run()

        assert sent_packet_ids(recorded) == [PACKET_NEW_PIT]
        assert result.ok is True
        assert "未加入工会" in result.message

    def test_dry_run_sends_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded: list[bytes] = []

        def fake_post_raw(self: GameClient, body: bytes) -> FakeResponse:
            recorded.append(body)
            raise AssertionError("演练模式不该发出任何请求")

        monkeypatch.setattr(GameClient, "post_raw", fake_post_raw)
        task = create_task("alliance_mining_personal", make_session(), dry_run=True)
        result = task.run()

        assert recorded == []
        assert result.ok is True
        assert "演练" in result.message

    # ------------------------------------------------------------------
    # ★★ 按矿点条件挑人（2026-10-06）★★
    #
    # 140 个模板**全部**要求元素 + 资质（84 个还要职业、64 个要星级、
    # 50 个要品质、5 个要种族）。过去只按等级挑人，被服务端拒是必然的 ——
    # 这就是 21047 一直回 errorCode=-2 的根因。
    # 条件语义依据 `NewPitPersonalLimit.IsAchieveLimit` 的反汇编：
    # 元素/资质/职业/种族是 ==，星级/等级/品质是 >=。
    # ------------------------------------------------------------------
    def test_picks_roles_satisfying_element_condition(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """坑要 1 个**水**元素角色 → 必须挑到水属性的那个，而不是"等级最高的"。"""
        patch_pit_conditions(
            monkeypatch,
            {
                935000114: PersonalPitCondition(
                    935000114,
                    role_max=2,
                    title="牛刀小試",
                    terms=(PitConditionTerm("element", 138000003),),
                )
            },
        )
        patch_role_attributes(
            monkeypatch,
            {
                ROLE_MAIN_KURTIS: role_attributes(ROLE_MAIN_KURTIS, element=138000004),
                ROLE_MAIN_ZHANGCHEN: role_attributes(
                    ROLE_MAIN_ZHANGCHEN, element=138000004
                ),
                ROLE_MAIN_SOL: role_attributes(ROLE_MAIN_SOL, element=138000003),
            },
        )
        pit_res = new_pit_res_payload(
            personal=[
                encode_int(1, 21)
                + encode_int(2, 935000114)
                + encode_int(4, -1)
                + encode_int(5, 0)
            ]
        )
        # 水属性的 SOL 等级**最低** —— 只按等级挑的话根本轮不到它
        roster = roster_payload(
            [
                role_bytes(701, ROLE_MAIN_KURTIS, 50),
                role_bytes(702, ROLE_MAIN_ZHANGCHEN, 40),
                role_bytes(703, ROLE_MAIN_SOL, 10),
            ]
        )
        recorded = patch_transport(monkeypatch, pit_res=pit_res, roster=roster)
        result = create_task("alliance_mining_personal", make_session()).run()

        pits = sent_one_key_pits(recorded)
        assert len(pits) == 1
        assert 703 in pits[0].roleSidList, "水属性的角色必须在编队里（尽管它等级最低）"
        assert len(pits[0].roleSidList) == 2, "人数仍要满编 roleMaxCondition"
        assert result.ok is True

    def test_skips_pit_when_condition_cannot_be_met(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """名册里没有满足条件的角色 → **整坑不发**，并说清缺的是哪条条件。"""
        patch_pit_conditions(
            monkeypatch,
            {
                935000114: PersonalPitCondition(
                    935000114,
                    role_max=2,
                    title="牛刀小試",
                    terms=(PitConditionTerm("element", 138000003),),
                )
            },
        )
        # 三个角色全是火属性 → 凑不出"水"
        patch_role_attributes(
            monkeypatch,
            {
                ROLE_MAIN_KURTIS: role_attributes(ROLE_MAIN_KURTIS, element=138000004),
                ROLE_MAIN_ZHANGCHEN: role_attributes(
                    ROLE_MAIN_ZHANGCHEN, element=138000004
                ),
                ROLE_MAIN_SOL: role_attributes(ROLE_MAIN_SOL, element=138000004),
            },
        )
        pit_res = new_pit_res_payload(
            personal=[
                encode_int(1, 22)
                + encode_int(2, 935000114)
                + encode_int(4, -1)
                + encode_int(5, 0)
            ]
        )
        roster = roster_payload(
            [
                role_bytes(701, ROLE_MAIN_KURTIS, 50),
                role_bytes(702, ROLE_MAIN_ZHANGCHEN, 40),
                role_bytes(703, ROLE_MAIN_SOL, 30),
            ]
        )
        recorded = patch_transport(monkeypatch, pit_res=pit_res, roster=roster)
        result = create_task("alliance_mining_personal", make_session()).run()

        assert PACKET_ONE_KEY_START not in sent_packet_ids(recorded), (
            "编队不满足条件就一个包都不该发（发了必被拒）"
        )
        notes = "；".join(result.data["allocation_notes"])
        assert "元素=水" in notes, "要报出缺的是哪条条件（还带中文元素名），而不是笼统的'凑不齐'"
        assert "不提交残阵" in notes

    def test_condition_roles_are_not_shared_across_pits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """两个坑都要"水"角色，而名册只有一个 → 第二个坑如实跳过。

        （游戏规则：一个角色同一时间只能在一个坑里干活，抓包 8 坑 37 个 sid 无重复。）
        """
        patch_pit_conditions(
            monkeypatch,
            {
                935000114: PersonalPitCondition(
                    935000114,
                    role_max=2,
                    terms=(PitConditionTerm("element", 138000003),),
                ),
                935000115: PersonalPitCondition(
                    935000115,
                    role_max=2,
                    terms=(PitConditionTerm("element", 138000003),),
                ),
            },
        )
        patch_role_attributes(
            monkeypatch,
            {
                ROLE_MAIN_KURTIS: role_attributes(ROLE_MAIN_KURTIS, element=138000003),
                ROLE_MAIN_ZHANGCHEN: role_attributes(
                    ROLE_MAIN_ZHANGCHEN, element=138000004
                ),
                ROLE_MAIN_SOL: role_attributes(ROLE_MAIN_SOL, element=138000004),
            },
        )
        pit_res = new_pit_res_payload(
            personal=[
                encode_int(1, 31) + encode_int(2, 935000114) + encode_int(4, -1) + encode_int(5, 0),
                encode_int(1, 32) + encode_int(2, 935000115) + encode_int(4, -1) + encode_int(5, 0),
            ]
        )
        roster = roster_payload(
            [
                role_bytes(701, ROLE_MAIN_KURTIS, 50),
                role_bytes(702, ROLE_MAIN_ZHANGCHEN, 40),
                role_bytes(703, ROLE_MAIN_SOL, 30),
            ]
        )
        recorded = patch_transport(monkeypatch, pit_res=pit_res, roster=roster)
        result = create_task("alliance_mining_personal", make_session()).run()

        pits = sent_one_key_pits(recorded)
        assert [pit.sid for pit in pits] == [31], "只有一个水角色，只能开一个坑"
        assert 701 in pits[0].roleSidList
        assert "元素=水" in "；".join(result.data["allocation_notes"])

    def test_missing_attribute_table_does_not_lie(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """读不到属性表 → 明确报"读不到表"，**不许**谎称"名册里没有合适的角色"。"""
        monkeypatch.setattr(
            tasks.alliance_mining,
            "load_role_attributes",
            lambda *a, **k: (_ for _ in ()).throw(OSError("表不在")),
        )
        patch_pit_conditions(
            monkeypatch,
            {935000114: PersonalPitCondition(935000114, role_max=2)},
        )
        pit_res = new_pit_res_payload(
            personal=[
                encode_int(1, 41) + encode_int(2, 935000114) + encode_int(4, -1) + encode_int(5, 0)
            ]
        )
        recorded = patch_transport(
            monkeypatch, pit_res=pit_res, roster=roster_payload([role_bytes(701, ROLE_MAIN_KURTIS, 50)])
        )
        result = create_task("alliance_mining_personal", make_session()).run()

        assert PACKET_ONE_KEY_START not in sent_packet_ids(recorded)
        assert "读不到角色属性表" in result.message


# ---------------------------------------------------------------------------
# 团体矿
# ---------------------------------------------------------------------------
class TestTeamMining:
    """★ 2026-10-03 语义重写 + ★ 2026-10-05 空槽哨兵 / 角色 ID 空间修正。

    占坑是二元问题 —— 空槽位（``memberPid == -1``，见 ``EMPTY_MEMBER_PID``）
    上名册有匹配角色就占；不发 21045（满员自动开矿）；排序 = 空槽少优先、
    并列时难度高的优先；能占几个**完全听服务端的**（本地无闸门）。

    【★ 角色匹配必须跨 ID 空间】槽位给的是 ``needRoleInfoID``（**RoleInfoID**，
    133xxxxxx），名册给的是 ``roleID``（**RoleMainID**，130xxxxxx）。
    本类统一用 :func:`patch_role_id_map` 装一份内存换算表，夹具里
    ``ROLE_INFO_*`` / ``ROLE_MAIN_*`` 成对出现 —— 谁再写出
    "``role.roleID == slot.needRoleInfoID``"这种同段比较，用例会直接红。
    """

    @pytest.fixture(autouse=True)
    def _role_ids(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """本类全部用例都装内存换算表（不依赖本地 TextAsset 素材）。"""
        patch_role_id_map(monkeypatch)

    def test_fills_empty_slot_with_matching_role(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """空槽位（memberPid=-1）有匹配角色 → 发 21043 占坑；已占槽位不动。"""
        pit_res = new_pit_res_payload(
            team=[
                team_pit_bytes(
                    41,
                    slots=[
                        slot_bytes(ROLE_INFO_KURTIS, 30, 999),  # 已被别人占：不动
                        slot_bytes(ROLE_INFO_ZHANGCHEN, 30, EMPTY_MEMBER_PID),  # 空：名册有匹配 → 占
                    ],
                ),
            ],
        )
        roster = roster_payload(
            [
                role_bytes(701, ROLE_MAIN_KURTIS, 40),
                role_bytes(702, ROLE_MAIN_ZHANGCHEN, 99),  # 模板匹配槽位 1
            ]
        )
        recorded = patch_transport(monkeypatch, pit_res=pit_res, roster=roster)
        task = create_task("alliance_mining_team", make_session())
        result = task.run()

        assert PACKET_SET_ROLE in sent_packet_ids(recorded)
        packets = [
            AllianceNewPitSetRolePacket.decode(parse_envelope(b).sub_packet_bytes)
            for b in recorded
            if parse_envelope(b).packet_id == PACKET_SET_ROLE
        ]
        assert len(packets) == 1
        assert packets[0].pitSid == 41
        assert packets[0].teamPitIndex == 1  # 只占空槽位
        assert packets[0].roleSidList == [702]
        assert result.ok is True
        assert "占坑 1 次" in result.message

    def test_unfillable_slot_sends_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """名册里有这个角色但**等级不够** → 不发 21043，且**说清是等级问题**。

        ★ 2026-10-05：旧断言只查一句笼统的"名册中没有可用的匹配"，那句话
        同时覆盖"没这个角色""等级不够""被别的坑占了"三种情况 —— 用户拿到
        日志也不知道该去抽卡还是去升级。现在文案必须**分因**。
        """
        pit_res = new_pit_res_payload(
            team=[
                team_pit_bytes(
                    42,
                    slots=[slot_bytes(ROLE_INFO_SOL, 99, EMPTY_MEMBER_PID)],
                ),
            ],
        )
        roster = roster_payload([role_bytes(703, ROLE_MAIN_SOL, 10)])  # 等级不够
        recorded = patch_transport(monkeypatch, pit_res=pit_res, roster=roster)
        task = create_task("alliance_mining_team", make_session())
        result = task.run()

        assert PACKET_SET_ROLE not in sent_packet_ids(recorded)
        assert result.ok is True  # 没发包也就没有失败
        assert "最高只有 10 级" in result.message, "必须指出是等级不足，而不是含糊的'没有匹配'"
        assert "名册里没有这个角色" not in result.message, "角色是有的，别误报成没有"

    def test_unowned_role_is_reported_as_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """名册里**根本没有这个角色** → 文案要跟"等级不够"区分开。

        ★ 这是 2026-10-05 角色 ID 空间修正的**核心钉子**：换算表里没有
        133003000 之外的角色时，槽位要 133007400（將臣）就应当被报成
        "名册里没有这个角色"；若有人把 ``matches()`` 退回成
        ``role.roleID == slot.needRoleInfoID``，这条会因为 ID 段不同而
        依然"报缺"，但下面的 ``test_role_id_map_is_required_for_matching``
        会直接抓住真正的回归。
        """
        pit_res = new_pit_res_payload(
            team=[
                team_pit_bytes(
                    43,
                    slots=[slot_bytes(ROLE_INFO_ZHANGCHEN, 1, EMPTY_MEMBER_PID)],
                ),
            ],
        )
        # 名册只有庫爾蒂絲，槽位要的是將臣
        roster = roster_payload([role_bytes(704, ROLE_MAIN_KURTIS, 99)])
        recorded = patch_transport(monkeypatch, pit_res=pit_res, roster=roster)
        task = create_task("alliance_mining_team", make_session())
        result = task.run()

        assert PACKET_SET_ROLE not in sent_packet_ids(recorded)
        assert "名册里没有这个角色" in result.message

    def test_role_id_map_is_required_for_matching(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """★ 核心回归钉子：名册 ``roleID``(RoleMainID) 与槽位 ``needRoleInfoID``
        (RoleInfoID) **不在同一个 ID 段**，匹配必须经过换算表。

        这里刻意把两者的**数字**设成"看起来一样"（都用 130003000），
        只有真正的换算逻辑才能让它们配上；同时断言换算表里查不到时**不许**
        直接相等 —— 谁把 ``matches()`` 退回成 ``==``，这条立刻红。
        """
        pit_res = new_pit_res_payload(
            team=[
                team_pit_bytes(
                    45,
                    slots=[slot_bytes(ROLE_INFO_KURTIS, 1, EMPTY_MEMBER_PID)],
                ),
            ],
        )
        roster = roster_payload([role_bytes(705, ROLE_MAIN_KURTIS, 50)])
        recorded = patch_transport(monkeypatch, pit_res=pit_res, roster=roster)
        task = create_task("alliance_mining_team", make_session())
        result = task.run()

        packets = [
            AllianceNewPitSetRolePacket.decode(parse_envelope(b).sub_packet_bytes)
            for b in recorded
            if parse_envelope(b).packet_id == PACKET_SET_ROLE
        ]
        assert len(packets) == 1, "RoleMainID 经换算命中 RoleInfoID 后才能占坑"
        assert packets[0].roleSidList == [705], "填进请求的必须是**实例 sid**"
        assert "占坑 1 次" in result.message

    def test_missing_role_id_table_does_not_lie(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """换算表读不到 → 明确报"读不到表"，**不许**谎称"名册里没有这个角色"。

        【为什么单列一条】把"素材表缺失"说成"你没有这个角色"，是那种
        "错误信息指向错误方向"的典型 —— 用户会去抽卡而不是去修素材。
        """
        monkeypatch.setattr(
            tasks.alliance_mining,
            "load_role_id_map",
            lambda *a, **k: (_ for _ in ()).throw(OSError("表不在")),
        )
        pit_res = new_pit_res_payload(
            team=[
                team_pit_bytes(
                    46,
                    slots=[slot_bytes(ROLE_INFO_KURTIS, 1, EMPTY_MEMBER_PID)],
                ),
            ],
        )
        recorded = patch_transport(
            monkeypatch, pit_res=pit_res, roster=roster_payload([role_bytes(706, ROLE_MAIN_KURTIS, 50)])
        )
        task = create_task("alliance_mining_team", make_session())
        result = task.run()

        assert PACKET_SET_ROLE not in sent_packet_ids(recorded)
        assert "读不到角色 ID 换算表" in result.message
        assert "名册里没有这个角色" not in result.message, "不许把缺表说成缺角色"

    def test_priority_fewer_empty_slots_first(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """优先占**剩余空槽少**的坑（更容易被补满自动开矿）。

        坑 A（4 槽 3 空）vs 坑 B（2 槽 1 空）→ 先占 B 的那个空槽。
        """
        pit_res = new_pit_res_payload(
            team=[
                team_pit_bytes(
                    61,
                    slots=[
                        slot_bytes(ROLE_INFO_KURTIS, 1, 999),
                        slot_bytes(ROLE_INFO_KURTIS, 1, EMPTY_MEMBER_PID),
                        slot_bytes(ROLE_INFO_KURTIS, 1, EMPTY_MEMBER_PID),
                        slot_bytes(ROLE_INFO_KURTIS, 1, EMPTY_MEMBER_PID),
                    ],
                ),
                team_pit_bytes(
                    62,
                    slots=[
                        slot_bytes(ROLE_INFO_ZHANGCHEN, 1, 999),
                        slot_bytes(ROLE_INFO_ZHANGCHEN, 1, EMPTY_MEMBER_PID),
                    ],
                ),
            ],
        )
        roster = roster_payload(
            [
                role_bytes(801, ROLE_MAIN_KURTIS, 50),
                role_bytes(802, ROLE_MAIN_ZHANGCHEN, 50),
            ]
        )
        recorded = patch_transport(monkeypatch, pit_res=pit_res, roster=roster)
        task = create_task("alliance_mining_team", make_session())
        result = task.run()

        packets = [
            AllianceNewPitSetRolePacket.decode(parse_envelope(b).sub_packet_bytes)
            for b in recorded
            if parse_envelope(b).packet_id == PACKET_SET_ROLE
        ]
        assert [p.pitSid for p in packets] == [62, 61], "空槽少的坑 B 先占"

    def test_priority_higher_difficulty_on_tie(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """★ 2026-10-05：空槽数并列时，**难度高**（``AllianceTeamPitData.difficulty``）
        的先占 —— 奖励更多。

        坑 C（模板 936000047，难度 5，6 槽 3 占 3 空）
        vs 坑 D（模板 936000021，难度 3，4 槽 1 占 3 空）
        → 空槽并列（3），难度高的 C 先占。

        【与旧的 ``-len(detaliClassList)`` 的关系】两者数值等价（槽位数只由
        难度决定，10.3 抓包 18/18 实测），但读 difficulty 把"奖励多"这层
        语义写进了代码。
        """
        pit_res = new_pit_res_payload(
            team=[
                team_pit_bytes(
                    71,
                    pit_id=936000047,  # 難度 5 → 6 槽
                    slots=[
                        slot_bytes(ROLE_INFO_KURTIS, 1, 999),
                        slot_bytes(ROLE_INFO_KURTIS, 1, 999),
                        slot_bytes(ROLE_INFO_KURTIS, 1, 999),
                        slot_bytes(ROLE_INFO_KURTIS, 1, EMPTY_MEMBER_PID),
                        slot_bytes(ROLE_INFO_KURTIS, 1, EMPTY_MEMBER_PID),
                        slot_bytes(ROLE_INFO_KURTIS, 1, EMPTY_MEMBER_PID),
                    ],
                ),
                team_pit_bytes(
                    72,
                    pit_id=936000021,  # 難度 3 → 4 槽
                    slots=[
                        slot_bytes(ROLE_INFO_ZHANGCHEN, 1, 999),
                        slot_bytes(ROLE_INFO_ZHANGCHEN, 1, EMPTY_MEMBER_PID),
                        slot_bytes(ROLE_INFO_ZHANGCHEN, 1, EMPTY_MEMBER_PID),
                        slot_bytes(ROLE_INFO_ZHANGCHEN, 1, EMPTY_MEMBER_PID),
                    ],
                ),
            ],
        )
        roster = roster_payload(
            [
                role_bytes(811, ROLE_MAIN_KURTIS, 50),
                role_bytes(812, ROLE_MAIN_ZHANGCHEN, 50),
            ]
        )
        recorded = patch_transport(monkeypatch, pit_res=pit_res, roster=roster)
        task = create_task("alliance_mining_team", make_session())
        result = task.run()

        packets = [
            AllianceNewPitSetRolePacket.decode(parse_envelope(b).sub_packet_bytes)
            for b in recorded
            if parse_envelope(b).packet_id == PACKET_SET_ROLE
        ]
        assert [p.pitSid for p in packets] == [71, 72], "并列时难度高的先占"

    def test_priority_survives_missing_template_table(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """★ 模板表读不到时**不阻断占坑** —— 退化为"只按空槽数排序"。

        这是与个人矿的关键差异：个人矿读不到模板表就不发包（请求形状依赖它），
        团体矿的 21043 请求不含模板数据，difficulty 只影响顺序。
        """
        monkeypatch.setattr(
            tasks.alliance_mining,
            "load_team_pit_conditions",
            lambda *a, **k: (_ for _ in ()).throw(OSError("表不在")),
        )
        pit_res = new_pit_res_payload(
            team=[
                team_pit_bytes(
                    81,
                    pit_id=936000047,
                    slots=[
                        slot_bytes(ROLE_INFO_KURTIS, 1, 999),
                        slot_bytes(ROLE_INFO_KURTIS, 1, EMPTY_MEMBER_PID),
                    ],
                ),
            ],
        )
        roster = roster_payload([role_bytes(821, ROLE_MAIN_KURTIS, 50)])
        recorded = patch_transport(monkeypatch, pit_res=pit_res, roster=roster)
        task = create_task("alliance_mining_team", make_session())
        result = task.run()

        assert PACKET_SET_ROLE in sent_packet_ids(recorded), "读不到表也照占"
        assert result.ok is True

    def test_role_not_reused_across_pits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """同一角色本次运行只占一个坑 —— 两个坑需要同一角色时，第二个如实报缺。

        （游戏规则：占坑后同角色的其他坑会灰掉 —— 一个角色只能占一个坑。）

        ★ 2026-10-05：第二个坑的缺口语义是"**可用角色已被本轮其它坑位占用**"
        —— 与"没这个角色""等级不够"必须区分开（旧文案三种情况都是同一句话）。
        """
        pit_res = new_pit_res_payload(
            team=[
                team_pit_bytes(
                    81,
                    slots=[
                        slot_bytes(ROLE_INFO_KURTIS, 1, 999),
                        slot_bytes(ROLE_INFO_KURTIS, 1, EMPTY_MEMBER_PID),
                    ],
                ),
                team_pit_bytes(
                    82,
                    slots=[slot_bytes(ROLE_INFO_KURTIS, 1, EMPTY_MEMBER_PID)],
                ),
            ],
        )
        roster = roster_payload([role_bytes(821, ROLE_MAIN_KURTIS, 50)])  # 只有一个匹配角色
        recorded = patch_transport(monkeypatch, pit_res=pit_res, roster=roster)
        task = create_task("alliance_mining_team", make_session())
        result = task.run()

        packets = [
            AllianceNewPitSetRolePacket.decode(parse_envelope(b).sub_packet_bytes)
            for b in recorded
            if parse_envelope(b).packet_id == PACKET_SET_ROLE
        ]
        assert len(packets) == 1, "角色已用于坑 81，不再拿去占坑 82"
        assert result.ok is True
        assert "可用角色已被本轮其它坑位占用" in result.message, (
            "要说清是'被占用了'，而不是笼统的'没有匹配'"
        )

    # ------------------------------------------------------------------
    # ★★ 每日次数闸门（2026-10-05 晚恢复）★★
    #
    # 【这一组用例是在"撤回"上一轮的结论】上一轮把 21040.teamLimit 判成
    # "按坑的限制、不能当配额用"，据此删掉了闸门；真机随即连吃 15 个 -8。
    # 反汇编实证：`AllianceNewPitRes` 只有 5 个字段，teamLimit 就是个 int，
    # 而客户端 `PitRoleIcon.IsTeamCountMax()` 比的正是
    # `get_TeamCount() >= get_TeamLimitCount()`。
    # 所以**闸门是对的，删它是错的** —— 下面两条就是那条撤回的钉子。
    # ------------------------------------------------------------------
    def test_daily_quota_caps_the_number_of_joins(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """额度 = ``teamLimit − 我已占``；**占满就停**，多发一发都不行。

        本用例：我已占 1 个、``teamLimit=2`` ⇒ 额度 1；名册里够 2 个坑的角色，
        但**只能发 1 次** 21043。
        """
        pit_res = new_pit_res_payload(
            team_limit=2,
            team=[
                team_pit_bytes(
                    93,
                    slots=[
                        slot_bytes(ROLE_INFO_KURTIS, 1, MY_PLAYER_ID),  # 我已占的坑
                        slot_bytes(ROLE_INFO_ZHANGCHEN, 1, EMPTY_MEMBER_PID),  # 空，匹配
                    ],
                ),
                team_pit_bytes(
                    94,
                    slots=[
                        slot_bytes(ROLE_INFO_KURTIS, 1, EMPTY_MEMBER_PID),  # 空，匹配
                        slot_bytes(ROLE_INFO_ZHANGCHEN, 1, EMPTY_MEMBER_PID),  # 空，匹配
                    ],
                ),
            ],
        )
        roster = roster_payload(
            [role_bytes(841, ROLE_MAIN_KURTIS, 50), role_bytes(842, ROLE_MAIN_ZHANGCHEN, 50)]
        )
        recorded = patch_transport(monkeypatch, pit_res=pit_res, roster=roster)
        task = create_task("alliance_mining_team", make_session())
        result = task.run()

        packets = [
            AllianceNewPitSetRolePacket.decode(parse_envelope(b).sub_packet_bytes)
            for b in recorded
            if parse_envelope(b).packet_id == PACKET_SET_ROLE
        ]
        assert len(packets) == 1, "额度只剩 1 个，不该发第 2 发"
        assert result.ok is True
        assert "占坑 1 次" in result.message
        assert result.data["team_limit"] == 2
        assert result.data["already_mine"] == 1
        assert result.data["allowance"] == 1

    def test_exhausted_quota_skips_without_sending(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """★ 已占数 == teamLimit 时**一个占坑包都不发**，也**不拉名册**。

        这就是"只发送能填进坑的数据"——额度为 0 时再发任何 21043 都只会被拒。
        """
        pit_res = new_pit_res_payload(
            team_limit=1,
            team=[
                team_pit_bytes(
                    95,
                    slots=[
                        slot_bytes(ROLE_INFO_KURTIS, 1, MY_PLAYER_ID),  # 我已占 1 个 = teamLimit
                        slot_bytes(ROLE_INFO_ZHANGCHEN, 1, EMPTY_MEMBER_PID),  # 空，但没额度了
                    ],
                ),
            ],
        )
        recorded = patch_transport(
            monkeypatch,
            pit_res=pit_res,
            roster=roster_payload([role_bytes(851, ROLE_MAIN_ZHANGCHEN, 50)]),
        )
        task = create_task("alliance_mining_team", make_session())
        result = task.run()

        assert sent_packet_ids(recorded) == [PACKET_NEW_PIT], "额度为 0 时只发读状态的 21039"
        assert PACKET_ROSTER not in sent_packet_ids(recorded), "没必要拉名册"
        assert result.ok is True, "次数用尽是正常状态，不算失败"
        assert "今日团队矿次数已用尽" in result.message
        assert "上限 1" in result.message and "已占 1" in result.message
        assert result.data["skipped"] is True
        assert result.data["allowance"] == 0

    def test_unknown_team_limit_does_not_block(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """★ 服务端没下发 ``teamLimit``（<=0）时**不许**当成 0 而什么都不做。

        那会变成"读不到配额就静默漏做" —— 比多发几次糟得多。
        此时退化为"服务端裁决"（= 上一轮的行为）并告警。
        """
        pit_res = new_pit_res_payload(
            team_limit=0,
            team=[
                team_pit_bytes(
                    96,
                    slots=[
                        slot_bytes(ROLE_INFO_KURTIS, 1, EMPTY_MEMBER_PID),
                        slot_bytes(ROLE_INFO_ZHANGCHEN, 1, EMPTY_MEMBER_PID),
                    ],
                ),
            ],
        )
        roster = roster_payload(
            [role_bytes(861, ROLE_MAIN_KURTIS, 50), role_bytes(862, ROLE_MAIN_ZHANGCHEN, 50)]
        )
        recorded = patch_transport(monkeypatch, pit_res=pit_res, roster=roster)
        task = create_task("alliance_mining_team", make_session())
        result = task.run()

        assert PACKET_SET_ROLE in sent_packet_ids(recorded), "读不到配额也要照占"
        assert result.data["allowance"] is None, "None = 没设闸门"
        assert "没给出团队矿次数上限" in result.message

    def test_absurd_team_limit_is_clamped_and_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """★ 服务端给出比游戏已知最大值还大的上限 → **夹到 4 并留痕**。

        游戏内公会表里"挖礦次數"这一列最大值就是 4（LV7~LV10），10.3 抓包
        ``teamLimit`` 也正好是 4。真收到更大的数，要么游戏改版、要么字段读错了
        —— 无论哪种，都**不能**闷头发一堆请求，也**不能**静默少占。
        """
        slots = [
            slot_bytes(ROLE_INFO_KURTIS, 1, EMPTY_MEMBER_PID) for _ in range(6)
        ]
        pits = [
            team_pit_bytes(200 + i, slots=[slots[i]]) for i in range(6)
        ]
        pit_res = new_pit_res_payload(team_limit=99, team=pits)
        roster = roster_payload(
            [role_bytes(900 + i, ROLE_MAIN_KURTIS, 50) for i in range(6)]
        )
        recorded = patch_transport(monkeypatch, pit_res=pit_res, roster=roster)
        task = create_task("alliance_mining_team", make_session())
        result = task.run()

        assert result.data["allowance"] == 4, "夹到游戏已知最大值"
        assert sent_packet_ids(recorded).count(PACKET_SET_ROLE) == 4
        assert "超过已知最大值" in result.message, "夹了就要说，不许静默"
        assert result.data["team_limit"] == 99, "服务端原值要如实记录"

    # ------------------------------------------------------------------
    # ★★ 每坑我最多占 1 个（2026-10-05 晚新增）★★
    #
    # 游戏内文案：「每個團隊挖礦公會成員**最多僅能上陣1個**自身擁有符合指定條件的角色」
    # 客户端判据：`GetRoleJoinPitCount_Team(pitSid) < GetTeamPitRoleCountLimit()`
    # （在 `PitRoleIcon.CheckCanJoinRole` 里，与每日次数检查并列）。
    # ------------------------------------------------------------------
    def test_only_one_slot_per_pit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """同一个坑里只占 **1** 个槽位 —— 占第 2 个必然被服务端拒。

        本用例：一个坑 3 个空槽、名册有 3 个匹配角色、额度充足
        → 仍然**只发 1 次** 21043。
        """
        pit_res = new_pit_res_payload(
            team=[
                team_pit_bytes(
                    97,
                    slots=[
                        slot_bytes(ROLE_INFO_KURTIS, 1, EMPTY_MEMBER_PID),
                        slot_bytes(ROLE_INFO_KURTIS, 1, EMPTY_MEMBER_PID),
                        slot_bytes(ROLE_INFO_KURTIS, 1, EMPTY_MEMBER_PID),
                    ],
                ),
            ],
        )
        roster = roster_payload(
            [
                role_bytes(871, ROLE_MAIN_KURTIS, 50),
                role_bytes(872, ROLE_MAIN_KURTIS, 50),
                role_bytes(873, ROLE_MAIN_KURTIS, 50),
            ]
        )
        recorded = patch_transport(monkeypatch, pit_res=pit_res, roster=roster)
        task = create_task("alliance_mining_team", make_session())
        result = task.run()

        packets = [
            AllianceNewPitSetRolePacket.decode(parse_envelope(b).sub_packet_bytes)
            for b in recorded
            if parse_envelope(b).packet_id == PACKET_SET_ROLE
        ]
        assert len(packets) == 1, "一个坑最多占 1 个槽位"
        assert result.ok is True
        assert "占坑 1 次" in result.message

    def test_pit_i_already_occupy_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """我已经占了槽位的坑 → **整坑跳过**，连那个空槽也不试。

        这条同时说明"我已占的坑"不该再计入可占候选 —— 否则会白发一发。
        """
        pit_res = new_pit_res_payload(
            team=[
                team_pit_bytes(
                    98,
                    slots=[
                        slot_bytes(ROLE_INFO_KURTIS, 1, MY_PLAYER_ID),  # 我已占
                        slot_bytes(ROLE_INFO_ZHANGCHEN, 1, EMPTY_MEMBER_PID),  # 空，但坑是我的了
                    ],
                ),
            ],
        )
        roster = roster_payload([role_bytes(881, ROLE_MAIN_ZHANGCHEN, 50)])
        recorded = patch_transport(monkeypatch, pit_res=pit_res, roster=roster)
        task = create_task("alliance_mining_team", make_session())
        result = task.run()

        assert sent_packet_ids(recorded) == [PACKET_NEW_PIT], "整坑跳过，不该发占坑包"
        assert result.ok is True
        assert "没有可占的坑位" in result.message

    # ------------------------------------------------------------------
    # ★★ 连续被同一个理由拒绝就停手（2026-10-05 晚新增）★★
    # ------------------------------------------------------------------
    def test_stops_after_two_consecutive_same_rejections(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """连续 2 次同错误码 → 停止本轮，**不再白发后续请求**。

        【为什么】真机那次 3 次成功后连吃 15 个 -8，后 14 发全是白发的。
        阈值 2：单次失败可能是偶发竞争，连续两次同因就说明再发也没用。
        """
        pit_res = new_pit_res_payload(
            team=[
                team_pit_bytes(101, slots=[slot_bytes(ROLE_INFO_KURTIS, 1, EMPTY_MEMBER_PID)]),
                team_pit_bytes(102, slots=[slot_bytes(ROLE_INFO_KURTIS, 1, EMPTY_MEMBER_PID)]),
                team_pit_bytes(103, slots=[slot_bytes(ROLE_INFO_KURTIS, 1, EMPTY_MEMBER_PID)]),
            ],
        )
        roster = roster_payload(
            [
                role_bytes(891, ROLE_MAIN_KURTIS, 50),
                role_bytes(892, ROLE_MAIN_KURTIS, 50),
                role_bytes(893, ROLE_MAIN_KURTIS, 50),
            ]
        )
        recorded = patch_transport(
            monkeypatch,
            pit_res=pit_res,
            roster=roster,
            set_role_res=simple_res_payload(error_code=-8),
        )
        task = create_task("alliance_mining_team", make_session())
        result = task.run()

        assert sent_packet_ids(recorded).count(PACKET_SET_ROLE) == 2, (
            "连续 2 次同因就该停手，第 3 发不该发出去"
        )
        assert result.ok is False, "有占坑失败 → 任务失败"
        assert "占坑被拒 2 次" in result.message
        assert "已停止继续占坑" in result.message

    def test_run_view_never_shows_raw_error_codes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """★ 运行视图里**不许出现裸错误码**（用户读不懂，纯噪音）。

        但错误码不能丢：它要进 ``data["join_error_codes"]`` 供排查
        （``-8`` 至今没在任何工会枚举里找到，是**未证实**的码）。
        """
        pit_res = new_pit_res_payload(
            team=[
                team_pit_bytes(104, slots=[slot_bytes(ROLE_INFO_KURTIS, 1, EMPTY_MEMBER_PID)]),
                team_pit_bytes(105, slots=[slot_bytes(ROLE_INFO_KURTIS, 1, EMPTY_MEMBER_PID)]),
            ],
        )
        roster = roster_payload(
            [role_bytes(894, ROLE_MAIN_KURTIS, 50), role_bytes(895, ROLE_MAIN_KURTIS, 50)]
        )
        recorded = patch_transport(
            monkeypatch,
            pit_res=pit_res,
            roster=roster,
            set_role_res=simple_res_payload(error_code=-8),
        )
        task = create_task("alliance_mining_team", make_session())
        result = task.run()

        assert "-8" not in result.message, "运行视图不该出现裸错误码"
        assert "errorCode" not in result.message
        assert result.data["join_error_codes"] == (-8, -8), "错误码要在结构化 data 里留痕"
        assert len(sent_packet_ids(recorded)) > 0  # 确认真的跑过

    # ------------------------------------------------------------------
    # ★★ 汇总聚合（2026-10-05 晚新增）★★
    # ------------------------------------------------------------------
    def test_unfillable_summary_is_aggregated_not_dumped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """33 条明细不该把运行视图刷成 60 行 —— 汇总按原因聚合、明细封顶。

        【真机教训】2026-10-05 那次运行视图有 33 条 unfillable + 15 条失败，
        人根本读不了。聚合后一眼能看出"卡在哪一类"。
        """
        slots = [
            slot_bytes(ROLE_INFO_ZHANGCHEN, 1, EMPTY_MEMBER_PID) for _ in range(6)
        ]
        pit_res = new_pit_res_payload(team=[team_pit_bytes(106, slots=slots)])
        # 名册里只有庫爾蒂絲 → 6 个槽位全是"名册里没有这个角色"
        roster = roster_payload([role_bytes(896, ROLE_MAIN_KURTIS, 50)])
        recorded = patch_transport(monkeypatch, pit_res=pit_res, roster=roster)
        task = create_task("alliance_mining_team", make_session())
        result = task.run()

        assert "无法占坑 6 个" in result.message
        assert "名册里没有这个角色 ×6" in result.message, "要按原因聚合计数"
        assert "另 3 个见开发者日志" in result.message, "明细要封顶（最多列 3 条）"
        assert result.ok is True
        assert PACKET_SET_ROLE not in sent_packet_ids(recorded)

    def test_full_pit_is_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """没有空槽位的坑 → 无事可做，不发占坑包，也**不拉名册**（没必要）。"""
        pit_res = new_pit_res_payload(
            team=[
                team_pit_bytes(
                    91,
                    slots=[
                        slot_bytes(ROLE_INFO_KURTIS, 1, 999),
                        slot_bytes(ROLE_INFO_ZHANGCHEN, 1, 888),
                    ],
                ),
            ],
        )
        recorded = patch_transport(monkeypatch, pit_res=pit_res)
        task = create_task("alliance_mining_team", make_session())
        result = task.run()

        assert sent_packet_ids(recorded) == [PACKET_NEW_PIT]
        assert result.ok is True

    def test_mining_pit_is_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """已在挖掘中的团体矿不占坑。"""
        pit_res = new_pit_res_payload(
            team=[
                team_pit_bytes(
                    44,
                    slots=[slot_bytes(ROLE_INFO_KURTIS, 1, EMPTY_MEMBER_PID)],
                    end_time=FUTURE_MS,  # ★ 毫秒
                ),
            ],
        )
        recorded = patch_transport(
            monkeypatch, pit_res=pit_res, roster=roster_payload([])
        )
        task = create_task("alliance_mining_team", make_session())
        result = task.run()

        assert sent_packet_ids(recorded) == [PACKET_NEW_PIT], "挖掘中的矿不需要拉名册"
        assert result.ok is True
        assert "挖掘中 1 个" in result.message

    def test_no_start_packet_is_ever_sent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """★ 21045 已从流程移除（满员自动开矿）—— 任何情况下都不应出现。"""
        assert PACKET_START not in sent_packet_ids(
            patch_transport(
                monkeypatch,
                pit_res=new_pit_res_payload(
                    team=[
                        team_pit_bytes(
                            92,
                            slots=[
                                slot_bytes(ROLE_INFO_KURTIS, 1, EMPTY_MEMBER_PID),
                                slot_bytes(ROLE_INFO_ZHANGCHEN, 1, EMPTY_MEMBER_PID),
                            ],
                        ),
                    ],
                ),
                roster=roster_payload(
                    [role_bytes(831, ROLE_MAIN_KURTIS, 50), role_bytes(832, ROLE_MAIN_ZHANGCHEN, 50)]
                ),
            )
        )

    def test_dry_run_sends_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded: list[bytes] = []

        def fake_post_raw(self: GameClient, body: bytes) -> FakeResponse:
            recorded.append(body)
            raise AssertionError("演练模式不该发出任何请求")

        monkeypatch.setattr(GameClient, "post_raw", fake_post_raw)
        task = create_task("alliance_mining_team", make_session(), dry_run=True)
        result = task.run()

        assert recorded == []
        assert result.ok is True
        assert "演练" in result.message


if __name__ == "__main__":  # 支持 `python -m tests.test_tasks_alliance_mining`
    raise SystemExit(pytest.main([__file__, "-v", "--no-header"]))
