"""protobuf wire format 实现的单元测试。

运行方式：

    python -m tests.test_protobuf_wire
    python -m pytest tests/test_protobuf_wire.py -q

【为什么这些测试用「硬编码的十六进制」而不是往返比较】
往返比较（encode → decode → 相等）只能证明**自洽**，不能证明**正确**：
如果我把 varint 的续位写反了，编码和解码会一起错，往返依然通过，
但发给服务端的字节是错的 —— 而服务端只会静默丢弃或报一个含糊的错误。

所以这里用 **protobuf 官方文档里的标准示例字节**作为基准，
它们是全世界 protobuf 实现共同遵循的约定，与我们的代码无关。
"""

from __future__ import annotations

import pytest

from models.protobuf_wire import (
    MAX_FIELD_NUMBER,
    WIRE_LENGTH_DELIMITED,
    WIRE_VARINT,
    Field,
    ProtobufWireError,
    decode_varint,
    encode_bool,
    encode_bytes,
    encode_int,
    encode_message,
    encode_packed_ints,
    encode_repeated_ints,
    encode_string,
    encode_tag,
    encode_varint,
    fields_by_number,
    iter_fields,
    read_int_list,
)


class TestVarint:
    """varint 编码（官方示例：150 → ``96 01``）。"""

    def test_single_byte_values(self) -> None:
        assert encode_varint(0) == b"\x00"
        assert encode_varint(1) == b"\x01"
        assert encode_varint(127) == b"\x7f"  # 单字节上限

    def test_two_byte_values(self) -> None:
        assert encode_varint(128) == b"\x80\x01"
        assert encode_varint(150) == bytes.fromhex("9601")
        assert encode_varint(16383) == bytes.fromhex("ff7f")

    def test_negative_needs_ten_bytes(self) -> None:
        """负数按 64 位补码编码，固定 10 字节。

        若按 32 位补码处理会少 5 字节，服务端从下一个字段起全部错位 ——
        这是最容易犯、也最难从现象上定位的错误。
        """
        encoded = encode_varint(-1)
        assert len(encoded) == 10
        assert encoded == bytes.fromhex("ffffffffffffffffff01")

    def test_roundtrip(self) -> None:
        for value in (0, 1, 127, 128, 300, 65535, 1 << 31, -1, -300, -(1 << 40)):
            encoded = encode_varint(value)
            if value < 0:
                # 负数是 64 位补码，解出来是无符号等价值
                decoded, position = decode_varint(encoded)
                assert decoded == value + (1 << 64)
            else:
                decoded, position = decode_varint(encoded)
                assert decoded == value
            assert position == len(encoded)

    def test_decodes_negative_as_unsigned(self) -> None:
        decoded, _ = decode_varint(bytes.fromhex("ffffffffffffffffff01"))
        assert decoded == (1 << 64) - 1

    def test_truncated_raises(self) -> None:
        with pytest.raises(ProtobufWireError, match="截断"):
            decode_varint(b"\x80")  # 声明「后面还有」，但数据没了

    def test_too_long_raises(self) -> None:
        with pytest.raises(ProtobufWireError, match="10 字节"):
            decode_varint(b"\x80" * 11)


class TestTag:
    def test_known_tags(self) -> None:
        """标签 = (编号 << 3) | 线类型，这三条覆盖了最常见的组合。"""
        assert encode_tag(1, WIRE_VARINT) == b"\x08"
        assert encode_tag(2, WIRE_LENGTH_DELIMITED) == b"\x12"
        assert encode_tag(4, WIRE_LENGTH_DELIMITED) == b"\x22"

    def test_field_number_boundaries(self) -> None:
        assert encode_tag(15, WIRE_VARINT) == b"\x78"          # 单字节标签上限
        assert encode_tag(16, WIRE_VARINT) == bytes.fromhex("8001")  # 变成两字节

    def test_rejects_field_number_zero(self) -> None:
        """编号 0 是保留值，用了它说明字段对齐已经错了。"""
        with pytest.raises(ProtobufWireError, match="非法"):
            encode_tag(0, WIRE_VARINT)

    def test_rejects_out_of_range_field_number(self) -> None:
        with pytest.raises(ProtobufWireError, match="非法"):
            encode_tag(MAX_FIELD_NUMBER + 1, WIRE_VARINT)

    def test_rejects_unknown_wire_type(self) -> None:
        with pytest.raises(ProtobufWireError, match="线类型"):
            encode_tag(1, 7)


class TestOfficialVectors:
    """protobuf 官方文档的标准示例（与我们的实现无关的第三方基准）。"""

    def test_int_field(self) -> None:
        assert encode_int(1, 150) == bytes.fromhex("089601")

    def test_string_field(self) -> None:
        assert encode_string(2, "testing") == bytes.fromhex("120774657374696e67")

    def test_packed_int_list(self) -> None:
        assert encode_packed_ints(4, [3, 270, 86942]) == bytes.fromhex(
            "2206038e029ea705"
        )

    def test_chinese_string(self) -> None:
        """中文按 UTF-8 编码：``武汉`` = E6 AD A6 E6 B1 89。"""
        assert encode_string(2, "武汉") == bytes.fromhex("1206e6ada6e6b189")

    def test_negative_int32(self) -> None:
        assert encode_int(1, -1) == bytes.fromhex("08ffffffffffffffffff01")


class TestDecode:
    """解析侧：字节流 → 字段。"""

    def test_iter_fields_basic(self) -> None:
        data = encode_int(1, 150) + encode_string(2, "hi")
        fields = list(iter_fields(data))
        assert [(f.number, f.wire_type) for f in fields] == [
            (1, WIRE_VARINT),
            (2, WIRE_LENGTH_DELIMITED),
        ]
        assert fields[0].as_int32() == 150
        assert fields[1].as_string() == "hi"

    def test_fields_by_number_groups_repeats(self) -> None:
        data = encode_int(1, 1) + encode_int(1, 2) + encode_int(2, 9)
        grouped = fields_by_number(data)
        assert [f.as_int32() for f in grouped[1]] == [1, 2]
        assert [f.as_int32() for f in grouped[2]] == [9]

    def test_int32_negative_roundtrip(self) -> None:
        """-1 在线上是 64 位补码，必须还原成 -1 而不是天文数字。"""
        grouped = fields_by_number(encode_int(3, -1))
        assert grouped[3][0].as_int32() == -1

    def test_positive_large_value_is_not_mistaken_for_negative(self) -> None:
        """3_000_000_000 大于 2^31，但它是合法正数，不能被当成负数。"""
        grouped = fields_by_number(encode_int(3, 3_000_000_000))
        assert grouped[3][0].as_int32() == 3_000_000_000

    def test_bool(self) -> None:
        assert fields_by_number(encode_bool(1, True))[1][0].as_bool() is True
        assert fields_by_number(encode_bool(1, False))[1][0].as_bool() is False

    def test_nested_message(self) -> None:
        inner = encode_int(1, 7) + encode_string(2, "x")
        outer = encode_message(3, inner)
        child = fields_by_number(outer)[3][0]
        assert child.as_fields()[0].as_int32() == 7
        assert child.as_fields()[1].as_string() == "x"

    def test_packed_int_list(self) -> None:
        grouped = fields_by_number(encode_packed_ints(4, [3, 270, 86942]))
        assert read_int_list(grouped, 4) == [3, 270, 86942]

    def test_non_packed_int_list(self) -> None:
        grouped = fields_by_number(encode_repeated_ints(4, [3, 270, 86942]))
        assert read_int_list(grouped, 4) == [3, 270, 86942]

    def test_missing_field_yields_empty_list(self) -> None:
        """服务端省略空数组是正常行为，不能当错误处理。"""
        assert read_int_list(fields_by_number(b""), 4) == []

    def test_describe_is_single_line(self) -> None:
        grouped = fields_by_number(encode_string(2, "a\nb"))
        text = grouped[2][0].describe()
        assert "\n" not in text

    def test_rejects_field_number_zero(self) -> None:
        with pytest.raises(ProtobufWireError, match="字段编号是 0"):
            list(iter_fields(bytes.fromhex("0000")))

    def test_rejects_truncated_length_delimited(self) -> None:
        with pytest.raises(ProtobufWireError, match="截断"):
            list(iter_fields(bytes.fromhex("120774")))

    def test_rejects_group_wire_type(self) -> None:
        """线类型 3 是已废弃的 group；碰到它说明字段对齐错了。"""
        with pytest.raises(ProtobufWireError, match="group"):
            list(iter_fields(bytes.fromhex("0b")))

    def test_rejects_unknown_wire_type(self) -> None:
        with pytest.raises(ProtobufWireError, match="线类型"):
            list(iter_fields(bytes.fromhex("0f")))

    def test_wire_type_mismatch_message_is_actionable(self) -> None:
        """类型猜错时必须说明「哪里猜错了」，而不是抛一个裸的 TypeError。"""
        field = Field(number=5, wire_type=WIRE_LENGTH_DELIMITED, value=b"abc")
        with pytest.raises(ProtobufWireError, match="假设不一致"):
            field.as_int32()


class TestRoundTripWithModelLikePayload:
    """贴近真实用途的一次往返：模拟 DMReward 请求（misID + mistypeID）。"""

    def test_build_and_parse(self) -> None:
        payload = encode_int(1, 249000007) + encode_int(2, 1)
        grouped = fields_by_number(payload)
        assert grouped[1][0].as_int32() == 249000007
        assert grouped[2][0].as_int32() == 1

    def test_bytes_field_keeps_binary_content(self) -> None:
        """二进制字段必须原样保留：``bytes`` 里出现 0x00 也不能被截断。"""
        raw = b"\x00\x01\xff\x00"
        grouped = fields_by_number(encode_bytes(6, raw))
        assert grouped[6][0].as_bytes() == raw


class TestTruncationTolerantMode:
    """**容忍截断**模式（``stop_at_truncation=True``）。

    它存在的唯一理由：抓包夹具只内联大响应的前 256 字节，
    截断点几乎必然落在某个字段中间，而**断点之前的内容正是要读的东西**
    （实测：区服列表的头部就在那 256 字节里）。

    这里刻意用「自己构造 + 自己截断」的输入：不依赖任何手工算出来的偏移，
    所以用例本身不会因为算错字节而"看起来通过"。
    """

    def test_strict_mode_still_raises(self) -> None:
        """默认行为不变：严格模式遇到截断必须报错（否则会掩盖字段对齐错误）。"""
        data = encode_string(2, "hello") + encode_string(3, "cut")
        with pytest.raises(ProtobufWireError, match="截断"):
            fields_by_number(data[:-1])

    def test_tolerant_mode_keeps_complete_fields(self) -> None:
        """截断发生在**第二个**字段里时，第一个字段必须照样读出来。"""
        data = encode_int(1, 150) + encode_string(2, "hello")
        grouped = fields_by_number(data[:-1], stop_at_truncation=True)
        assert grouped[1][0].as_int32() == 150
        # 第二个字段声明 5 字节、实际只剩 4 字节 —— 宽容模式下交出已有的部分
        assert grouped[2][0].as_bytes() == b"hell"

    def test_tolerant_mode_stops_after_truncation(self) -> None:
        """截断之后的字节可能是别的包，不能继续当作本消息的字段解析。"""
        data = encode_int(1, 1) + encode_string(2, "abc") + encode_int(3, 9)
        grouped = fields_by_number(data[:-2], stop_at_truncation=True)
        assert set(grouped) == {1, 2}

    def test_tolerant_mode_on_complete_data_matches_strict(self) -> None:
        """数据完整时，两种模式的解析结果必须**完全一致**。"""
        data = encode_int(1, 7) + encode_string(2, "ok")
        strict = fields_by_number(data)
        tolerant = fields_by_number(data, stop_at_truncation=True)
        assert {n: [f.value for f in fs] for n, fs in strict.items()} == {
            n: [f.value for f in fs] for n, fs in tolerant.items()
        }


# ---------------------------------------------------------------------------
# 解析整条响应时的宽容开关（models/envelope.py）
# ---------------------------------------------------------------------------
class TestParseEnvelopeTruncation:
    """``parse_envelope`` 的宽容开关（``tolerate_truncation``）。

    报文按真实结构构造：根字段 1 是 ``packetID``，子包槽位编号 = 消息号。
    """

    @staticmethod
    def _truncated_body() -> bytes:
        """一条「子包只发了一半」的响应字节。"""
        body = encode_int(1, 1002) + encode_message(1002, encode_int(1, 0) + encode_int(2, 12345))
        return body[:-2]

    def test_truncated_response_is_rejected_by_default(self) -> None:
        from models.envelope import EnvelopeError, parse_envelope

        with pytest.raises(EnvelopeError, match="protobuf"):
            parse_envelope(self._truncated_body())

    def test_tolerant_mode_recovers_packet_id_and_partial_sub_packet(self) -> None:
        from models.envelope import parse_envelope

        view = parse_envelope(self._truncated_body(), tolerate_truncation=True)
        assert view.packet_id == 1002
        assert view.truncation_tolerant is True
        assert view.sub_packet_bytes  # 子包被截断，但已读到的部分仍然交出来



if __name__ == "__main__":  # 支持 `python -m tests.test_protobuf_wire`
    raise SystemExit(pytest.main([__file__, "-v", "--no-header"]))


