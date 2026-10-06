"""服务端异常包与错误分类（``models/game_error.py`` + ``client/game_client.py``）的单测。

运行方式：

    python -m pytest tests/test_game_error.py -q

【为什么这个模块值得单独一份测试】
实测服务端拒绝请求时只回 ``ServerExceptionRes{errorCode=-3, CustomErrorMsg="ABNORMAL_PACKET"}``
—— **信息量为零**。它唯一的价值是告诉我们"别再重试了，回去改字节"。
所以这份测试守的是三件事：

1. 这个包能被**认出来**（否则它就退化成一条 `errorCode != 0` 的普通业务错误）；
2. 认出来之后抛的异常**说得清下一步该做什么**（包号、字节数、排查顺序）；
3. 正常业务响应**不会**被误判（误判会让好端端的流程中断）。
"""

from __future__ import annotations

import pytest

from client.exceptions import AbnormalPacketError, ProtocolError
from client.game_client import GameClient
from models.envelope import GameEnvelope
from models.game_error import (
    ABNORMAL_PACKET_CODE,
    GAME_ERROR_PACKET_ID,
    GameErrorClass,
    ServerExceptionRes,
    classify_error_code,
    is_abnormal_packet,
)
from models.handshake import VersionControlServerPacket
from models.protobuf_wire import encode_int, encode_message, encode_string
from tests.test_game_client import FakeResponse, handshake_response_bytes

#: 实测的异常描述（服务端原样回这个短语）
ABNORMAL_MSG = "ABNORMAL_PACKET"


def server_exception_payload(
    *, code: int = ABNORMAL_PACKET_CODE, msg: str = ABNORMAL_MSG
) -> bytes:
    """拼 ``ServerExceptionRes`` 的子包载荷（字段 1 = errorCode、2 = CustomErrorMsg）。"""
    return encode_int(1, code) + encode_string(2, msg)


def envelope_bytes(packet_id: int, payload: bytes) -> bytes:
    """拼一段响应字节：字段 1 = 消息号，业务子包槽位编号 = 消息号本身。"""
    return encode_int(1, packet_id) + encode_message(packet_id, payload)


def exception_response_bytes(**kwargs: object) -> bytes:
    """完整的 99004 响应字节。"""
    payload = server_exception_payload(**kwargs)  # type: ignore[arg-type]
    return envelope_bytes(GAME_ERROR_PACKET_ID, payload)


def handshake_packet() -> VersionControlServerPacket:
    """随便一个**合法**请求子包（消息号 99015），用来触发一次发送。"""
    return VersionControlServerPacket(
        channelPlatformType=2,
        clientVersion="2.2.0-el-h-win",
        languageCode="zhcn",
    )


def client_with_response(monkeypatch: pytest.MonkeyPatch, content: bytes) -> GameClient:
    """构造一个"应答固定为给定字节"的客户端（不发任何网络请求）。"""
    client = GameClient()
    monkeypatch.setattr(client, "post_raw", lambda body: FakeResponse(content))
    return client


class TestServerExceptionRes:
    """模型本身：字段编号与实测一致。"""

    def test_decodes_error_code_and_message(self) -> None:
        error = ServerExceptionRes.decode(server_exception_payload())
        assert error.errorCode == ABNORMAL_PACKET_CODE
        assert error.CustomErrorMsg == ABNORMAL_MSG

    def test_describe_reads_like_a_log_line(self) -> None:
        text = ServerExceptionRes.decode(server_exception_payload()).describe()
        assert f"code={ABNORMAL_PACKET_CODE}" in text
        assert ABNORMAL_MSG in text

    def test_packet_id_matches_dossier(self) -> None:
        """消息号是 99004（``tools/proto_dossier.py ServerExceptionRes`` 实测）。"""
        assert GAME_ERROR_PACKET_ID == 99004


class TestClassification:
    """错误码分类：决定"能否重试"。"""

    def test_minus_three_is_abnormal_packet(self) -> None:
        assert classify_error_code(-3) is GameErrorClass.ABNORMAL_PACKET
        assert is_abnormal_packet(-3) is True

    @pytest.mark.parametrize("code", [0, 1, -1, 401, 99999])
    def test_other_codes_are_business(self, code: int) -> None:
        """其余码一律归"业务"（不重试）。

        保守是刻意的：目前**只有 -3 是实测确认的**。把某个未知码猜成"令牌失效"，
        会导致为一处字段问题去重新登录，反而增加风控暴露面（见 Q-010）。
        """
        assert classify_error_code(code) is GameErrorClass.BUSINESS
        assert is_abnormal_packet(code) is False


class TestDetectionInClient:
    """``GameClient`` 在唯一出口处识别异常包。"""

    def test_abnormal_packet_raises_with_request_packet_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = client_with_response(monkeypatch, exception_response_bytes())
        with pytest.raises(AbnormalPacketError) as excinfo:
            client.send(handshake_packet())

        error = excinfo.value
        # ⚠️ 这里记的是"发出去的包号"（99015），不是响应包号 99004 ——
        # 排查时需要的正是"我发的哪个包被拒了"。
        assert error.packet_id == 99015
        assert error.code == ABNORMAL_PACKET_CODE
        assert error.detail == ABNORMAL_MSG
        assert isinstance(error, ProtocolError)

    def test_message_tells_you_what_to_do_next(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """报错必须自带排查步骤 —— 因为这个错误码本身零信息量。"""
        client = client_with_response(monkeypatch, exception_response_bytes())
        with pytest.raises(AbnormalPacketError) as excinfo:
            client.send(handshake_packet())

        message = str(excinfo.value)
        assert "99004" in message
        assert "重试" in message
        assert "--dry-run" in message

    def test_unparsable_payload_still_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """异常包内容读不出来时也要抛 —— 不能因为"解析失败"就当成正常响应放行。"""
        broken = envelope_bytes(GAME_ERROR_PACKET_ID, b"\xff\xff\xff\xff")
        client = client_with_response(monkeypatch, broken)
        with pytest.raises(AbnormalPacketError) as excinfo:
            client.send(handshake_packet())
        assert excinfo.value.code == 0

    def test_extra_packet_is_detected_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """响应可含多个包（实测握手响应就有 3 个）：异常包藏在附加包里也要认出来。"""
        content = envelope_bytes(13002, encode_int(1, 0)) + encode_message(
            GAME_ERROR_PACKET_ID, server_exception_payload()
        )
        client = client_with_response(monkeypatch, content)
        with pytest.raises(AbnormalPacketError):
            client.send(handshake_packet())

    def test_normal_response_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """正常业务响应不能被误判（误判会让好端端的流程中断）。"""
        client = client_with_response(monkeypatch, handshake_response_bytes())
        view = client.send(handshake_packet())
        assert view.packet_id == 99016


class TestRequestPacketIdExtraction:
    """``GameClient._packet_id_of``：从请求体里读信封字段 1。"""

    def test_reads_packet_id_from_request(self) -> None:
        body = GameEnvelope().encode_with(handshake_packet())
        assert GameClient._packet_id_of(body) == 99015

    def test_garbage_returns_none(self) -> None:
        assert GameClient._packet_id_of(b"\xff\xff\xff\xff") is None
