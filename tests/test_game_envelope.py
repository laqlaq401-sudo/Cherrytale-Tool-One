"""信封层（``models/envelope.py``）的单元测试 —— **全部基于真实抓包字节**。

运行方式：

    python -m tests.test_game_envelope
    python -m pytest tests/test_game_envelope.py -q

【为什么这组测试最关键】
别的测试只能证明"代码自洽"，这组证明的是"**我们真的读懂了线上字节**"。
样本来自 ``captures/`` 里的 Fiddler 抓包（由 ``tools/extract_har.py`` 转成 hex 夹具），
所以这里断言的每一个数字，都能回抓包里逐字节核对。

反过来也成立：**如果哪天游戏改了协议，这组测试会最先红**——
而它给出的失败信息（期望 vs 实际）就是你重新逆向的起点。
"""

from __future__ import annotations

from typing import Any

import pytest

from models.envelope import (
    EnvelopeError,
    GameEnvelope,
    parse_envelope,
)
from models.handshake import (
    CheckFunctionStateRes,
    SpecialActivityRefreshClass,
    VersionControlServerPacket,
    VersionControlServerRes,
)
from protocol_samples.gateway_samples import SAMPLES, VERSION_CONTROL_REQUEST_HEX


def sample_with(packet_id: int) -> dict[str, Any]:
    """按响应消息号取抓包样本。

    刻意**不用下标**（如 ``SAMPLES[0]``）：样本顺序会随抓包文件数量变化，
    按下标写测试会在某次重新导出后悄悄断错对象。
    """
    for sample in SAMPLES:
        if sample["response_packet_id"] == packet_id:
            return sample
    raise AssertionError(f"抓包夹具里没有消息号 {packet_id} 的样本")


def response_view(packet_id: int):
    """取某个样本的响应并解析成信封视图。"""
    sample = sample_with(packet_id)
    return parse_envelope(bytes.fromhex(sample["response_hex"]))


class TestRequestSide:
    """请求侧：用 HexView 提供的 88 字节首包，验证「解析」是否正确。"""

    def test_is_eighty_eight_bytes(self) -> None:
        assert len(bytes.fromhex(VERSION_CONTROL_REQUEST_HEX)) == 88

    def test_envelope_identifies_packet(self) -> None:
        view = parse_envelope(bytes.fromhex(VERSION_CONTROL_REQUEST_HEX))
        assert view.packet_id == 99015
        assert view.class_name == "VersionControlServerPacket"

    def test_sub_packet_is_24_bytes(self) -> None:
        view = parse_envelope(bytes.fromhex(VERSION_CONTROL_REQUEST_HEX))
        assert len(view.sub_packet_bytes) == 24

    def test_sub_packet_decodes_to_real_values(self) -> None:
        """三个字段的值必须与抓包一致 —— 这是"编号 = 声明顺序"的直接证据。

        线上字节：``08 02 | 12 0E "2.2.0-el-h-win" | 1A 04 "zhcn"``
        """
        view = parse_envelope(bytes.fromhex(VERSION_CONTROL_REQUEST_HEX))
        packet = view.decode_sub_packet(VersionControlServerPacket)
        assert isinstance(packet, VersionControlServerPacket)
        assert packet.channelPlatformType == 2
        assert packet.clientVersion == "2.2.0-el-h-win"
        assert packet.languageCode == "zhcn"

    def test_meta_placeholders_match_capture(self) -> None:
        """首包里的两个占位值：``settingMd5 = "-1"``、``timeStampToken = -1``。"""
        view = parse_envelope(bytes.fromhex(VERSION_CONTROL_REQUEST_HEX))
        assert view.text("settingMd5") == "-1"
        assert view.integer("timeStampToken") == -1
        assert view.text("vCode") == "BA32E78A3C5264338104BDCA67DA6993"



def build_handshake_body(**envelope_kwargs: Any) -> bytes:
    """按抓包结构组装一个握手请求体（供多个用例复用）。

    默认值与抓包保持一致：``settingMd5="-1"``、``timeStampToken=-1``，
    并把 ``vCode`` / 时间戳固定下来，让断言可以逐字节比对。
    """
    envelope_kwargs.setdefault("setting_md5", "-1")
    envelope_kwargs.setdefault("time_stamp_token", -1)
    envelope_kwargs.setdefault("v_code", "00" * 16)
    envelope_kwargs.setdefault("time_stamp_client", 1)
    envelope = GameEnvelope(**envelope_kwargs)
    return envelope.encode_with(
        VersionControlServerPacket(
            channelPlatformType=2,
            clientVersion="2.2.0-el-h-win",
            languageCode="zhcn",
        )
    )


class TestResponseSide:
    """响应侧：650 字节握手响应（**一个响应里含 3 个包**）。"""

    def test_envelope_identifies_packet(self) -> None:
        view = response_view(99016)
        assert view.packet_id == 99016
        assert view.class_name == "VersionControlServerRes"

    def test_contains_three_packets(self) -> None:
        """一个响应里可以有多个包 —— 这正是实测到的握手响应形态。

        漏掉额外包会造成很误导的现象：明明服务端下发了配置，我们却"看不到"，
        于是跑去别的地方找原因。
        """
        view = response_view(99016)
        names = [name for _number, name, _payload in view.extra_packets]
        assert "SpecialActivityRefreshClass" in names
        assert "CheckFunctionStateRes" in names

    def test_server_address_is_confirmed_by_server(self) -> None:
        """服务端在载荷里确认了服务器地址。

        .. important::
           实测 ``serverPort = 1888``，**而我们发请求用的是 1893** ——
           两者不是同一个端口。合理推测：1893 是 HTTP 网关（我们一直在用的），
           1888 是服务端下发给客户端的"游戏业务端口"（供长连接/重连使用）。
           在没有抓到这个端口的实际流量之前，**不要**擅自把请求改到 1888。
        """
        packet = response_view(99016).decode_sub_packet(VersionControlServerRes)
        assert isinstance(packet, VersionControlServerRes)
        assert packet.serverIP == "game-ct-labs.ecchi.xxx"
        assert packet.serverPort == 1888
        assert packet.chatPort == 1900
        assert packet.isUseSSL is True

    def test_dns_and_cdn_lists(self) -> None:
        """DNS / CDN 列表也由服务端下发。

        ``dnsList`` 里的 ``8.8.8.8`` 与我们在元数据字符串里挖到的那个完全一致 ——
        两条独立线索互相印证。
        """
        packet = response_view(99016).decode_sub_packet(VersionControlServerRes)
        assert "8.8.8.8" in packet.dnsList
        assert any("patch-ct-labs" in entry for entry in packet.cdnList)

    def test_announcement_field_is_populated(self) -> None:
        """``customMsg`` 实测有内容（"系统维护中"）。

        刻意**不断言具体文字**：它是服务端随时可改的公告文案，
        写死会在某次运营改文案后让测试红灯，而那种红灯毫无意义。
        """
        packet = response_view(99016).decode_sub_packet(VersionControlServerRes)
        assert packet.has_announcement() is True

    def test_json_settings_are_plain_text(self) -> None:
        """``jsonInfo`` 是**明文 JSON**（抓包里肉眼可见）—— 应用层没有加密。"""
        packet = response_view(99016).decode_sub_packet(VersionControlServerRes)
        settings = packet.json_settings()
        assert settings, "jsonInfo 应该能解析出配置项"
        assert "UseHttpFactory" in settings

    def test_config_fingerprints(self) -> None:
        """那两段 32 字符 MD5 = ``SpecialActivityRefreshClass`` 的两个字段。"""
        payload = response_view(99016).packet_bytes("SpecialActivityRefreshClass")
        assert payload is not None, "握手响应里应当带配置指纹"
        config = SpecialActivityRefreshClass.decode(payload)
        assert config.activeMission == "2DB9B982CA690276061B90E63F3C3D38"
        assert config.puzzleRoleGroup == "4E73561E79779CF2912DC0BF728B31F9"

    def test_function_state_is_kept(self) -> None:
        """功能开关列表（99010）也要能取出来，不能因为"用不到"就丢掉。"""
        payload = response_view(99016).packet_bytes("CheckFunctionStateRes")
        assert payload is not None
        states = CheckFunctionStateRes.decode(payload)
        assert isinstance(states.functionStateList, list)


class TestEncodeMatchesRealBytes:
    """编码侧：自己组出来的字节，必须与抓包**逐字节可比**。"""

    def test_packet_id_prefix(self) -> None:
        """``packetID = 99015`` 编出来是 ``08 C7 85 06`` —— 与抓包完全一致。"""
        assert build_handshake_body().startswith(bytes.fromhex("08c78506"))

    def test_sub_packet_slot_uses_message_number(self) -> None:
        """子包槽位的标签是 ``BA AC 30``（= 字段 99015），**不是**顺序编号。"""
        assert bytes.fromhex("baac30") in build_handshake_body()

    def test_placeholder_fields_match_capture(self) -> None:
        """两个占位字段的字节与抓包一致。

        ``settingMd5 = "-1"`` → ``1A 02 2D 31``；
        ``timeStampToken = -1`` → ``50`` + 九个 ``FF`` + ``01``（负数按 64 位补码占 10 字节）。
        """
        body = build_handshake_body()
        assert bytes.fromhex("1a022d31") in body
        assert bytes.fromhex("50" + "ff" * 9 + "01") in body

    def test_sub_packet_content(self) -> None:
        """子包内容 ``08 02 12 0E "2.2.0-el-h-win" 1A 04 "zhcn"``（24 字节）。"""
        expected = (
            bytes.fromhex("0802")
            + bytes.fromhex("120e")
            + b"2.2.0-el-h-win"
            + bytes.fromhex("1a04")
            + b"zhcn"
        )
        assert len(expected) == 24
        assert expected in build_handshake_body()

    def test_roundtrip(self) -> None:
        """自己编的包，自己也能完整解回来。"""
        packet = VersionControlServerPacket(
            channelPlatformType=2,
            clientVersion="2.2.0-el-h-win",
            languageCode="zhcn",
        )
        view = parse_envelope(build_handshake_body())
        assert view.packet_id == 99015
        assert view.decode_sub_packet(VersionControlServerPacket) == packet


class TestEnvelopeState:
    """信封状态的维护（跨请求保持同一个会话）。"""

    def test_update_from_keeps_existing_when_absent(self) -> None:
        """响应里没有的字段，**不能把本地已有值清空**。

        握手响应里就没有 token；若这里实现成"无条件赋值"，
        登录后拿到的令牌会在下一次握手时被抹掉，而现象极难定位。
        """
        envelope = GameEnvelope(token="keep-me", player_id=42)
        envelope.update_from(response_view(99016))
        assert envelope.token == "keep-me"
        assert envelope.player_id == 42

    def test_v_code_is_generated_when_empty(self) -> None:
        body = GameEnvelope(v_code="").encode_with(
            VersionControlServerPacket(clientVersion="x", languageCode="zhcn")
        )
        generated = parse_envelope(body).text("vCode")
        assert generated is not None
        assert len(generated) == 32

    def test_clone_is_independent(self) -> None:
        original = GameEnvelope(token="a", player_id=1)
        copy = original.clone()
        copy.token = "b"
        assert original.token == "a"


class TestErrors:
    """出错时必须**明确报错**，而不是静默返回空结果。"""

    def test_empty_response(self) -> None:
        with pytest.raises(EnvelopeError, match="为空"):
            parse_envelope(b"")

    def test_not_protobuf(self) -> None:
        with pytest.raises(EnvelopeError):
            parse_envelope(b"HTTP/1.1 200 OK\r\n\r\n<html>")

    def test_missing_class_name(self) -> None:
        from models.game_packet import ProtoMessage

        class NoName(ProtoMessage):
            pass

        with pytest.raises(EnvelopeError, match="proto_class_name"):
            GameEnvelope().encode_with(NoName())

    def test_unknown_class_name(self) -> None:
        from models.game_packet import ProtoMessage

        class Unknown(ProtoMessage):
            proto_class_name = "NotARealPacket"

        with pytest.raises(EnvelopeError, match="不在消息号表"):
            GameEnvelope().encode_with(Unknown())

    def test_missing_sub_packet_is_reported(self) -> None:
        """响应不含子包时，强行解析要给明确提示（而不是抛个 TypeError）。"""
        view = parse_envelope(bytes.fromhex("08c88506"))  # 只有 packetID 的极简响应
        with pytest.raises(EnvelopeError, match="不含业务子包"):
            view.decode_sub_packet(VersionControlServerRes)


if __name__ == "__main__":  # 支持 `python -m tests.test_game_envelope`
    raise SystemExit(pytest.main([__file__, "-v", "--no-header"]))

