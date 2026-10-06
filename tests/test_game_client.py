"""游戏网关客户端（``client/game_client.py``）的单元测试。

运行方式：

    python -m tests.test_game_client
    python -m pytest tests/test_game_client.py -q

【本文件**完全不联网**】
需要网络的地方一律用 :class:`FakeResponse` 替代。理由不只是"快"：
真实发包会对账号产生副作用（握手虽小，也是往服务端发数据），
而单元测试可能被反复运行 —— 不能让"跑测试"变成"操作账号"。

【测什么】
1. 组装出来的**字节与抓包一致**（长度 88、开头 `08 c7 85 06`、头部逐项）——
   这是"协议实现正确"的唯一硬证据；
2. 响应解析与会话状态回填；
3. 两条安全默认：**不带鉴权头**、**POST 不自动重试**。
"""

from __future__ import annotations

from typing import Any

import pytest

import config
from client.game_client import UNITY_HEADERS, GameClient, OutgoingRequest
from models.handshake import (
    CheckFunctionStateRes,
    VersionControlServerPacket,
    VersionControlServerRes,
)
from protocol_samples.gateway_samples import SAMPLES

#: 抓包实测的请求开头（前 9 字节：packetID + settingMd5 + timeStampToken 的标签）
CAPTURE_REQUEST_PREFIX: str = "08c785061a022d3150"

#: 抓包实测的请求体长度
CAPTURE_REQUEST_LENGTH: int = 88


def handshake_response_bytes() -> bytes:
    """从夹具里取 99016 那条真实响应字节。"""
    for sample in SAMPLES:
        if sample["response_packet_id"] == 99016:
            return bytes.fromhex(sample["response_hex"])
    raise AssertionError("抓包夹具里没有 99016（握手响应）")


def make_handshake_packet() -> VersionControlServerPacket:
    """按实测取值构造握手包（渠道 2 / 版本 2.2.0-el-h-win / 语言 zhcn）。"""
    return VersionControlServerPacket(
        channelPlatformType=2,
        clientVersion="2.2.0-el-h-win",
        languageCode="zhcn",
    )


class FakeResponse:
    """``requests.Response`` 的最小替身（只实现本模块用到的属性）。"""

    def __init__(self, content: bytes, status_code: int = 200) -> None:
        self.content = content
        self.status_code = status_code
        self.headers = {"Content-Type": config.GAME_CONTENT_TYPE}


class TestBuild:
    """``build()`` 只组装字节，绝不落网。"""

    def test_url_is_gateway_root(self) -> None:
        """实测目标就是根路径 ``/``（不是常见的 ``/api/xxx``）。"""
        request = GameClient().build(make_handshake_packet())
        assert request.url == "https://game-ct-labs.ecchi.xxx:1893/"

    def test_headers_match_capture(self) -> None:
        request = GameClient().build(make_handshake_packet())
        assert request.headers["Content-Type"] == "application/octet-stream"
        assert request.headers["Accept"] == "*/*"
        assert request.headers["Accept-Encoding"] == "deflate, gzip"
        assert request.headers["User-Agent"].startswith("UnityPlayer/")
        assert request.headers["X-Unity-Version"] == config.UNITY_VERSION

    def test_no_auth_header(self) -> None:
        """**首包不带任何鉴权头**（实测如此）—— 这是它能安全试发的根本原因。"""
        request = GameClient().build(make_handshake_packet())
        suspicious = [
            name
            for name in request.headers
            if "auth" in name.lower() or "token" in name.lower() or "cookie" in name.lower()
        ]
        assert suspicious == []

    def test_length_matches_capture(self) -> None:
        """默认信封 + 握手包 = **88 字节**，与抓包完全一致。"""
        request = GameClient().build(make_handshake_packet())
        assert len(request.body) == CAPTURE_REQUEST_LENGTH

    def test_prefix_matches_capture(self) -> None:
        """开头字节逐位一致：packetID=99015 → settingMd5=-1 → timeStampToken=-1。"""
        request = GameClient().build(make_handshake_packet())
        assert request.preview_bytes(9) == CAPTURE_REQUEST_PREFIX

    def test_sub_packet_slot_uses_message_number(self) -> None:
        """子包槽位标签 ``BA AC 30``（= 字段 99015）。"""
        request = GameClient().build(make_handshake_packet())
        assert bytes.fromhex("baac30") in request.body

    def test_hex_lines_cover_body(self) -> None:
        request = GameClient().build(make_handshake_packet())
        assert "".join(request.hex_lines()) == request.hex()

    def test_describe_is_human_readable(self) -> None:
        text = GameClient().build(make_handshake_packet()).describe()
        assert "POST https://game-ct-labs.ecchi.xxx:1893/" in text
        assert "Content-Type" in text
        assert "88 字节" in text


class TestSend:
    """``send()``：用假响应替代网络。"""

    @staticmethod
    def client_with_response(
        monkeypatch: pytest.MonkeyPatch, content: bytes
    ) -> GameClient:
        client = GameClient()
        monkeypatch.setattr(client, "post_raw", lambda body: FakeResponse(content))
        return client

    def test_returns_envelope_view(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = self.client_with_response(monkeypatch, handshake_response_bytes())
        view = client.send(make_handshake_packet())
        assert view.packet_id == 99016
        assert view.class_name == "VersionControlServerRes"

    def test_wrong_model_yields_misleading_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """用错模型时 protobuf 不但不报错，还可能解出**看似合理**的值。

        实测：把 ``VersionControlServerRes`` 的内容按 ``CheckFunctionStateRes`` 解析，
        得到 ``functionStateList = [0]`` —— 一个完全说得通、却毫无意义的结果
        （那个 0 其实来自对方报文的字段 1 ``errorCode = 0``）。

        这正是本项目坚持「新报文第一次使用前必须与抓包字节逐字段对照」的原因：
        **类型系统挡不住这类错误，只有真实数据能。**
        """
        client = self.client_with_response(monkeypatch, handshake_response_bytes())
        view = client.send(make_handshake_packet(), expect=CheckFunctionStateRes)
        parsed = view.decode_sub_packet(CheckFunctionStateRes)
        assert parsed.functionStateList == [0]

    def test_correct_model_yields_real_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """正向验证：用对模型才能解出真实值 —— 这才是"实现正确"的证据。"""
        client = self.client_with_response(monkeypatch, handshake_response_bytes())
        view = client.send(make_handshake_packet(), expect=VersionControlServerRes)
        packet = view.decode_sub_packet(VersionControlServerRes)
        assert packet.serverIP == "game-ct-labs.ecchi.xxx"
        assert packet.serverPort == 1888

    def test_handshake_returns_server_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = self.client_with_response(monkeypatch, handshake_response_bytes())
        server_config = client.handshake()
        assert server_config.serverIP == "game-ct-labs.ecchi.xxx"
        assert server_config.serverPort == 1888
        assert server_config.isUseSSL is True

    def test_handshake_keeps_default_fingerprint_when_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """响应里没有 ``settingMd5`` 字段时，**本地占位值不能被清空**。

        握手响应确实不带这个字段（它在 ``SpecialActivityRefreshClass`` 里），
        所以这是真实场景，不是构造出来的边界情况。
        """
        client = self.client_with_response(monkeypatch, handshake_response_bytes())
        client.handshake()
        assert client.envelope.setting_md5 == "-1"

    def test_send_raw_accepts_handcrafted_bytes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``send_raw`` 允许直接发手工构造的字节（排查时用）。"""
        client = self.client_with_response(monkeypatch, handshake_response_bytes())
        view = client.send_raw(bytes.fromhex(CAPTURE_REQUEST_PREFIX + "00"))
        assert view.packet_id == 99016


class TestSafetyDefaults:
    """两条安全默认 —— 它们防的都是"会造成实际损失"的错误。"""

    def test_post_is_not_retried(self) -> None:
        """POST 绝不能自动重试：领奖类接口不幂等，重发可能重复领取。

        这里断言的是 ``GameSession`` 的默认策略，因为 ``GameClient`` 刻意
        **没有**覆盖它 —— 一旦有人想"顺手改成重试 POST"，这条会立刻变红。
        """
        from client.session import DEFAULT_RETRY_METHODS

        assert DEFAULT_RETRY_METHODS == frozenset({"GET"})
        assert "POST" not in DEFAULT_RETRY_METHODS

    def test_token_field_is_not_configured(self) -> None:
        """令牌走 ``RootPacket`` 字段，**不在** HTTP 头。

        配了 ``token_field`` 会让每个请求多出一个鉴权头，
        而服务端的反应是含糊的失败 —— 这类问题最难往回追。
        """
        assert GameClient().session.token_field is None

    def test_unity_headers_are_complete(self) -> None:
        assert set(UNITY_HEADERS) == {
            "User-Agent",
            "Accept",
            "Accept-Encoding",
            "Content-Type",
            "X-Unity-Version",
        }


class TestOutgoingRequest:
    def test_preview_bytes(self) -> None:
        request = OutgoingRequest(
            method="POST", url="u", headers={}, body=bytes.fromhex("0102030405")
        )
        assert request.preview_bytes(3) == "010203"


if __name__ == "__main__":  # 支持 `python -m tests.test_game_client`
    raise SystemExit(pytest.main([__file__, "-v", "--no-header"]))
