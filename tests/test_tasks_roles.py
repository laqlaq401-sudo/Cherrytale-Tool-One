"""角色名册只读任务（``tasks/roles.py``）的单元测试。

运行方式：

    python -m pytest tests/test_tasks_roles.py -q

【本文件钉死什么】
1. **实时优先**：5011 拿得到非空名册就用它（``source == "live"``）；
2. **快照兜底**：5011 回空 / 报错时，退回登录 1004 存下的快照，
   并且**在结果里说明用了哪一份**（``source`` + ``fallback_reason``）——
   "这份数据是刚拉的还是登录时的"是用户判断数字对不对的前提；
3. **两条都空时如实报缺**，并给出可执行的下一步（跑 login），
   而不是安静地返回一个空列表；
4. **只读**：只发 5011，不发任何别的包；演练模式零发包。

消息号写死在本文件（不 import 生产常量）：协议编号被改动会在这里直接暴露。
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from client.game_client import GameClient
from client.session import GameSession
from models.envelope import parse_envelope
from models.protobuf_wire import encode_int, encode_message
from models.session_state import GameSessionState
from tasks.base import create_task

from tests.test_game_client import FakeResponse

import tasks.roles  # noqa: F401  导入即注册

PACKET_GET_ALL_ROLE = 5011
PACKET_GET_ALL_ROLE_RES = 5012

#: 角色 ID 的两个空间（见 ``models/game_config.RoleIdMap``）——
#: 名册里装 RoleMainID(130xxxxxx)，槽位里装 RoleInfoID(133xxxxxx)。
ROLE_MAIN_KURTIS = 130003000
ROLE_INFO_KURTIS = 133003000
ROLE_MAIN_ZHANGCHEN = 130007400
ROLE_INFO_ZHANGCHEN = 133007400


def make_session(
    *, roles: tuple[Mapping[str, int], ...] = ()
) -> GameSession:
    """构造一个**不会联网**的会话，可带登录时存下的角色快照。"""
    session = GameSession("https://example.invalid", timeout=1.0)
    session.token_store.update(uid="u1", token="tok")
    session.set_game_state(
        GameSessionState(token="tok", player_id=1, server_id=1, roles=tuple(dict(r) for r in roles))
    )
    return session


def role_bytes(role_sid: int, role_main_id: int, level: int, star: int = 1) -> bytes:
    """``RoleClass``：1 roleSid、2 roleID、3 roleLV、4 roleStar。"""
    return (
        encode_int(1, role_sid)
        + encode_int(2, role_main_id)
        + encode_int(3, level)
        + encode_int(4, star)
    )


def roster_payload(roles: list[bytes]) -> bytes:
    """5012 载荷：1 roleList（repeated）。"""
    payload = b""
    for item in roles:
        payload += encode_message(1, item)
    return payload


def envelope_bytes(packet_id: int, payload: bytes) -> bytes:
    """拼响应字节：字段 1 = 消息号，业务子包槽位编号 = 消息号本身。"""
    return encode_int(1, packet_id) + encode_message(packet_id, payload)


#: "服务端回了名册、但里面一个角色都没有"的响应：**子包非空**，只是没有 ``roleList``。
#:
#: 【为什么不直接发 0 字节子包】``GameEnvelopeView.decode_sub_packet`` 对
#: **空子包**一律抛 ``EnvelopeError``（"响应不含业务子包" —— 那条是给心跳类
#: 接口准备的）。所以"账号一个角色都没有"与"5011 被拒"在信封层是**同一个现象**，
#: 真机走的是异常路径（生产代码已兜住，见 ``test_failed_live_falls_back_to_snapshot``）。
#: 这里另造一个"有子包、但 roleList 为空"的响应，把**另一条**分支也钉住 ——
#: 例如服务端回了别的新字段（编号 99 我们不认识）却没回 roleList。
EMPTY_ROSTER_WITH_SUBPACKET: bytes = encode_int(99, 1)


def patch_transport(
    monkeypatch: pytest.MonkeyPatch,
    *,
    roster: bytes | None = None,
    error: Exception | None = None,
) -> list[bytes]:
    """把 ``GameClient.post_raw`` 换成"记录请求 + 回放 5012"的替身。"""
    recorded: list[bytes] = []

    def fake_post_raw(self: GameClient, body: bytes, **kwargs: object) -> FakeResponse:
        recorded.append(body)
        if error is not None:
            raise error
        packet_id = parse_envelope(body).packet_id
        if packet_id == PACKET_GET_ALL_ROLE:
            payload = roster if roster is not None else roster_payload([])
            return FakeResponse(envelope_bytes(PACKET_GET_ALL_ROLE_RES, payload))
        raise AssertionError(f"测试替身收到了意料之外的请求：{packet_id}")

    monkeypatch.setattr(GameClient, "post_raw", fake_post_raw)
    return recorded


def patch_role_id_map(monkeypatch: pytest.MonkeyPatch) -> None:
    """装一份内存换算表（不依赖本地 TextAsset 素材）。

    ⚠️ 必须 patch：否则用例会去读真实配置表，而真实表里
    ``130003000`` 对应的是 **庫爾蒂絲** —— 用例断言的"名字"会随游戏更新漂移。
    """
    from models.game_config import RoleIdMap

    role_map = RoleIdMap(
        parent={},
        info_of_base={ROLE_MAIN_KURTIS: (ROLE_INFO_KURTIS,), ROLE_MAIN_ZHANGCHEN: (ROLE_INFO_ZHANGCHEN,)},
        name_of_info={ROLE_INFO_KURTIS: "庫爾蒂絲", ROLE_INFO_ZHANGCHEN: "將臣"},
    )
    monkeypatch.setattr(tasks.roles, "load_role_id_map", lambda *a, **k: role_map)


def create_role_id_map_from_real_tables():
    """读**真实**配置表建换算表；缺素材返回 ``None``（调用方自行 Skip）。

    ⚠️ 与 :func:`patch_role_id_map` 相反：这里要的就是真表 —— 用来验证
    ``icon_code`` 真的能在静态目录里找到文件（这条判据离开真表就没意义）。
    """
    from models.game_config import load_role_id_map

    try:
        return load_role_id_map()
    except (OSError, ValueError):
        return None


def sent_packet_ids(recorded: list[bytes]) -> list[int]:
    """从记录的请求体里解出全部消息号（按发送顺序）。"""
    return [parse_envelope(body).packet_id for body in recorded]


# ---------------------------------------------------------------------------
# 实时名册优先
# ---------------------------------------------------------------------------
class TestLiveRoster:
    @pytest.fixture(autouse=True)
    def _role_map(self, monkeypatch: pytest.MonkeyPatch) -> None:
        patch_role_id_map(monkeypatch)

    def test_uses_live_roster_when_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """5011 有数据 → 用实时名册，且**不碰**会话快照。"""
        recorded = patch_transport(
            monkeypatch,
            roster=roster_payload(
                [role_bytes(701, ROLE_MAIN_KURTIS, 50, star=5)]
            ),
        )
        session = make_session(
            roles=({"roleSid": 999, "roleID": ROLE_MAIN_ZHANGCHEN, "roleLV": 1, "roleStar": 1},)
        )
        result = create_task("roles", session).run()

        assert result.ok is True
        assert result.data["source"] == "live"
        assert result.data["fallback_reason"] == ""
        assert [r["role_sid"] for r in result.data["roles"]] == [701], "应以实时名册为准"
        assert result.data["roles"][0]["name"] == "庫爾蒂絲"
        assert result.data["roles"][0]["role_info_id"] == ROLE_INFO_KURTIS
        assert "icon_code" in result.data["roles"][0], "头像 code 必须随角色一起透出"
        assert sent_packet_ids(recorded) == [PACKET_GET_ALL_ROLE], "只读任务只发这一个包"

    def test_roles_are_sorted_by_level_desc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """等级降序 —— 与挖矿"从高到低挑角色"的口径一致。"""
        patch_transport(
            monkeypatch,
            roster=roster_payload(
                [
                    role_bytes(701, ROLE_MAIN_KURTIS, 10),
                    role_bytes(702, ROLE_MAIN_ZHANGCHEN, 60),
                    role_bytes(703, ROLE_MAIN_KURTIS, 35),
                ]
            ),
        )
        result = create_task("roles", make_session()).run()
        assert [r["level"] for r in result.data["roles"]] == [60, 35, 10]

    def test_missing_config_table_still_returns_roles(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """读不到配置表 → 名字留空，但**角色列表照常返回**（不因展示问题停功能）。"""
        monkeypatch.setattr(
            tasks.roles,
            "load_role_id_map",
            lambda *a, **k: (_ for _ in ()).throw(OSError("表不在")),
        )
        patch_transport(
            monkeypatch, roster=roster_payload([role_bytes(701, ROLE_MAIN_KURTIS, 50)])
        )
        result = create_task("roles", make_session()).run()

        assert result.ok is True
        assert result.data["role_count"] == 1
        assert result.data["roles"][0]["name"] == "", "没配置表就别编名字"
        assert result.data["roles"][0]["role_sid"] == 701, "ID 数据不依赖配置表"
        assert result.data["roles"][0]["icon_code"] == "", "没换算表就别给假头像路径"

    def test_icon_code_is_a_real_filename(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """``icon_code`` 非空时**必须在静态目录里存在**同名 PNG。

        （前端直接拼 ``assets/avatars/{code}.png`` 不做校验 —— 这里给错路径
        就是一片破图。判据与 ``services/avatar_service.resolve_role_icon_code``
        的承诺一致：要么空、要么有文件。）

        用**真实素材表**跑（不 patch ``load_role_id_map``），挑一个已知有图的
        角色 ``ROLE_MAIN_KURTIS``。
        """
        from pathlib import Path

        avatar_dir = (
            Path(__file__).resolve().parents[1] / "web" / "assets" / "avatars"
        )
        if not avatar_dir.is_dir():
            pytest.skip("缺少 web/assets/avatars 静态头像")

        role_map = create_role_id_map_from_real_tables()
        if role_map is None:
            pytest.skip("缺少 RoleMainData / RoleInfoData 素材")
        monkeypatch.setattr(tasks.roles, "load_role_id_map", lambda *a, **k: role_map)

        patch_transport(
            monkeypatch, roster=roster_payload([role_bytes(701, ROLE_MAIN_KURTIS, 50)])
        )
        result = create_task("roles", make_session()).run()
        code = result.data["roles"][0]["icon_code"]
        assert code, "庫爾蒂絲应有头像（缺图说明算错了）"
        assert (avatar_dir / f"{code}.png").is_file(), f"返回了不存在的 {code}.png"


# ---------------------------------------------------------------------------
# 快照兜底
# ---------------------------------------------------------------------------
class TestSnapshotFallback:
    @pytest.fixture(autouse=True)
    def _role_map(self, monkeypatch: pytest.MonkeyPatch) -> None:
        patch_role_id_map(monkeypatch)

    def test_empty_live_falls_back_to_snapshot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """服务端回了空名册（有子包但无 roleList）→ 用登录快照，并说明原因。"""
        patch_transport(monkeypatch, roster=EMPTY_ROSTER_WITH_SUBPACKET)
        session = make_session(
            roles=({"roleSid": 801, "roleID": ROLE_MAIN_ZHANGCHEN, "roleLV": 42, "roleStar": 4},)
        )
        result = create_task("roles", session).run()

        assert result.ok is True
        assert result.data["source"] == "snapshot"
        assert "roleList 为空" in result.data["fallback_reason"]
        assert [r["role_sid"] for r in result.data["roles"]] == [801]
        assert result.data["roles"][0]["name"] == "將臣"
        assert "登录快照" in result.message, "结果行必须写明用的是哪一份数据"

    def test_failed_live_falls_back_to_snapshot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """5011 报错（例如包被拒）→ 同样退回快照，并**留痕**。"""
        from client.exceptions import ApiError

        patch_transport(monkeypatch, error=ApiError(-2, "被拒"))
        session = make_session(
            roles=({"roleSid": 802, "roleID": ROLE_MAIN_KURTIS, "roleLV": 30, "roleStar": 3},)
        )
        result = create_task("roles", session).run()

        assert result.ok is True
        assert result.data["source"] == "snapshot"
        assert "实时名册请求失败" in result.data["fallback_reason"]

    def test_snapshot_absent_and_live_empty_reports_clearly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """两条路都空 → ok=False，并给出**可执行的下一步**。"""
        patch_transport(monkeypatch, roster=EMPTY_ROSTER_WITH_SUBPACKET)
        result = create_task("roles", make_session()).run()

        assert result.ok is False
        assert result.data["role_count"] == 0
        assert "python main.py run login" in result.message, "要告诉用户怎么补救"

    def test_zero_role_account_hits_the_same_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """★ 真机上"账号没有角色"的形态：**0 字节子包**。

        ``decode_sub_packet`` 会把它抛成 ``EnvelopeError``（"不含业务子包"）——
        生产代码必须把它当"实时名册拿不到"来兜底，而不是让整个任务失败。
        """
        patch_transport(monkeypatch, roster=b"")
        session = make_session(
            roles=({"roleSid": 803, "roleID": ROLE_MAIN_KURTIS, "roleLV": 1, "roleStar": 1},)
        )
        result = create_task("roles", session).run()

        assert result.ok is True, "0 字节子包不该让任务失败"
        assert result.data["source"] == "snapshot"

    def test_session_without_game_state_is_guarded_before_sending(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """会话里没有 game_state（未登录）→ 被 ``requires_auth`` 挡在发包之前。

        断言的是"**一个包都没发**" + "错误信息给了补救办法"，
        而不是某个具体措辞 —— 这条守卫在 ``tasks/base.py`` 里，措辞可能演进。
        """
        recorded = patch_transport(monkeypatch, roster=EMPTY_ROSTER_WITH_SUBPACKET)
        session = GameSession("https://example.invalid", timeout=1.0)
        session.token_store.update(uid="u1", token="tok")
        result = create_task("roles", session).run()

        assert result.ok is False
        assert recorded == [], "没有会话状态就不该发包"
        assert "python main.py run login" in result.message


# ---------------------------------------------------------------------------
# 演练模式
# ---------------------------------------------------------------------------
class TestDryRun:
    def test_dry_run_sends_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded = patch_transport(monkeypatch, roster=roster_payload([]))
        result = create_task("roles", make_session(), dry_run=True).run()

        assert recorded == [], "演练模式不许发任何请求"
        assert result.ok is True
        assert result.data["sent_packets"] == ()
