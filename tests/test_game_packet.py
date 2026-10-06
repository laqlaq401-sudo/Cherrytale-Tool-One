"""protobuf 报文基类（``models/game_packet.py``）的单元测试。

运行方式：

    python -m tests.test_game_packet
    python -m pytest tests/test_game_packet.py -q

【为什么用 monkeypatch 注入「测试用字段表」】
字段编号要从 ``models/proto_fields.py`` 查，那张表由 dump.cs 生成 ——
测试里凭空造一个类名会直接 KeyError。所以这里往表里塞两个测试专用类名
（``TestInner`` / ``TestOuter``），既能把基类完整验证一遍，又不用动生成文件。
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import Field

from models import proto_fields
from models.game_packet import (
    ProtoEncodeError,
    ProtoMessage,
    is_default_value,
    is_message_type,
    list_item_type,
    unwrap_optional,
)


class TestAnnotationHelpers:
    """注解解析工具（基类的编解码全靠它们推断类型）。"""

    def test_unwrap_optional(self) -> None:
        assert unwrap_optional(int | None) is int
        assert unwrap_optional(str) is str

    def test_list_item_type(self) -> None:
        assert list_item_type(list[int]) is int
        assert list_item_type(list[str]) is str
        assert list_item_type(int) is None

    def test_list_item_type_through_optional(self) -> None:
        """``list[int] | None`` 也要能看出元素类型。"""
        assert list_item_type(list[int] | None) is int


class TestIsDefaultValue:
    """``include_defaults=False`` 时用来判断「这个字段该不该省略」。"""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, True),
            (0, True),
            (False, True),
            ("", True),
            ([], True),
            (b"", True),
            (0.0, True),
            (1, False),
            (True, False),
            ("x", False),
            ([0], False),
            (b"\x00", False),
        ],
    )
    def test_matrix(self, value: Any, expected: bool) -> None:
        assert is_default_value(value) is expected


@pytest.fixture
def packets(monkeypatch: pytest.MonkeyPatch) -> tuple[type, type]:
    """构造两个测试报文类（内层 + 外层），并把字段名注入编号表。

    :return: ``(TestInner, TestOuter)``
    """
    monkeypatch.setitem(proto_fields.PROTO_FIELDS, "TestInner", ("x", "y"))
    monkeypatch.setitem(
        proto_fields.PROTO_FIELDS,
        "TestOuter",
        ("a", "b", "c", "d", "e", "inner", "inners", "blob", "ratio"),
    )

    class TestInner(ProtoMessage):
        proto_class_name = "TestInner"
        x: int = 0
        y: str = ""

    class TestOuter(ProtoMessage):
        proto_class_name = "TestOuter"
        a: int = 0
        b: str = ""
        c: bool = False
        d: list[int] = Field(default_factory=list)
        e: list[str] = Field(default_factory=list)
        inner: TestInner | None = None
        inners: list[TestInner] = Field(default_factory=list)
        blob: bytes = b""
        ratio: float = 0.0

    return TestInner, TestOuter


def test_is_message_type_recognises_proto_message() -> None:
    """``is_message_type`` 要能把「嵌套消息」与普通类型区分开。"""

    class Sample(ProtoMessage):
        proto_class_name = "Sample"

    assert is_message_type(Sample) is True
    assert is_message_type(Sample | None) is True
    assert is_message_type(int) is False


class TestEncode:
    def test_field_tags_come_from_generated_table(self, packets: tuple[type, type]) -> None:
        _inner, outer = packets
        assert outer.field_tag_map() == {
            "a": 1,
            "b": 2,
            "c": 3,
            "d": 4,
            "e": 5,
            "inner": 6,
            "inners": 7,
            "blob": 8,
            "ratio": 9,
        }

    # ------------------------------------------------------------------
    # 下面这些「精确到字节」的用例统一用 include_defaults=False。
    #
    # 【为什么】
    # 默认模式（True）会把所有非 None 字段都发出去，包括 0 / False / ""，
    # 于是每个用例的期望值里都会混进一堆「零值字段」的字节，
    # 真正要验证的那几个字节反而被淹没。
    # 该模式本身的行为由 test_include_defaults_false_skips_zeros 单独覆盖。
    # ------------------------------------------------------------------
    def test_scalar_fields(self, packets: tuple[type, type]) -> None:
        _inner, outer = packets
        # 字段 1 = int 150 → 08 96 01；字段 2 = "hi" → 12 02 68 69
        assert outer(a=150, b="hi").encode(include_defaults=False).hex() == "08960112026869"

    def test_negative_int_uses_ten_bytes(self, packets: tuple[type, type]) -> None:
        _inner, outer = packets
        assert (
            outer(a=-1).encode(include_defaults=False).hex()
            == "08" + "ffffffffffffffffff01"
        )

    def test_packed_int_list(self, packets: tuple[type, type]) -> None:
        _inner, outer = packets
        # 字段 4 的标签 = 0x22，长度 6，载荷 03 8E 02 9E A7 05
        assert outer(d=[3, 270, 86942]).encode(include_defaults=False).hex() == "2206038e029ea705"

    def test_string_list_is_not_packed(self, packets: tuple[type, type]) -> None:
        """repeated string 不支持 packed，必须每个元素带一次标签。"""
        _inner, outer = packets
        assert outer(e=["a", "b"]).encode(include_defaults=False).hex() == "2a01612a0162"

    def test_empty_list_is_omitted(self, packets: tuple[type, type]) -> None:
        """空集合与「省略该字段」在 protobuf 里语义相同，不必浪费字节。"""
        _inner, outer = packets
        # 用 include_defaults=False 才能看出「只有空列表这一个字段」的结果：
        # 默认模式会把其它零值字段也发出去，报文自然不是空的。
        assert outer(d=[]).encode(include_defaults=False) == b""

    def test_nested_message(self, packets: tuple[type, type]) -> None:
        inner, outer = packets
        # 字段 6 的标签 = 0x32；内层 x=7 → 08 07
        assert outer(inner=inner(x=7)).encode(include_defaults=False).hex() == "32020807"

    def test_repeated_nested_messages(self, packets: tuple[type, type]) -> None:
        inner, outer = packets
        assert (
            outer(inners=[inner(x=1), inner(x=2)]).encode(include_defaults=False).hex()
            == "3a0208013a020802"
        )

    def test_nested_message_inherits_include_defaults(self, packets: tuple[type, type]) -> None:
        """``include_defaults`` 必须传到嵌套消息。

        否则会出现「外层省了、内层还发」这种看不出来的不一致：
        内层 ``x=0`` 会变成 ``0800`` 混进报文里。
        """
        inner, outer = packets
        # 内层 x 保持默认值 0、y 有值 → 只应出现 y 的字节（1201 79）
        encoded = outer(inner=inner(y="y")).encode(include_defaults=False)
        assert encoded.hex() == "3203120179"

    def test_chinese_string(self, packets: tuple[type, type]) -> None:
        _inner, outer = packets
        assert outer(b="武汉").encode(include_defaults=False).hex() == "1206e6ada6e6b189"

    def test_include_defaults_false_skips_zeros(self, packets: tuple[type, type]) -> None:
        """两种模式的差异：``False`` 只发非默认值，``True`` 把零值字段也发出去。

        发零值是合法的（protobuf 里「字段缺失」与「值为 0」在非 optional 字段上等价），
        而且报文内容与模型字段一一对应，调试时更好核对 —— 所以默认用 ``True``。
        """
        _inner, outer = packets
        assert outer(a=0, b="x").encode(include_defaults=False).hex() == "120178"

        # True 模式：a=0 → 0800、b="x" → 120178、c=False → 1800、
        #            blob 为空 → 4200、ratio=0.0 → 49 + 8 字节零
        assert (
            outer(a=0, b="x").encode(include_defaults=True).hex()
            == "080012017818004200490000000000000000"
        )

    def test_missing_field_in_model_raises(
        self, packets: tuple[type, type], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """字段表里有、模型里没有 → 必须报错。

        漏发字段会让服务端收到不完整的请求，而它的错误信息通常
        不会告诉你「少了哪个字段」，排查成本极高。
        """
        _inner, outer = packets
        monkeypatch.setitem(
            proto_fields.PROTO_FIELDS, "TestOuter", ("a", "b", "并不存在的字段")
        )
        with pytest.raises(ProtoEncodeError, match="落后于 dump.cs"):
            outer(a=1).encode()

    def test_missing_proto_class_name_raises(self) -> None:
        class NoName(ProtoMessage):
            pass

        with pytest.raises(ProtoEncodeError, match="proto_class_name"):
            NoName().encode()


class TestDecode:
    def test_roundtrip_all_types(self, packets: tuple[type, type]) -> None:
        inner, outer = packets
        original = outer(
            a=249000007,
            b="日常",
            c=True,
            d=[1, 2, 3],
            e=["x", "y"],
            inner=inner(x=5, y="内"),
            inners=[inner(x=6)],
            blob=b"\x00\xff",
            ratio=0.5,
        )
        assert outer.decode(original.encode()) == original

    def test_unknown_field_is_ignored(self, packets: tuple[type, type]) -> None:
        """服务端版本比客户端新是常态，多一个字段不该让整包解析失败。"""
        _inner, outer = packets
        data = outer(a=1).encode() + bytes.fromhex("4807")  # 字段 9 之外的编号
        assert outer.decode(data).a == 1

    def test_missing_field_uses_default(self, packets: tuple[type, type]) -> None:
        _inner, outer = packets
        decoded = outer.decode(outer(a=1).encode())
        assert decoded.b == ""
        assert decoded.d == []

    def test_accepts_non_packed_int_list(self, packets: tuple[type, type]) -> None:
        """线上两种写法都真实存在，解析必须都认。"""
        _inner, outer = packets
        # 字段 4，逐个带标签：20 01 20 02
        assert outer.decode(bytes.fromhex("20012002")).d == [1, 2]


class TestDebugHelpers:
    def test_to_hex_matches_encode(self, packets: tuple[type, type]) -> None:
        _inner, outer = packets
        message = outer(a=1)
        assert message.to_hex() == message.encode().hex()

    def test_describe_shows_field_names(self, packets: tuple[type, type]) -> None:
        _inner, outer = packets
        text = outer(a=7, b="hi").describe()
        assert "TestOuter" in text
        assert "#1 a = 7" in text
        assert "#2 b = 'hi'" in text


if __name__ == "__main__":  # 支持 `python -m tests.test_game_packet`
    raise SystemExit(pytest.main([__file__, "-v", "--no-header"]))

