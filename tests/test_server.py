"""区服（``models/server.py``）的单元测试 —— **真实字节 + 合成场景两部分**。

运行方式：

    python -m tests.test_server
    python -m pytest tests/test_server.py -q

【两类用例各管什么】

1. **真实字节**（``TestRealCapture``）：拿抓包里那条 8872 字节的 1002 响应
   （夹具内联的前 256 字节）解区服列表。它证明的不是"代码自洽"，
   而是"我们真的能从服务端的字节里读出区服编号"—— 这是选服能落地的前提。
2. **合成场景**（其余）：区服的取舍规则（显式指定 / 建议区 / 上次登录 / 兜底）、
   边界（列表为空、指定的区不存在）这些分支在抓包里只出现一两种，
   靠合成数据才能把它们全部钉死。
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

import config
from models.envelope import GameEnvelope, parse_envelope
from models.proto_fields import field_tags, tag_of
from models.protobuf_wire import fields_by_number
from models.server import (
    AccountClientInfoLoginPacket,
    AccountClientInfoLoginRes,
    EquipClass,
    LoginPacket,
    LoginRes,
    ServerListClass,
    ServerSelectError,
    TeamClass,
    group_servers,
)
from models.handshake import SpecialActivityRefreshClass
from tasks.login import build_login_packet, choose_server_for_login
from protocol_samples.gateway_samples import SAMPLES
from protocol_samples.session_samples import (
    LOGIN_REQUEST_ACTIVE_MISSION_MD5,
    LOGIN_REQUEST_EXTRA_STATE_LENGTH,
    LOGIN_REQUEST_FIELDS,
    LOGIN_REQUEST_LENGTH,
    LOGIN_REQUEST_PREFIX_HEX,
    LOGIN_REQUEST_SUBPACKET_HEADER_HEX,
    LOGIN_REQUEST_SUBPACKET_LENGTH,
    LOGIN_REQUEST_TIMESTAMP_CLIENT,
)
from tools.extract_har import HAR_FILES
from tools.show_servers import (
    build_server_table,
    find_in_captures,
    resolve_capture_dir,
    select_and_describe,
)

#: 登录响应（区服列表）的消息号
LOGIN_RES_ID = 1002


def sample_with(packet_id: int) -> dict[str, Any]:
    """按响应消息号取抓包样本（刻意不用下标，见 test_game_envelope.py 的说明）。"""
    for sample in SAMPLES:
        if sample["response_packet_id"] == packet_id:
            return sample
    raise AssertionError(f"抓包夹具里没有消息号 {packet_id} 的样本")


def captured_login_response() -> AccountClientInfoLoginRes:
    """把抓包里的登录响应解成模型。

    .. note::
       这里**必须**打开 ``tolerate_truncation``：夹具只内联了那条 8872 字节响应的
       前 256 字节（见 tools/extract_har.py 的 PREVIEW_BYTES），而区服列表包
       在线上声明了 8781 字节 —— 严格模式会直接判「数据被截断」而拒绝解析。
       好消息是：**前 3 个区服条目完整地落在这 256 字节里**，
       所以"能读出真实的区服编号"这件事依然成立（这正是要验证的）。
    """
    view = parse_envelope(
        bytes.fromhex(sample_with(LOGIN_RES_ID)["response_hex"]),
        tolerate_truncation=True,
    )
    assert view.class_name == "AccountClientInfoLoginRes"
    return view.decode_sub_packet(AccountClientInfoLoginRes)


def make_server(
    server_id: int,
    *,
    name: str = "",
    group: int = 1,
    character: str = "",
    note: str = "",
) -> ServerListClass:
    """造一个区服条目（合成场景用）。

    只填需要断言的字段，其余留默认值 —— 这样用例的意图一眼可见，
    也不会因为以后给模型加字段而全部失效。
    """
    return ServerListClass(
        serverID=server_id,
        serverName=name or f"区服{server_id}",
        serverGroupID=group,
        playerInfo=character,
        serverNote=note,
    )


# ---------------------------------------------------------------------------
# 字段编号（错了就全盘皆错，必须单测）
# ---------------------------------------------------------------------------
class TestFieldTags:
    def test_server_list_class_tags(self) -> None:
        """``ServerListClass`` 的 7 个字段编号 = 声明顺序（实测字节已印证）。"""
        assert field_tags("ServerListClass") == {
            "serverID": 1,
            "serverName": 2,
            "serverState": 3,
            "serverNote": 4,
            "people_count": 5,
            "playerInfo": 6,
            "serverGroupID": 7,
        }

    def test_all_server_list_is_field_three(self) -> None:
        """区服列表在 1002 响应里的编号是 3 —— 抓包字节里就是 ``1a``。"""
        assert tag_of("AccountClientInfoLoginRes", "allServerList") == 3
        assert tag_of("AccountClientInfoLoginRes", "suggestedServer") == 4
        assert tag_of("AccountClientInfoLoginRes", "lastLoginedServerList") == 5

    def test_login_packet_server_id_is_field_two(self) -> None:
        """**选服结果就写在这里**：LoginPacket 的字段 2（线上字节是 ``10``）。"""
        assert tag_of("LoginPacket", "serverID") == 2

    def test_models_declare_every_field_in_table(self) -> None:
        """模型必须声明字段表里的**每一个**字段，否则编码时会报错。"""
        for model in (ServerListClass, AccountClientInfoLoginRes, LoginPacket):
            expected = set(field_tags(model.proto_class_name))
            missing = expected - set(model.model_fields)
            assert not missing, f"{model.__name__} 缺少字段：{sorted(missing)}"


# ---------------------------------------------------------------------------
# 1004 LoginRes 的初始化清单（★ 2026-10-05：英雄数据就藏在这里）
# ---------------------------------------------------------------------------
class TestLoginResInitLists:
    """``LoginRes`` 里除令牌外还有四份"家当快照"，编号错一位就会静默读错。

    【为什么这些字段值得单测】
    ``LoginRes`` **只解码、不编码**，所以 ``encode()`` 那条"模型必须声明所有
    字段"的校验**碰不到它** —— 漏声明或声明错位置都**不会报错**，
    只会安静地拿不到数据（这正是"英雄数据一直取不到"的原因）。
    字段编号一旦错位，解码器会把别的东西读成角色列表，而且**没有任何异常**。
    """

    def test_init_list_tags_are_four_through_seven(self) -> None:
        """装备(4) / 角色(5) / 背包(6) / 队伍(7) —— 与 dump.cs 声明顺序一致。"""
        assert tag_of("LoginRes", "equipInitClass") == 4
        assert tag_of("LoginRes", "roleInitClass") == 5
        assert tag_of("LoginRes", "itemInitClass") == 6
        assert tag_of("LoginRes", "teamInitClass") == 7

    def test_role_init_class_decodes_as_role_class(self) -> None:
        """字段 5 解出来必须是 ``RoleClass``（角色实例），不是别的列表。"""
        from models.protobuf_wire import encode_int, encode_message
        from models.role import RoleClass

        # 一个 50 级 5 星的角色实例
        role = RoleClass(roleSid=155242087, roleID=130003000, roleLV=50, roleStar=5)
        payload = encode_message(5, role.encode()) + encode_int(1, 0)

        decoded = LoginRes.decode(payload)
        assert len(decoded.roleInitClass) == 1
        assert decoded.roleInitClass[0].roleSid == 155242087
        assert decoded.roleInitClass[0].roleID == 130003000
        assert decoded.roleInitClass[0].roleLV == 50
        assert decoded.roleInitClass[0].roleStar == 5

    def test_unknown_init_fields_do_not_break_decoding(self) -> None:
        """``LoginRes`` 里那 21 个**未建模**的初始化字段必须被安全忽略。

        服务端每次登录都会把它们发全（85 KB 的响应），少忽略一个就解析失败。
        """
        from models.protobuf_wire import encode_int

        payload = encode_int(1, 0) + encode_int(8, 12345) + encode_int(9, 7)
        decoded = LoginRes.decode(payload)
        assert decoded.errorCode == 0
        assert decoded.roleInitClass == []

    def test_equip_and_team_models_have_their_wire_fields(self) -> None:
        """装备 / 队伍两个新模型与字段表一一对应（缺一个就无法编码）。"""
        assert set(EquipClass.model_fields) == set(field_tags("EquipClass"))
        assert set(TeamClass.model_fields) == set(field_tags("TeamClass"))

    def test_team_roles_sid_holds_role_instance_ids(self) -> None:
        """队伍里的 ``rolesSid`` 装的是**角色实例 sid** —— 与挖矿 ``roleSidList`` 同源。"""
        from models.protobuf_wire import encode_message

        payload = encode_message(7, TeamClass(teamID=1, rolesSid=[155242087, 223860075]).encode())
        decoded = LoginRes.decode(payload)
        assert len(decoded.teamInitClass) == 1
        assert decoded.teamInitClass[0].rolesSid == [155242087, 223860075]


# ---------------------------------------------------------------------------
# 真实抓包字节
# ---------------------------------------------------------------------------
class TestRealCapture:
    def test_response_decodes_without_error(self) -> None:
        response = captured_login_response()
        assert response.errorCode == 0
        assert response.accountID > 0

    def test_server_ids_start_with_known_values(self) -> None:
        """夹具（前 256 字节）里至少能看到 1 / 2 / 3 三个区服。"""
        response = captured_login_response()
        assert len(response.allServerList) >= 3
        assert response.server_ids()[:3] == (1, 2, 3)

    def test_first_three_server_names_have_expected_lengths(self) -> None:
        """名字长度来自字节里的长度前缀（12 0C / 12 06 / 12 09）。

        刻意只断言长度而不断言具体文字：这样即使游戏改名，测试也不会误红
        （改名不是我们的问题），但**字段错位**一定能让它红 —— 错位会让长度变得离谱。
        """
        servers = captured_login_response().allServerList
        assert [len(server.serverName) for server in servers[:3]] == [4, 2, 3]

    def test_first_three_share_group_one(self) -> None:
        servers = captured_login_response().allServerList
        assert [server.serverGroupID for server in servers[:3]] == [1, 1, 1]

    def test_no_character_in_captured_account(self) -> None:
        """抓包账号在这些区都还没有角色（``playerInfo`` 为空）。"""
        servers = captured_login_response().allServerList
        assert not any(server.has_character() for server in servers)

    def test_choose_server_returns_first_when_no_hint(self) -> None:
        """没有 suggestedServer 提示时，兜底策略选中编号最小的那个。"""
        response = captured_login_response()
        chosen = response.choose_server()
        assert chosen.serverID == min(response.server_ids())

    def test_describe_servers_mentions_count(self) -> None:
        text = captured_login_response().describe_servers()
        assert "区服列表（共" in text


# ---------------------------------------------------------------------------
# 选服规则（合成场景）
# ---------------------------------------------------------------------------
def make_response(
    servers: list[ServerListClass],
    *,
    suggested: ServerListClass | None = None,
    last_logined: list[ServerListClass] | None = None,
    error_code: int = 0,
) -> AccountClientInfoLoginRes:
    """造一个 1002 响应（只填用例关心的部分）。"""
    return AccountClientInfoLoginRes(
        errorCode=error_code,
        accountID=12345,
        allServerList=servers,
        suggestedServer=suggested,
        lastLoginedServerList=list(last_logined or []),
    )


class TestChooseServer:
    def test_explicit_id_wins(self) -> None:
        response = make_response([make_server(1), make_server(2)], suggested=make_server(1))
        assert response.choose_server(server_id=2).serverID == 2

    def test_explicit_id_not_found_lists_available(self) -> None:
        """指定的区服不存在时必须报错并列出可用编号 —— 绝不「帮你换一个」。"""
        response = make_response([make_server(1), make_server(5)])
        with pytest.raises(ServerSelectError) as excinfo:
            response.choose_server(server_id=9)
        message = str(excinfo.value)
        assert "serverID=9" in message
        assert "1、5" in message
        assert "CHERRYTALE_SERVER_ID" in message

    def test_suggested_is_preferred(self) -> None:
        response = make_response(
            [make_server(1), make_server(2), make_server(3)], suggested=make_server(3)
        )
        assert response.choose_server().serverID == 3

    def test_suggested_can_be_ignored(self) -> None:
        response = make_response([make_server(1), make_server(3)], suggested=make_server(3))
        assert response.choose_server(prefer_suggested=False).serverID == 1

    def test_last_logined_used_when_no_suggested(self) -> None:
        history = [make_server(2), make_server(3)]
        response = make_response(
            [make_server(1), make_server(2), make_server(3)], last_logined=history
        )
        assert response.choose_server().serverID == 2

    def test_fallback_is_smallest_id(self) -> None:
        """连建议区与历史记录都没有时取编号最小的（保证新号也能进游戏）。"""
        response = make_response([make_server(7), make_server(4)])
        assert response.choose_server().serverID == 4

    def test_group_filter_is_respected_by_suggested(self) -> None:
        """建议区不在指定分组里时**不能**绕过分组 —— 否则行为自相矛盾。"""
        response = make_response(
            [make_server(1, group=1), make_server(2, group=2)],
            suggested=make_server(1, group=1),
        )
        assert response.choose_server(group_id=2).serverID == 2

    def test_prefer_with_character(self) -> None:
        response = make_response([make_server(1), make_server(2, character="玩家名|Lv.30")])
        assert response.choose_server(prefer_with_character=True).serverID == 2

    def test_empty_list_reports_error_code(self) -> None:
        """列表为空是「账号登录没成功」的典型症状，报错要把 errorCode 带出来。"""
        response = make_response([], error_code=1002)
        with pytest.raises(ServerSelectError) as excinfo:
            response.choose_server()
        assert "errorCode=1002" in str(excinfo.value)

    def test_empty_group_reports_existing_groups(self) -> None:
        response = make_response([make_server(1, group=1), make_server(2, group=1)])
        with pytest.raises(ServerSelectError) as excinfo:
            response.choose_server(group_id=9)
        message = str(excinfo.value)
        assert "分组号 9" in message
        assert "[1]" in message

    def test_empty_when_character_required(self) -> None:
        response = make_response([make_server(1), make_server(2)])
        with pytest.raises(ServerSelectError, match="已有角色"):
            response.choose_server(prefer_with_character=True)


class TestGrouping:
    def test_group_servers_splits_and_sorts(self) -> None:
        servers = [make_server(3, group=2), make_server(1, group=1), make_server(2, group=2)]
        groups = group_servers(servers)
        assert [group.group_id for group in groups] == [1, 2]
        assert groups[1].ids() == (2, 3)

    def test_group_servers_accepts_empty(self) -> None:
        assert group_servers([]) == ()


# ---------------------------------------------------------------------------
# 生成登录包（1003）
# ---------------------------------------------------------------------------
class TestLoginPacketForServer:
    def test_server_id_is_encoded_at_tag_two(self) -> None:
        """**核心断言**：选中的 ``serverID`` 必须落在编码字节的字段 2 上。"""
        packet = LoginPacket.for_server(make_server(3), account_id=6265757)
        grouped = fields_by_number(packet.encode())
        assert grouped[2][0].as_int32() == 3
        assert grouped[1][0].as_int32() == 6265757

    def test_zero_server_id_is_rejected(self) -> None:
        """``serverID = 0`` 在协议里是「未指定」，必须在组包阶段就拦下。"""
        with pytest.raises(ServerSelectError, match="未指定"):
            LoginPacket.for_server(make_server(0))

    def test_token_is_masked_in_repr(self) -> None:
        packet = LoginPacket.for_server(make_server(1), auer_web_login_token="SUPER-SECRET")
        assert "SUPER-SECRET" not in str(packet)
        assert "***" in str(packet)

    def test_login_token_defaults_to_dash_one(self) -> None:
        """``auerWebLogin_token`` 的默认值是字符串 ``"-1"``，**不是空串**。

        实测依据：真实报文的字段 4 编码为 ``22 02 2d 31``（= ``"-1"``）。
        写成空串会让整包少 2 字节，服务端回 ``ABNORMAL_PACKET``。
        """
        packet = LoginPacket.for_server(make_server(98), account_id=10000004)
        assert packet.auerWebLogin_token == "-1"

    def test_hami_sub_no_list_carries_platform_user_id(self) -> None:
        """``hamiSubNoList`` 必须带平台 userId —— 服务端靠它定位账号。

        这是我们踩过的**最大的一个坑**：该字段一直是空列表，于是 1003 永远被拒，
        而服务端只回一句笼统的 ``ABNORMAL_PACKET``，从错误里看不出任何线索。
        字段名里的 "Hami" 是历史遗留，它承载的实际是账号凭据。
        """
        user_id = "ER00000000-0000-0000-0000-000000000000"
        packet = LoginPacket.for_server(
            make_server(98), account_id=10000004, platform_user_id=user_id
        )
        assert packet.hamiSubNoList == [user_id]

    def test_no_platform_user_id_keeps_list_empty(self) -> None:
        """没传平台 userId 时保持空列表 —— 不能塞空串，那会多编码一个字段出来。"""
        packet = LoginPacket.for_server(make_server(98))
        assert packet.hamiSubNoList == []

    def test_full_request_matches_captured_length(self) -> None:
        """整包（信封 + 子包）长度应与真实报文一致（**178 字节**）。

        用它挡住"字段漏发 / 多发"这类结构性问题：
        信封 98 字节（packetID + settingMd5 + timeStampToken + timeStampClient +
        vCode + activity）+ 子包 80 字节（LoginPacket 的 8 个字段）= 178。
        """
        packet = LoginPacket.for_server(
            make_server(98),
            account_id=10000004,
            platform_user_id="ER00000000-0000-0000-0000-000000000000",
            channel_platform_type=2,
            client_version="2.2.0-el-h-win",
            language_code="zhcn",
        )
        envelope = GameEnvelope()
        envelope.activity = SpecialActivityRefreshClass(
            activeMission="2DB9B982CA690276061B90E63F3C3D38", puzzleRoleGroup=""
        )
        body = envelope.encode_with(packet, include_defaults=True)
        assert len(body) == 178


class TestEnvelopeIntegration:
    def test_login_packet_round_trips_through_envelope(self) -> None:
        """装进 RootPacket 再解出来，消息号与 serverID 都不能变（离线端到端）。"""
        packet = LoginPacket.for_server(make_server(2), account_id=42)
        body = GameEnvelope().encode_with(packet)
        view = parse_envelope(body)
        assert view.packet_id == 1003
        assert view.class_name == "LoginPacket"
        decoded = view.decode_sub_packet(LoginPacket)
        assert decoded.serverID == 2
        assert decoded.accountID == 42


# ---------------------------------------------------------------------------
# 与 config / tasks 的接线（配置改了必须真的生效）
# ---------------------------------------------------------------------------
class TestConfigWiring:
    def test_configured_server_id_is_used(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "SERVER_ID", 2)
        monkeypatch.setattr(config, "SERVER_GROUP_ID", None)
        response = make_response([make_server(1), make_server(2), make_server(3)])
        assert choose_server_for_login(response).serverID == 2
        assert build_login_packet(response).serverID == 2

    def test_unknown_configured_server_id_fails_loudly(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "SERVER_ID", 99)
        monkeypatch.setattr(config, "SERVER_GROUP_ID", None)
        with pytest.raises(ServerSelectError):
            choose_server_for_login(make_response([make_server(1)]))

    def test_group_id_from_config_is_used(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "SERVER_ID", None)
        monkeypatch.setattr(config, "SERVER_GROUP_ID", 2)
        # 两个区都要「有角色」：选服默认只在**有角色的区**里挑
        # （见 choose_server_for_login 的说明），否则过滤后没有候选，会直接报错。
        response = make_response(
            [
                make_server(1, group=1, character="A"),
                make_server(2, group=2, character="B"),
            ]
        )
        assert choose_server_for_login(response).serverID == 2

    def test_build_login_packet_fills_account_id(self, monkeypatch) -> None:
        """组包时 ``accountID`` 必须自动带过来（它是 1003 的字段 1）。"""
        monkeypatch.setattr(config, "SERVER_ID", None)
        monkeypatch.setattr(config, "SERVER_GROUP_ID", None)
        packet = build_login_packet(make_response([make_server(1, character="A")]))
        assert packet.accountID == 12345

    def test_summary_is_human_readable(self) -> None:
        text = config.server_selection_summary()
        assert isinstance(text, str) and text

    def test_account_login_request_model_can_be_built(self) -> None:
        """1001 请求模型要能被构造（读懂抓包的前提）。"""
        request = AccountClientInfoLoginPacket(deviceID="x", clientVersion="2.2.0-el-h-win")
        assert request.deviceID == "x"

    def test_account_login_request_masks_credentials(self) -> None:
        request = AccountClientInfoLoginPacket(socialAccount="acc", passWord="pwd")
        assert "pwd" not in str(request)


# ---------------------------------------------------------------------------
# 完整抓包（未截断）—— 夹具预览覆盖不到的部分
# ---------------------------------------------------------------------------
_CAPTURE_DIR = resolve_capture_dir(None)

#: 抓包目录里是否有 HAR（没有就跳过整个类，因为 fixtures 只有 256 字节预览）
_HAS_CAPTURE = any((_CAPTURE_DIR / name).is_file() for name in HAR_FILES)


# ---------------------------------------------------------------------------
# 新账号（所有区都无角色）的兜底：进服务端建议的区
# ---------------------------------------------------------------------------
class TestNewAccountServerFallback:
    """``choose_server_for_login`` 比 ``choose_server`` 多出来的一层。

    【为什么必须有这一层】全新账号在每个区都没有角色，而本函数默认
    ``prefer_with_character=True``（进自己玩过的区）→ 候选为空 → 直接报错。
    真实场景：用新账号跑 ``main.py run login`` 时会卡在这里。
    """

    @staticmethod
    def no_character_response(suggested_id: int) -> AccountClientInfoLoginRes:
        """三个区都没角色，服务端建议 ``suggested_id``。"""
        return make_response(
            [make_server(1), make_server(3), make_server(7)],
            suggested=make_server(suggested_id),
        )

    def test_falls_back_to_suggested_server(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(config, "SERVER_ID", None)
        monkeypatch.setattr(config, "SERVER_GROUP_ID", None)
        with caplog.at_level(logging.WARNING, logger="tasks.login"):
            chosen = choose_server_for_login(self.no_character_response(3))

        assert chosen.serverID == 3, "应当退一步用服务端建议的区"
        assert "没有角色" in caplog.text, "兜底必须留痕"
        assert "创建角色" in caplog.text, "要提醒首次进区会建角色"

    def test_explicit_server_that_does_not_exist_still_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """显式指定了区服却没命中 → 原样报错，**绝不兜底成别的区**。

        这是「绝不悄悄帮你换一个」原则的边界：兜底只服务于"没指定"的场景。
        """
        monkeypatch.setattr(config, "SERVER_ID", 999)
        with pytest.raises(ServerSelectError):
            choose_server_for_login(self.no_character_response(3))

    def test_account_with_character_keeps_old_behaviour(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """有角色时行为不变：仍然优先「自己玩过的区」，不会被兜底带到新区。"""
        monkeypatch.setattr(config, "SERVER_ID", None)
        monkeypatch.setattr(config, "SERVER_GROUP_ID", None)
        response = make_response(
            [make_server(1, character="role"), make_server(3)], suggested=make_server(3)
        )
        assert choose_server_for_login(response).serverID == 1


@pytest.mark.skipif(not _HAS_CAPTURE, reason="抓包文件不在（captures/ 未同步时跳过）")
class TestFullCapture:
    """用**完整**响应做严格解析（不容忍截断）。

    夹具里的预览是截断的，所以「整条 8781 字节的区服列表能严格解析通」这件事
    只有真实抓包能证明 —— 它也是"选服在真实数据上确实可用"的最终依据。
    """

    @staticmethod
    def _response() -> AccountClientInfoLoginRes:
        found = find_in_captures(_CAPTURE_DIR)
        assert found is not None, "抓包里没有 1002 响应"
        _source, body = found
        # 注意：**没有** tolerate_truncation —— 真实响应必须能严格解析
        return parse_envelope(body).decode_sub_packet(AccountClientInfoLoginRes)

    def test_servers_count_is_far_more_than_preview(self) -> None:
        """完整列表远多于夹具预览里的 4 条（实测 167 条）。"""
        assert len(self._response().allServerList) > 50

    def test_some_server_has_non_zero_state(self) -> None:
        """状态字段确实会取非零值 —— 正因语义未知，才**不能**拿它做过滤。"""
        assert any(server.serverState for server in self._response().allServerList)

    def test_some_server_has_character(self) -> None:
        """``playerInfo`` 判据在真实数据上有效：确实有区服已存在角色。"""
        assert any(server.has_character() for server in self._response().allServerList)

    def test_prefer_with_character_picks_existing_character(self) -> None:
        chosen = self._response().choose_server(prefer_with_character=True)
        assert chosen.has_character()

    def test_suggested_server_is_in_list(self) -> None:
        response = self._response()
        assert response.suggestedServer is not None
        assert response.suggestedServer.serverID in response.server_ids()

    def test_named_groups_hold_at_most_three_servers(self) -> None:
        """实测：具名分组是「每 3 个区服一组」，而 ``-1`` 表示未分组（会很大）。"""
        groups = group_servers(self._response().allServerList)
        assert any(group.group_id == -1 for group in groups)
        for group in groups:
            if group.group_id == -1:
                continue
            assert len(group.servers) <= 3, f"组 {group.group_id} 有 {len(group.servers)} 个区服"


# ---------------------------------------------------------------------------
# 验证工具的选服流水线（tools/show_servers.py）
# ---------------------------------------------------------------------------
class TestShowServersTool:
    """``select_and_describe`` 是工具与模型之间的接缝，单独测一次。

    报告里的"选择依据"文案也一并断言：它是给人看的唯一线索，
    写错了会让人误判程序是按什么规则挑的。
    """

    def test_with_character_flag_picks_character_server(self) -> None:
        response = make_response([make_server(1), make_server(2, character="角色|Lv.30")])
        packet, text, error = select_and_describe(
            response, server_id=None, group_id=None, prefer_with_character=True
        )
        assert error is None
        assert packet is not None and packet.serverID == 2
        assert "本账号有角色" in text

    def test_error_is_returned_not_raised(self) -> None:
        packet, _text, error = select_and_describe(
            make_response([]), server_id=None, group_id=None
        )
        assert packet is None
        assert error is not None and "选服失败" in error

    def test_server_table_rows(self) -> None:
        rows = build_server_table(make_response([make_server(3, name="桃樂絲")]))
        assert rows[0].startswith("| 编号")
        assert "| 3 | 桃樂絲 |" in rows[2]


# ---------------------------------------------------------------------------
# 1001 登录请求：用真实字节验证「取值」与「字节布局」
# ---------------------------------------------------------------------------
class TestLoginRequestRealBytes:
    """用 330 字节的真实抓包验证 1001 的构造。

    【这组测试证明什么】
    在拿到这份字节之前，1001 的 15 个字段只能靠声明顺序推断（编号可信、取值未知）。
    现在可以断言三个不同层次的事实：

    1. **字段值**：模型填进去的值，就是抓包里的值；
    2. **字节布局**：编码出来的前缀与子包标签与抓包逐字节一致；
    3. **长度自洽**：子包恰好 228 字节 —— 这个数字本身就是"字段没多没少"的证据。

    第 3 条尤其值得留意：15 个字段逐个核对能对，但**长度对不上**依然说明有问题
    （比如某个字段被重复编码、或漏了一个空字段）。两者互为交叉验证。
    """

    def test_for_platform_user_fills_captured_values(self) -> None:
        """``for_platform_user`` 填出来的每个字段都应与抓包一致。

        这是最关键的一条：它证明「**用平台 userId 就能登录**」这个结论成立，
        也就是说 ``config.ACCOUNT_TOKEN`` 已经够用，不需要网页登录体系。
        """
        packet = AccountClientInfoLoginPacket.for_platform_user(
            LOGIN_REQUEST_FIELDS["socialAccount"],
            device_id=LOGIN_REQUEST_FIELDS["deviceID"],
            device_model=LOGIN_REQUEST_FIELDS["deviceModel"],
            os_version=LOGIN_REQUEST_FIELDS["osVersion"],
        )
        for name, expected in LOGIN_REQUEST_FIELDS.items():
            actual = getattr(packet, name)
            if name == "isRoot":
                # 抓包里是 varint 0，模型里是 bool —— 语义相同，单独断言
                assert actual is False, "抓包 isRoot=0，模型应为 False"
                assert expected == 0
                continue
            assert actual == expected, f"字段 {name} 与抓包不一致：{actual!r} != {expected!r}"

    def test_sub_packet_length_matches_capture(self) -> None:
        """子包恰好 228 字节 —— 抓包里的长度字段写的就是这个数。"""
        packet = AccountClientInfoLoginPacket(**LOGIN_REQUEST_FIELDS)
        # 1001 要**连默认值一起发** —— 实测抓包里显式带了 sid=0 / isRoot=0 / 空密码；
        # 用 False 会少 4 字节（少两个 ``70 00`` ``78 00``），长度就对不上了。
        assert (
            len(packet.encode(include_defaults=True))
            == LOGIN_REQUEST_SUBPACKET_LENGTH
        )

    def test_envelope_prefix_and_length(self) -> None:
        """带上字段 16 后，整体长度与抓包**精确相等**（330）。"""
        body = self._encode()
        assert body[:3].hex() == "08e907", "packetID 应为 1001"
        # 用 startswith 而不是按固定长度切片：切片长度写错会让测试失败得很像"协议不对"，
        # 而这其实只是测试自己数错了字节数。
        assert body.startswith(bytes.fromhex(LOGIN_REQUEST_PREFIX_HEX))
        assert len(body) == LOGIN_REQUEST_LENGTH, "应与抓包等长"
        assert bytes.fromhex("820124") in body, "字段 16 的标签与长度应为 82 01 24"

    def test_without_state_packet_is_shorter(self) -> None:
        """不带字段 16 时会短 39 字节 —— 这正是当初误判"字段编号错了"的原因。

        把这条差异钉成测试，是为了记住一个教训：
        **长度不符时先怀疑"漏发了哪个包"，而不是先怀疑字段编号**。
        """
        envelope = GameEnvelope(
            setting_md5="-1",
            time_stamp_token=-1,
            v_code="00" * 16,
            time_stamp_client=LOGIN_REQUEST_TIMESTAMP_CLIENT,
        )
        body = envelope.encode_with(
            AccountClientInfoLoginPacket(**LOGIN_REQUEST_FIELDS),
            include_defaults=True,
        )
        assert len(body) == LOGIN_REQUEST_LENGTH - LOGIN_REQUEST_EXTRA_STATE_LENGTH

    def test_sub_packet_slot_marker(self) -> None:
        """子包槽位标签 ``CA 3E E4 01``：字段编号 1001、长度 228。

        它同时再次印证「**子包槽位编号 = 消息号**」：
        ``1001 << 3 | 2 = 8010``，varint 编码即 ``CA 3E``。
        """
        assert LOGIN_REQUEST_SUBPACKET_HEADER_HEX in self._encode().hex()

    def test_roundtrip_through_envelope(self) -> None:
        """自己编出来的包，能原样解回去（且 deviceID 取自 config）。"""
        view = parse_envelope(self._encode())
        assert view.packet_id == 1001
        assert view.class_name == "AccountClientInfoLoginPacket"
        decoded = view.decode_sub_packet(AccountClientInfoLoginPacket)
        assert decoded.socialAccount == LOGIN_REQUEST_FIELDS["socialAccount"]
        assert decoded.deviceID == config.DEVICE_ID
        assert decoded.auerWebLogin_sid == 0

    @staticmethod
    def _encode() -> bytes:
        """按抓包同款信封参数编码一次。

        时间戳与 ``vCode`` 固定成已知值，这样长度与字节都能逐位比对 ——
        如果放任它们随机，就只能断言"差不多"，那等于没测。
        """
        envelope = GameEnvelope(
            setting_md5="-1",
            time_stamp_token=-1,
            v_code="00" * 16,
            time_stamp_client=LOGIN_REQUEST_TIMESTAMP_CLIENT,
            # 信封字段 16：活动指纹。抓包里 1001 就是带着它发的 ——
            # 缺了这段会短 39 字节，字节布局也对不上。
            activity=SpecialActivityRefreshClass(
                activeMission=LOGIN_REQUEST_ACTIVE_MISSION_MD5,
                puzzleRoleGroup="",
            ),
        )
        # include_defaults=True：实测 1001 会显式发送零值字段
        # （对照表见 models/envelope.py 的 encode_with 文档）
        return envelope.encode_with(
            AccountClientInfoLoginPacket(**LOGIN_REQUEST_FIELDS),
            include_defaults=True,
        )


if __name__ == "__main__":  # 支持 `python -m tests.test_server`
    raise SystemExit(pytest.main([__file__, "-v", "--no-header"]))



