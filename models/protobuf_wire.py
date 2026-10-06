"""protobuf wire format 的最小实现（只实现我们需要的那部分）。

【为什么自己写，而不是装 protobuf 官方库】
1. **游戏用的是 protobuf-net**（C# 版），它的 ``[ProtoMember(n)]`` 编号规则与
   ``.proto`` 文件无关；官方 Python 库要靠 ``.proto`` / descriptor 才能工作，
   而这份游戏根本没有 ``.proto`` —— 我们手上只有 dump.cs 里的字段清单；
2. wire format 本身**极其简单**（只有 varint 与「标签+长度+负载」两类编码），
   一百多行就能写全，且每一行都能用公开的标准向量验证；
3. 少一个二进制依赖，就少一类「换台机器就装不上」的问题。

【wire format 的本质（读懂这 4 条就够用）】
每个字段在字节流里长这样::

    标签(tag)  +  负载

- **标签** = ``(字段编号 << 3) | 线类型``，本身用 varint 编码；
- **线类型（wire type）**只有四种：

  ====  ==================  =======================================
  数值  名称                负载形态
  ====  ==================  =======================================
  0     varint              变长整数（int32 / int64 / bool / enum）
  1     64-bit              固定 8 字节（double / fixed64）
  2     length-delimited    先 varint 长度，再 N 字节（string / bytes / 嵌套消息）
  5     32-bit              固定 4 字节（float / fixed32）
  ====  ==================  =======================================

- **varint**：每字节用低 7 位存数据、最高位表示「后面还有没有」。
  所以 150 编码成 ``96 01``：0x96 去掉续位是 22，再接 0x01（高位）→ 1×128+22 = 150；
- **负数**：int32 的负数按 **64 位补码**编码，固定占 10 字节。
  这一点最容易写错：若按 32 位补码处理，长度会少 5 字节，
  服务端从下一字段开始全部错位。

【可直接引用的标准向量（测试里就是这么断言的）】
- ``08 96 01``                  → 字段 1（varint）= 150
- ``12 07 "testing"``           → 字段 2（字符串）= "testing"
- ``22 06 03 8E 02 9E A7 05``   → 字段 4（packed int32）= [3, 270, 86942]
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Iterator, Sequence

# ---- 线类型常量 ----------------------------------------------------------
WIRE_VARINT: Final[int] = 0
WIRE_FIXED64: Final[int] = 1
WIRE_LENGTH_DELIMITED: Final[int] = 2
WIRE_FIXED32: Final[int] = 5

#: 线类型 → 人类可读名称（报错信息里用，比裸数字好懂得多）
WIRE_TYPE_NAMES: Final[dict[int, str]] = {
    WIRE_VARINT: "varint",
    WIRE_FIXED64: "64bit",
    WIRE_LENGTH_DELIMITED: "length-delimited",
    WIRE_FIXED32: "32bit",
}

#: protobuf 允许的最大字段编号（2^29 - 1）
MAX_FIELD_NUMBER: Final[int] = (1 << 29) - 1


class ProtobufWireError(ValueError):
    """字节流不是合法的 protobuf 数据，或字段编号非法。

    出现本异常通常意味着「我们对报文的假设有偏差」——
    例如把某个字段猜成了嵌套消息，而它其实是字符串。
    这类问题必须报出来：静默跳过会让解析结果**看起来正常，却少了一部分字段**。
    """


# ---------------------------------------------------------------------------
# varint
# ---------------------------------------------------------------------------
def encode_varint(value: int) -> bytes:
    """把整数编码成 varint。

    :param value: 任意整数。**负数会按 64 位补码编码**（固定 10 字节），
        这是 protobuf 的规定动作，不是写法选择（见模块文档第 4 条）。

    >>> encode_varint(150).hex()
    '9601'
    >>> len(encode_varint(-1))
    10
    """
    if value < 0:
        # 关键一步：负数先转成 64 位无符号表示，再走正常的 varint 流程。
        # 漏掉它会让 -1 变成 1 字节的 0x01，整包随之错位。
        value += 1 << 64

    out = bytearray()
    while True:
        chunk = value & 0x7F
        value >>= 7
        if value:
            out.append(chunk | 0x80)
        else:
            out.append(chunk)
            return bytes(out)


def decode_varint(data: bytes, offset: int = 0) -> tuple[int, int]:
    """从 ``data[offset:]`` 解出一个 varint。

    :return: ``(值, 下一个字节的位置)``。
    :raises ProtobufWireError: 数据被截断，或 varint 超过 10 字节（非法编码）。

    为什么最多 10 字节？
        64 位数据每 7 位一组，最多 10 组。超过就说明这**不是** varint，
        更可能是字段对齐错了 —— 早点报错，好过读出一个天文数字继续往下跑。
    """
    result = 0
    shift = 0
    position = offset

    while True:
        if position >= len(data):
            raise ProtobufWireError(
                f"varint 被截断：偏移 {position} 已到数据末尾（共 {len(data)} 字节）"
            )
        byte = data[position]
        position += 1
        result |= (byte & 0x7F) << shift

        if not byte & 0x80:
            return result, position

        shift += 7
        if shift >= 70:
            raise ProtobufWireError(
                f"varint 超过 10 字节（偏移 {offset} 处），数据很可能不是 protobuf"
            )


# ---------------------------------------------------------------------------
# 标签
# ---------------------------------------------------------------------------
def encode_tag(field_number: int, wire_type: int) -> bytes:
    """编码字段标签（``(编号 << 3) | 线类型``）。

    :raises ProtobufWireError: 编号不在 1~2^29-1，或线类型不认识。

    编号 1~15 的标签只占 1 字节，16~2047 占 2 字节 —— 所以协议设计者总把
    最高频的字段（消息号、ID）放在前 15 号。这也是为什么 dump.cs 里
    各报文的 ``misID``、``errorCode`` 通常排在最前面。
    """
    if not 1 <= field_number <= MAX_FIELD_NUMBER:
        raise ProtobufWireError(
            f"字段编号 {field_number} 非法：必须在 1~{MAX_FIELD_NUMBER} 之间"
            "（0 是保留值，protobuf 不允许使用）"
        )
    if wire_type not in WIRE_TYPE_NAMES:
        raise ProtobufWireError(
            f"未知线类型 {wire_type}，合法值：{sorted(WIRE_TYPE_NAMES)}"
        )
    return encode_varint((field_number << 3) | wire_type)


# ---------------------------------------------------------------------------
# 字段编码
# ---------------------------------------------------------------------------
def encode_int(field_number: int, value: int) -> bytes:
    """编码 int32 / int64 / bool / enum 字段（线类型 0）。"""
    return encode_tag(field_number, WIRE_VARINT) + encode_varint(int(value))


def encode_bool(field_number: int, value: bool) -> bytes:
    """编码布尔字段（本质就是 0 / 1 的 varint）。"""
    return encode_int(field_number, 1 if value else 0)


def encode_bytes(field_number: int, value: bytes) -> bytes:
    """编码 bytes 字段（线类型 2）。"""
    return encode_tag(field_number, WIRE_LENGTH_DELIMITED) + encode_varint(len(value)) + value


def encode_string(field_number: int, value: str) -> bytes:
    """编码字符串字段。

    刻意使用严格的 ``utf-8``（不替换非法字节）：中文文案若因编码问题变成乱码，
    我们希望**立刻看到异常**，而不是把坏数据发给服务端。
    """
    return encode_bytes(field_number, value.encode("utf-8"))


def encode_message(field_number: int, payload: bytes) -> bytes:
    """把一个已编码好的子消息作为字段嵌入（线类型 2）。"""
    return encode_bytes(field_number, payload)


def encode_packed_ints(field_number: int, values: Sequence[int]) -> bytes:
    """编码 packed 形式的 repeated int 字段。

    protobuf-net 里被标注的 ``List<int>`` 默认就是 packed
    （一个标签 + 连续的 varint），而不是「每个元素一个标签」。
    写错这一点的症状是：服务端读到的列表比预期短，甚至直接解析失败。
    """
    payload = b"".join(encode_varint(int(value)) for value in values)
    return encode_bytes(field_number, payload)


def encode_repeated_ints(field_number: int, values: Sequence[int]) -> bytes:
    """编码**非** packed 形式的 repeated int（每个元素带一个标签）。

    它与 :func:`encode_packed_ints` 并存，是因为两种写法线上都真实存在：
    protobuf-net 对 ``List<int>`` 默认发 packed，但手工构造的包可能逐个发。
    **解析时必须两种都能读**（见 :func:`iter_fields`），
    发送时则要按服务端期望的形态来 —— 这一条只能靠抓包确认。
    """
    return b"".join(encode_int(field_number, int(value)) for value in values)


def encode_fixed64(field_number: int, value: int) -> bytes:
    """编码 fixed64 / double 的整数形态（线类型 1，小端序）。

    完整实现在解析一侧才有意义（见 :func:`iter_fields`）；
    这里保留对称能力，避免将来需要发 double 时再回头改结构。
    """
    return encode_tag(field_number, WIRE_FIXED64) + int(value).to_bytes(
        8, "little", signed=False
    )


def encode_fixed32(field_number: int, value: int) -> bytes:
    """编码 fixed32 / float 的整数形态（线类型 5，小端序）。"""
    return encode_tag(field_number, WIRE_FIXED32) + int(value).to_bytes(
        4, "little", signed=False
    )



# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
#: 线类型 3 / 4 是 protobuf 早期版本的分组（group）编码，现已废弃。
#: 若真的遇到，说明我们对报文的假设有偏差，必须人工看一眼，而不是硬解。
_GROUP_WIRE_TYPES: Final[frozenset[int]] = frozenset({3, 4})


@dataclass(frozen=True)
class Field:
    """从字节流里读出的一个字段。

    :param number: 字段编号（对应 dump.cs 里 ``[ProtoMember(n)]`` 的 n）。
    :param wire_type: 线类型。
    :param value: varint / 32bit / 64bit 字段是 ``int``；
        length-delimited 字段是原始 ``bytes``（字符串、子消息、packed 数组都走这里）。
    """

    number: int
    wire_type: int
    value: int | bytes

    # ---- 取值 ------------------------------------------------------------
    def as_int64(self) -> int:
        """按 int64 解释 varint。"""
        self._require(WIRE_VARINT)
        return int(self.value)

    def as_int32(self) -> int:
        """按 int32 解释 varint（**自动还原负数**）。

        【为什么需要专门的方法】
        protobuf 规定 int32 的负数编码成 **64 位补码**（占 10 字节），
        所以 ``-1`` 读出来是 ``0xFFFFFFFFFFFFFFFF`` 这个巨大的正数。
        不做还原，你会在日志里看到 ``misID = 18446744073709551615`` 这种值，
        根本想不到它其实是 -1。

        判定规则：值 >= 2^63 才视为负数。**故意不用「>= 2^31 就算负数」**——
        那会把合法的正数 3000000000 误判成 -1294967296。
        """
        self._require(WIRE_VARINT)
        raw = int(self.value)
        if raw >= 1 << 63:
            return raw - (1 << 64)
        return raw

    def as_bool(self) -> bool:
        """按布尔解释 varint（0 → False，其它 → True）。"""
        return self.as_int64() != 0

    def as_bytes(self) -> bytes:
        """取 length-delimited 字段的原始字节。"""
        self._require(WIRE_LENGTH_DELIMITED)
        raw = self.value
        assert isinstance(raw, bytes)  # 让类型检查器收窄类型
        return raw

    def as_string(self) -> str:
        """按 UTF-8 解释 length-delimited 字段。

        用 ``errors="replace"``：响应里可能含玩家自己输入的文字，
        一条坏数据不该让整包解析失败。**这与发送时严格编码是两种取向**：
        发送必须保证正确，接收要保证健壮。
        """
        return self.as_bytes().decode("utf-8", errors="replace")

    def as_fields(self) -> list["Field"]:
        """把 length-delimited 负载当作**子消息**递归解析。"""
        return list(iter_fields(self.as_bytes()))

    def as_packed_ints(self) -> list[int]:
        """把 length-delimited 负载当作 **packed varint 数组**解析。"""
        payload = self.as_bytes()
        values: list[int] = []
        position = 0
        while position < len(payload):
            value, position = decode_varint(payload, position)
            values.append(value)
        return values

    # ---- 调试 ------------------------------------------------------------
    def describe(self) -> str:
        """一行摘要（日志用；length-delimited 只显示长度与前 32 字节）。"""
        wire_name = WIRE_TYPE_NAMES.get(self.wire_type, f"?({self.wire_type})")
        if isinstance(self.value, bytes):
            preview = self.value[:32]
            suffix = "…" if len(self.value) > 32 else ""
            return f"#{self.number} {wire_name} len={len(self.value)} {preview!r}{suffix}"
        return f"#{self.number} {wire_name} {self.value}"

    def _require(self, expected_wire_type: int) -> None:
        """确认线类型符合预期，否则给出「我们猜错了」的明确提示。"""
        if self.wire_type != expected_wire_type:
            raise ProtobufWireError(
                f"字段 #{self.number} 的线类型是 "
                f"{WIRE_TYPE_NAMES.get(self.wire_type, self.wire_type)}，"
                f"但这里按 {WIRE_TYPE_NAMES.get(expected_wire_type, expected_wire_type)} 读取 —— "
                "说明该字段的类型与我们根据 dump.cs 做出的假设不一致"
            )



def iter_fields(data: bytes, *, stop_at_truncation: bool = False) -> Iterator[Field]:
    """逐字段解析一段 protobuf 数据。

    :param stop_at_truncation: 数据**被截断**时是否宽容处理。
        默认 ``False``：抛 :class:`ProtobufWireError`（见下面的 ``raises``）。
        置 ``True`` 时改为「能读多少读多少」，并把被截断的那个字段的
        **剩余字节**照样交出来，然后结束。

        【为什么需要这个模式】
        HAR 夹具只内联大响应的前 256 字节（``tools/extract_har.py`` 的
        ``PREVIEW_BYTES``），断点几乎必然落在某个字段中间。例如抓包里那条
        8872 字节的登录响应中，**区服列表包声明的长度是 8781 字节**，
        预览里只有它的开头 —— 但区服列表的头部（前几个区服）恰好在开头里，
        正是我们最需要的东西。严格模式下这里会直接报「数据被截断」，
        于是"能用的信息"被一条"整包不完整"的理由挡在门外。

        .. warning::
           宽容模式只应用于**明确知道来源被截断**的场景（夹具预览）。
           用它去解析真实完整响应会掩盖"字段对齐错了"这类问题 ——
           那种问题的症状正是"读到一半失败"，而宽容模式会让它看起来正常。

    :raises ProtobufWireError: 数据被截断、线类型非法、或长度前缀超出数据范围时
        （``stop_at_truncation=False`` 时）。

    为什么返回迭代器而不是字典？
        同一编号可以出现多次（repeated / packed 混用），迭代器能完整保留
        出现顺序与重复次数；需要按编号归类时用 :func:`fields_by_number`。
    """
    position = 0
    total = len(data)

    while position < total:
        try:
            tag, position = decode_varint(data, position)
        except ProtobufWireError:
            if stop_at_truncation:
                return
            raise
        field_number = tag >> 3
        wire_type = tag & 0x07

        if field_number == 0:
            raise ProtobufWireError(
                f"偏移 {position} 处读出的字段编号是 0，这不是合法的 protobuf 数据"
            )

        if wire_type == WIRE_VARINT:
            try:
                value, position = decode_varint(data, position)
            except ProtobufWireError:
                if stop_at_truncation:
                    return
                raise
            yield Field(field_number, wire_type, value)

        elif wire_type == WIRE_LENGTH_DELIMITED:
            try:
                length, position = decode_varint(data, position)
            except ProtobufWireError:
                if stop_at_truncation:
                    return
                raise
            end = position + length
            if end > total:
                if not stop_at_truncation:
                    raise ProtobufWireError(
                        f"字段 #{field_number} 声明长度 {length}，但剩余数据只有 "
                        f"{total - position} 字节（数据被截断）"
                    )
                # 截断：把**已有**的字节交出去（可能是空的），然后结束。
                # 交出而不是丢弃，是因为这些字节里往往正好有我们要的那几个字段。
                if position < total:
                    yield Field(field_number, wire_type, data[position:total])
                return
            yield Field(field_number, wire_type, data[position:end])
            position = end

        elif wire_type == WIRE_FIXED64:
            end = position + 8
            if end > total:
                if stop_at_truncation:
                    return  # 8 字节缺一点就凑不出值，直接结束（交不出半个整数）
                raise ProtobufWireError(f"字段 #{field_number} 的 64 位数据被截断")
            yield Field(field_number, wire_type, int.from_bytes(data[position:end], "little"))
            position = end

        elif wire_type == WIRE_FIXED32:
            end = position + 4
            if end > total:
                if stop_at_truncation:
                    return
                raise ProtobufWireError(f"字段 #{field_number} 的 32 位数据被截断")
            yield Field(field_number, wire_type, int.from_bytes(data[position:end], "little"))
            position = end

        elif wire_type in _GROUP_WIRE_TYPES:
            raise ProtobufWireError(
                f"字段 #{field_number} 使用了已废弃的 group 编码（线类型 {wire_type}）—— "
                "protobuf-net 不会生成这种数据，请先确认字段对齐是否正确"
            )

        else:
            raise ProtobufWireError(
                f"字段 #{field_number} 的线类型 {wire_type} 未知（合法值 0/1/2/5）"
            )


def fields_by_number(
    data: bytes, *, stop_at_truncation: bool = False
) -> dict[int, list[Field]]:
    """把一段数据解析成 ``{字段编号: [字段, ...]}``。

    用列表而不是单值：repeated / packed 字段天然会出现多次，
    强行只取最后一个会**静默丢数据**。

    :param stop_at_truncation: 见 :func:`iter_fields`（默认严格模式）。
    """
    grouped: dict[int, list[Field]] = {}
    for field in iter_fields(data, stop_at_truncation=stop_at_truncation):
        grouped.setdefault(field.number, []).append(field)
    return grouped


def read_int_list(grouped: dict[int, list[Field]], number: int) -> list[int]:
    """读取 repeated int 字段，**同时兼容 packed 与非 packed 两种形态**。

    这是解析响应时最常用的一行代码：我们事先无法确定服务端发的是哪种形态
    （protobuf-net 默认 packed，但它也能读非 packed），所以两种都要认。

    字段缺失时返回空列表而不是抛异常 —— 服务端省略空数组是正常行为，
    把它当错误处理会让你在「某个列表为空」时莫名其妙地失败。
    """
    values: list[int] = []
    for field in grouped.get(number, []):
        if field.wire_type == WIRE_VARINT:
            # 非 packed：每个元素带一个标签
            values.append(field.as_int32())
        elif field.wire_type == WIRE_LENGTH_DELIMITED:
            # packed：一个标签 + 连续 varint
            values.extend(field.as_packed_ints())
        else:
            raise ProtobufWireError(
                f"字段 #{number} 期望 repeated int，但线类型是 "
                f"{WIRE_TYPE_NAMES.get(field.wire_type, field.wire_type)}"
            )
    return values

