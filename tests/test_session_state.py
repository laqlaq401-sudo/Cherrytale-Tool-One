"""游戏会话状态（``models/session_state.py``）的单元测试 —— **全离线**。

运行方式：

    python -m pytest tests/test_session_state.py -q

【这份测试要钉住的三件事】

1. **令牌不被打印**：``describe()`` 只留前 4 个字符。凭据一旦进了日志/截图，
   比"临时找不到令牌"麻烦得多，所以这条要有测试守着（否则很容易被
   "为了排查方便"改成打印全文）。
2. **注入的位置正确**：``apply_to`` 写的是信封字段 ``token`` 与 ``player_id``。
   写错位置不会报错，只会让服务端回一句含糊的"包异常"。
3. **序列化宽容但不失格**：会话文件是缓存（容忍缺字段/多余字段），
   但**没有令牌就是无效**（``is_valid()`` 为 False），绝不"凑合着用"。
"""

from __future__ import annotations

from typing import Any

from models.envelope import GameEnvelope
from models.session_state import SESSION_JSON_VERSION, GameSessionState

#: 假的令牌（长度 34，与实测一致）—— 真实令牌绝不进仓库
TOKEN = "FAKETOKEN00000000000000000000000000"


def make_state(**overrides: Any) -> GameSessionState:
    """造一个字段齐全的会话状态（用例只改自己关心的字段）。"""
    base: dict[str, Any] = {
        "token": TOKEN,
        "player_id": 10000001,
        "account_id": 10000004,
        "server_id": 167,
        "server_name": "宮本武藏",
        "platform_user_id": "ER00000000-0000-0000-0000-000000000000",
        "obtained_at": 1789908064.0,
    }
    base.update(overrides)
    return GameSessionState(**base)


class TestValidity:
    """``has_token`` / ``is_expired`` / ``is_valid`` 的边界。"""

    def test_empty_state_is_invalid(self) -> None:
        assert GameSessionState().is_valid() is False

    def test_state_with_token_is_valid(self) -> None:
        assert make_state().is_valid() is True

    def test_missing_expiry_never_expires(self) -> None:
        """服务端没下发有效期（``PlayerClass`` 里确实没有该字段）→ 不主动判过期。

        自造一个"过期时间"会带来两种坏结果：编短了白白重登（登录有风控风险），
        编长了等于没判。
        """
        assert make_state(expires_at=None).is_expired(now=10**12) is False

    def test_past_expiry_is_expired(self) -> None:
        state = make_state(expires_at=1000.0)
        assert state.is_expired(now=1001.0) is True
        assert state.is_valid(now=1001.0) is False

    def test_future_expiry_is_valid(self) -> None:
        assert make_state(expires_at=2000.0).is_valid(now=1001.0) is True


class TestEnvelopeRoundTrip:
    """与 ``GameEnvelope`` 之间的注入 / 反向提取。"""

    def test_apply_to_writes_token_and_player_id(self) -> None:
        envelope = GameEnvelope()
        make_state().apply_to(envelope)
        assert envelope.token == TOKEN
        assert envelope.player_id == 10000001

    def test_apply_to_does_not_clear_player_id_when_unknown(self) -> None:
        """``player_id`` 未知（0）时**不要覆盖**信封里已有的值。

        覆盖会发生在"编排流程里先登录、后又套了一层状态"这类场景，
        结果是把已经带好的字段 8 擦掉 —— 而服务端只会含糊地拒绝。
        """
        envelope = GameEnvelope(player_id=999)
        make_state(player_id=0).apply_to(envelope)
        assert envelope.player_id == 999

    def test_from_envelope_keeps_context_fields(self) -> None:
        envelope = GameEnvelope(token=TOKEN, player_id=10000001)
        state = GameSessionState.from_envelope(
            envelope,
            account_id=10000004,
            server_id=167,
            server_name="宮本武藏",
            platform_user_id="ER-1",
            obtained_at=1234.0,
        )
        assert (state.token, state.player_id) == (TOKEN, 10000001)
        assert (state.account_id, state.server_id, state.server_name) == (10000004, 167, "宮本武藏")
        assert state.platform_user_id == "ER-1"
        assert state.obtained_at == 1234.0

    def test_from_envelope_defaults_obtained_at_to_now(self) -> None:
        state = GameSessionState.from_envelope(GameEnvelope(token=TOKEN))
        assert state.obtained_at > 0

    def test_from_envelope_passes_avatar_data(self) -> None:
        """头像 base64 从登录流程带进会话状态（★ 2026-10-05）。"""
        state = GameSessionState.from_envelope(
            GameEnvelope(token=TOKEN), avatar_data="data:image/png;base64,AAA"
        )
        assert state.avatar_data == "data:image/png;base64,AAA"
        assert GameSessionState.from_envelope(GameEnvelope(token=TOKEN)).avatar_data is None


class TestSerialization:
    """``to_dict`` / ``from_dict``：落盘与读回。"""

    def test_round_trip_preserves_all_fields(self) -> None:
        original = make_state()
        restored = GameSessionState.from_dict(original.to_dict())
        assert restored == original

    def test_round_trip_preserves_avatar_data(self) -> None:
        """头像 base64 落盘读回不丢（★ 2026-10-05）。"""
        original = make_state(avatar_data="data:image/png;base64,QUJD")
        restored = GameSessionState.from_dict(original.to_dict())
        assert restored.avatar_data == "data:image/png;base64,QUJD"

    def test_old_session_file_without_avatar_reads_as_none(self) -> None:
        """版本 3 的旧会话文件没有 avatar_data → 宽容解析成 None，不报错。"""
        data = make_state().to_dict()
        data.pop("avatar_data")
        data["version"] = 3
        state = GameSessionState.from_dict(data)
        assert state.avatar_data is None
        assert state.is_valid() is True

    def test_version_is_recorded(self) -> None:
        assert make_state().to_dict()["version"] == SESSION_JSON_VERSION

    def test_unknown_keys_are_ignored(self) -> None:
        """将来加了字段、又被旧版本读到，不该直接崩。"""
        data = make_state().to_dict()
        data["future_field"] = "whatever"
        assert GameSessionState.from_dict(data).is_valid() is True

    def test_bad_types_fall_back_to_defaults(self) -> None:
        """手写/损坏的 JSON 值不该抛异常，也不该把可用会话弄失效。"""
        state = GameSessionState.from_dict(
            {"token": TOKEN, "player_id": "abc", "expires_at": "?"}
        )
        assert state.player_id == 0
        # 读坏的过期时间按"服务端没下发"处理（None），而不是当成"已过期"：
        # 后者会让一个本来能用的会话被无谓丢弃，而真正的失效信号来自服务端响应。
        assert state.expires_at is None
        assert state.is_valid() is True

    def test_only_token_is_enough(self) -> None:
        """环境变量那条路只能给出令牌，所以这种字典必须能用。"""
        state = GameSessionState.from_dict({"token": TOKEN})
        assert state.is_valid() is True

    def test_missing_token_is_invalid(self) -> None:
        assert GameSessionState.from_dict({}).is_valid() is False


class TestDescribe:
    """``describe()``：能读、但不泄露。"""

    def test_token_is_masked(self) -> None:
        text = make_state().describe()
        assert TOKEN not in text
        assert "FAKE…" in text

    def test_shows_player_and_server(self) -> None:
        text = make_state().describe()
        assert "playerId=10000001" in text
        assert "宮本武藏(#167)" in text

    def test_empty_token_reads_as_empty(self) -> None:
        assert "token=<空>" in GameSessionState().describe()

    def test_repr_also_masks(self) -> None:
        """``repr`` 常被 f-string 顺手带进日志，所以同样要打码。"""
        assert TOKEN not in repr(make_state())
