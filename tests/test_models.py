"""models 包单元测试：基类序列化、别名映射、信封成功判定。

运行方式：

    python -m tests.test_models
    python -m pytest tests/test_models.py -q

【说明：为什么测试里要自己定义模型？】
真实的 Req/Res 结构必须从 dump.cs 反推，现在还没有（见 models/__init__.py）。
但**基类的能力**（序列化、别名、打码、容错）与具体协议无关，
可以先用这里定义的“样例模型”充分验证。等真实模型写好后，
它们会自动继承这套已经过验证的能力。
"""

from __future__ import annotations

from typing import ClassVar

import pytest
from pydantic import Field

from models.base import PayloadError, ProtocolModel
from models.common import ResponseEnvelope, parse_json_payload


class SampleLoginReq(ProtocolModel):
    """样例请求模型（仅用于测试基类能力，**不代表真实协议字段**）。"""

    uid: str
    token: str = ""
    device_id: str | None = None

    #: 调试输出时 token 必须打码
    masked_fields: ClassVar[frozenset[str]] = frozenset({"token"})


class AliasedModel(ProtocolModel):
    """演示「C# 的 PascalCase → Python 的 snake_case」映射。"""

    login_name: str = Field(alias="LoginName")


class BinaryPayloadModel(ProtocolModel):
    """演示二进制字段（protobuf 报文正是以 bytes 形式承载）。"""

    proto_bytes: bytes = b""
    sequence: int = 0


# ---------------------------------------------------------------------------
# 基类：序列化
# ---------------------------------------------------------------------------
class TestProtocolModelSerialization:
    def test_to_payload_excludes_none_by_default(self) -> None:
        """值为 None 的字段默认不发送：空字段常被服务端判为非法参数。"""
        payload = SampleLoginReq(uid="u1").to_payload()
        assert payload == {"uid": "u1", "token": ""}
        assert "device_id" not in payload

    def test_to_payload_keeps_none_when_requested(self) -> None:
        payload = SampleLoginReq(uid="u1").to_payload(exclude_none=False)
        assert payload["device_id"] is None

    def test_to_json_keeps_chinese_readable(self) -> None:
        """默认不转义中文，日志里能直接读懂内容。"""
        model = AliasedModel(LoginName="训练师小零")
        assert "训练师小零" in model.to_json()
        assert "\\u" not in model.to_json()

    def test_to_json_bytes_is_utf8(self) -> None:
        model = AliasedModel(LoginName="中文")
        assert model.to_json_bytes().decode("utf-8") == model.to_json()

    def test_bytes_field_roundtrip(self) -> None:
        """protobuf 报文以 bytes 承载，必须原样保留、不能变成 str。"""
        raw = bytes([0x08, 0x96, 0x01, 0xFF])
        model = BinaryPayloadModel(proto_bytes=raw, sequence=7)
        assert model.to_payload()["proto_bytes"] == raw

    def test_with_updates_returns_copy(self) -> None:
        """改字段应产生副本，原对象保持不变，方便前后对比。"""
        original = SampleLoginReq(uid="u1")
        updated = original.with_updates(token="filled-later")

        assert updated.token == "filled-later"
        assert original.token == ""

    def test_with_updates_keeps_type(self) -> None:
        assert isinstance(SampleLoginReq(uid="u").with_updates(), SampleLoginReq)


# ---------------------------------------------------------------------------
# 基类：反序列化与容错
# ---------------------------------------------------------------------------
class TestProtocolModelParsing:
    def test_from_json_string(self) -> None:
        model = SampleLoginReq.from_json('{"uid": "u1", "token": "t"}')
        assert model.uid == "u1"
        assert model.token == "t"

    def test_from_json_bytes(self) -> None:
        model = SampleLoginReq.from_json(b'{"uid": "u1"}')
        assert model.uid == "u1"

    def test_from_json_mapping(self) -> None:
        model = SampleLoginReq.from_json({"uid": "u1"})
        assert model.uid == "u1"

    def test_unknown_fields_are_ignored(self) -> None:
        """逆向关键：服务端新增字段不能导致解析失败。"""
        model = SampleLoginReq.from_json(
            '{"uid": "u1", "future_feature_flag": true, "extra": [1, 2, 3]}'
        )
        assert model.uid == "u1"
        assert not hasattr(model, "future_feature_flag")

    def test_invalid_json_raises_with_preview(self) -> None:
        """报错要带上原始数据开头，让你一眼看出收到的是什么。"""
        with pytest.raises(PayloadError, match="不是合法的 JSON") as excinfo:
            SampleLoginReq.from_json("<html>503 Service Unavailable</html>")

        assert excinfo.value.raw_preview.startswith("<html>")

    def test_missing_required_field_raises(self) -> None:
        with pytest.raises(PayloadError, match="字段校验失败"):
            SampleLoginReq.from_json('{"token": "t"}')

    def test_wrong_type_raises(self) -> None:
        with pytest.raises(PayloadError, match="字段校验失败"):
            SampleLoginReq.from_json('{"uid": {"nested": 1}}')

    def test_alias_accepts_csharp_field_name(self) -> None:
        """dump 里是 PascalCase，Python 里想用 snake_case，靠别名打通。"""
        model = AliasedModel.from_json('{"LoginName": "u1"}')
        assert model.login_name == "u1"

    def test_alias_also_accepts_python_field_name(self) -> None:
        """populate_by_name=True 的效果：两种写法都能赋值。"""
        assert AliasedModel.from_json('{"login_name": "u1"}').login_name == "u1"

    def test_alias_serialization_uses_python_name(self) -> None:
        """to_payload 默认输出 Python 字段名；若要输出 C# 名需显式 by_alias。"""
        assert AliasedModel(LoginName="u1").to_payload() == {"login_name": "u1"}


# ---------------------------------------------------------------------------
# 基类：调试输出脱敏
# ---------------------------------------------------------------------------
class TestProtocolModelDebugOutput:
    def test_str_masks_sensitive_field(self) -> None:
        text = str(SampleLoginReq(uid="u1", token="super-secret"))
        assert "super-secret" not in text
        assert "'***'" in text

    def test_str_keeps_non_sensitive_field(self) -> None:
        assert "u1" in str(SampleLoginReq(uid="u1", token="super-secret"))

    def test_str_contains_class_name(self) -> None:
        assert str(SampleLoginReq(uid="u1")).startswith("SampleLoginReq(")


# ---------------------------------------------------------------------------
# 通用信封
# ---------------------------------------------------------------------------
class TestResponseEnvelope:
    def test_success_with_default_code(self) -> None:
        assert ResponseEnvelope(code=0).is_success() is True

    def test_failure_with_non_zero_code(self) -> None:
        assert ResponseEnvelope(code=1001, msg="参数错误").is_success() is False

    def test_custom_success_code(self) -> None:
        """真实成功码可能是 200 而不是 0，因此必须允许逐个调用点覆盖。"""
        envelope = ResponseEnvelope(code=200)
        assert envelope.is_success() is False
        assert envelope.is_success(success_code=200) is True

    def test_missing_code_is_not_success(self) -> None:
        """没有 code 字段时判为失败，避免「把失败当成功」。"""
        assert ResponseEnvelope(msg="whatever").is_success() is False

    def test_message_fallback(self) -> None:
        assert ResponseEnvelope(code=1).message() == "<服务端未提供描述>"

    def test_message_passthrough(self) -> None:
        assert ResponseEnvelope(code=1, msg="签名错误").message() == "签名错误"

    def test_data_payload_parsed(self) -> None:
        envelope = ResponseEnvelope.from_json('{"code":0,"data":{"level":12}}')
        assert envelope.data == {"level": 12}
        assert envelope.is_success() is True


# ---------------------------------------------------------------------------
# 载荷解析工具
# ---------------------------------------------------------------------------
class TestParseJsonPayload:
    def test_parse_dict(self) -> None:
        assert parse_json_payload('{"a": 1}') == {"a": 1}

    def test_parse_bytes(self) -> None:
        assert parse_json_payload(b'{"a": 1}') == {"a": 1}

    def test_parse_utf8_chinese(self) -> None:
        assert parse_json_payload('{"name": "训练师"}')["name"] == "训练师"

    def test_invalid_json_raises_with_preview(self) -> None:
        with pytest.raises(PayloadError, match="不是合法 JSON") as excinfo:
            parse_json_payload(b"\x00\x01\x02 protobuf binary")
        assert excinfo.value.raw_preview

    def test_html_error_page_detected(self) -> None:
        """抓包时常见的坑：返回的其实是网关的 HTML 错误页。"""
        with pytest.raises(PayloadError):
            parse_json_payload("<html><body>502 Bad Gateway</body></html>")


if __name__ == "__main__":  # 支持 `python -m tests.test_models`
    raise SystemExit(pytest.main([__file__, "-v", "--no-header"]))
