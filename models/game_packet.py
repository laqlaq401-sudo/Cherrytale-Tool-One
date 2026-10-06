"""protobuf 报文基类：把「字段清单」自动变成「可编解码的报文」。

【这一层解决什么问题】
每个报文的字段清单在 dump.cs 里，编号依据在 ``models/proto_fields.py``。
如果为每个报文手写一遍组包/解包代码，很快会出现三类问题：

1. 抄字段名时打错一个字母 —— 运行时报 KeyError，排查要翻半天；
2. 增删字段后忘了同步另一个方向（只改了 encode，没改 decode）；
3. 同一个类型判断在几十个文件里各写一遍，行为悄悄出现差异。

所以这里做一层**通用基类**：子类只声明「字段名 + 类型 + 默认值」，
编解码由基类按 Python 类型自动推断（int → varint、str → 长度前缀、嵌套模型 → 递归）。

【字段名为什么保持 dump.cs 里的 camelCase】
逆向场景下「与 dump.cs 一字不差」比「符合 PEP 8」更有价值：
编号要靠 ``proto_fields`` 里的字段名查表，一旦改名就要维护一层映射，
而那一层映射本身就是新的出错点。所以这里不做 snake_case 转换。

【一个待验证的取舍】
默认会把所有**非 None** 字段都发出去（包括 0 / False / 空字符串）。
protobuf 的语义里「字段缺失」与「值为 0」在非 optional 字段上等价，
所以这样发是合法的；但如果服务端用「字段是否存在」做业务判断，
就可能出现差异。抓包验证后如需调整，改 :meth:`ProtoMessage.encode` 的
``include_defaults`` 默认值即可 —— **不需要改任何子类**。
"""

from __future__ import annotations

import logging
import struct
from typing import Any, ClassVar, Final, Self, Sequence, ForwardRef, get_args, get_origin

from models.base import PayloadError, ProtocolModel
from models.protobuf_wire import (
    WIRE_FIXED64,
    WIRE_LENGTH_DELIMITED,
    WIRE_VARINT,
    Field,
    ProtobufWireError,
    encode_bool,
    encode_bytes,
    encode_int,
    encode_message,
    encode_packed_ints,
    encode_repeated_ints,
    encode_string,
    encode_tag,
    fields_by_number,
    read_int_list,
)
from models.proto_fields import field_tags

_LOGGER: Final[logging.Logger] = logging.getLogger("models.game_packet")


class ProtoEncodeError(TypeError):
    """字段的类型无法编码成 protobuf。

    出现本异常说明「模型里声明了一个基类不认识的类型」——
    例如忘了给嵌套消息继承 :class:`ProtoMessage`，或用了 dict 这类
    protobuf 不直接支持的类型。报错时会把字段名与类型一起打出来，
    因为单看类型很难想起是哪个字段。
    """


# ---------------------------------------------------------------------------
# 类型注解工具
# ---------------------------------------------------------------------------
def unwrap_optional(annotation: Any) -> Any:
    """把 ``X | None`` 拆成 ``X``（不是可选类型时原样返回）。

    为什么必须拆？
        pydantic 会把 ``int | None`` 保留成 ``Union[int, None]``，
        而编解码只关心「真正的类型是 int」。不拆的话，
        每个字段都要先判断一次 Optional，代码会迅速变得不可读。
    """
    origin = get_origin(annotation)
    if origin is None:
        return annotation
    args = [arg for arg in get_args(annotation) if arg is not type(None)]
    if len(args) == 1 and len(get_args(annotation)) == 2:
        return args[0]
    return annotation


def list_item_type(annotation: Any) -> Any | None:
    """若注解是 ``list[X]`` / ``Sequence[X]``，返回 ``X``；否则返回 ``None``。"""
    origin = get_origin(unwrap_optional(annotation))
    if origin is None:
        return None
    # list / Sequence / set 都按「重复字段」处理（游戏协议里只会用到 list）
    if origin in (list, set, frozenset, tuple, Sequence):
        args = get_args(unwrap_optional(annotation))
        return args[0] if args else Any
    return None


def is_message_type(annotation: Any) -> bool:
    """注解是否指向一个 :class:`ProtoMessage` 子类（即嵌套消息）。"""
    target = unwrap_optional(annotation)
    return isinstance(target, type) and issubclass(target, ProtoMessage)


def is_int_type(annotation: Any) -> bool:
    """注解是否是整数型（bool 单独处理，因为它是 int 的子类但不该走同一条路）。"""
    return unwrap_optional(annotation) in (int,) or unwrap_optional(annotation) is int


def is_default_value(value: Any) -> bool:
    """判断一个值是否属于「protobuf 默认值」（0 / False / 空字符串 / 空集合）。

    用于 ``include_defaults=False`` 的场景：那种模式下这些字段会被省略，
    服务端读到的仍是同样的默认值，但报文更短、也更贴近 protobuf-net 的默认行为。
    """
    if value is None:
        return True
    if isinstance(value, bool):
        return value is False
    if isinstance(value, (int, float)):
        return value == 0
    if isinstance(value, (str, bytes, bytearray)):
        return len(value) == 0
    if isinstance(value, (list, tuple, set, frozenset)):
        return len(value) == 0
    return False


def _ensure_model_complete(cls: type[ProtoMessage]) -> None:
    """前向引用兜底：模型尚未被 pydantic 完成构建时先 ``model_rebuild()``。

    【为什么需要它（2026-10-03 特惠礼包实测踩坑）】
    若模型的字段注解是**字符串前向引用**（如 ``list["GiftInfoClass"]`` 且
    ``GiftInfoClass`` 定义在后面），pydantic 会延迟构建该模型 —— 此时
    ``model_fields`` 里的注解还是未解析的 ``ForwardRef``，编解码器的类型
    推断认不出它，解码会静默走 bytes 兜底分支、编码会直接报错。
    在编解码入口处触发一次 rebuild 即可消除隐患；
    根治办法仍是"元素类先定义、注解不加引号"（见 models 各模块惯例）。
    """
    if not cls.__pydantic_complete__:
        cls.model_rebuild()


class ProtoMessage(ProtocolModel):
    """所有 protobuf 报文的基类。

    子类只做两件事：

    1. 设 ``proto_class_name``（**必须与协议类名一字不差**，编号表按它查）；
    2. 声明字段（名字与 dump.cs 里的属性名一致）。

    编解码完全由基类按字段类型推断：

    ==========================  ======================  ==============================
    Python 类型                  protobuf 线类型         说明
    ==========================  ======================  ==============================
    ``int``                     varint                  负数自动按 64 位补码编码
    ``bool``                    varint                  0 / 1
    ``str``                     length-delimited        UTF-8
    ``bytes``                   length-delimited        原样二进制
    ``float``                   64bit（double）          小端 IEEE754
    ``list[int]``               length-delimited        **packed**，元素是连续 varint
    ``list[str]``               每个元素一个标签         repeated string 不支持 packed
    ``ProtoMessage``            length-delimited        嵌套消息，递归编解码
    ``list[ProtoMessage]``      每个元素一个标签         repeated 消息
    ==========================  ======================  ==============================
    """

    #: 对应 dump.cs 里的协议类名（用于查字段编号表）
    proto_class_name: ClassVar[str] = ""

    #: 显式要求以非 packed（逐元素独立带 tag）编码的 repeated 字段名清单
    unpacked_fields: ClassVar[frozenset[str]] = frozenset()

    # ------------------------------------------------------------------
    # 类信息
    # ------------------------------------------------------------------
    @classmethod
    def field_tag_map(cls) -> dict[str, int]:
        """``{字段名: 编号}``（来自生成表，编号依据见 ``models/proto_fields.py``）。"""
        if not cls.proto_class_name:
            raise ProtoEncodeError(
                f"{cls.__name__} 没有设置 proto_class_name —— "
                "它是查字段编号的唯一依据，不能为空"
            )
        return field_tags(cls.proto_class_name)

    # ------------------------------------------------------------------
    # 编码
    # ------------------------------------------------------------------
    def encode(self, *, include_defaults: bool = True) -> bytes:
        """编码成 protobuf 字节。

        :param include_defaults: 是否发送「值是默认值」的字段（0 / False / ``""``）。
            默认 ``True``：protobuf 里「字段缺失」与「值为 0」在非 optional 字段上等价，
            显式发送能让报文内容与我们的模型一一对应，调试时更好核对。
        :raises ProtoEncodeError: 模型缺少字段表里的字段，或字段类型无法编码时。

        .. note::
           字段表里**有**、模型里**没有**的字段会直接报错，而不是悄悄跳过 ——
           漏发一个字段会让服务端收到不完整的请求，而它的报错信息
           通常不会告诉你「少了哪个字段」，排查成本极高。
        """
        _ensure_model_complete(type(self))
        tags = self.field_tag_map()
        declared = type(self).model_fields

        chunks: list[bytes] = []
        for name, tag in tags.items():
            if name not in declared:
                raise ProtoEncodeError(
                    f"{self.proto_class_name} 的字段 {name!r} 在模型 "
                    f"{type(self).__name__} 里不存在 —— 模型落后于 dump.cs 了，请补上该字段"
                )
            value = getattr(self, name, None)
            if value is None:
                continue
            if not include_defaults and is_default_value(value):
                continue
            chunks.append(
                self._encode_value(tag, value, declared[name].annotation, name, include_defaults)
            )
        return b"".join(chunks)

    @classmethod
    def _encode_value(
        cls, tag: int, value: Any, annotation: Any, name: str, include_defaults: bool
    ) -> bytes:
        """按运行时值 + 注解类型编码一个字段。

        :param include_defaults: **必须往下传到嵌套消息**。
            否则会出现这种很难发现的不一致：外层省略了默认值，
            内层却仍然把 ``x=0`` 发出去 —— 报文看起来「有时长有时短」，
            而原因藏在递归调用里。
        """
        if isinstance(value, ProtoMessage):
            return encode_message(tag, value.encode(include_defaults=include_defaults))

        item_type = list_item_type(annotation)
        if item_type is not None and isinstance(value, (list, tuple, set, frozenset)):
            items = list(value)
            if not items:
                return b""  # 空集合与「省略该字段」在 protobuf 里语义相同
            if is_message_type(item_type):
                return b"".join(
                    encode_message(tag, item.encode(include_defaults=include_defaults))
                    for item in items
                )
            if all(isinstance(item, str) for item in items):
                # repeated string 不能用 packed，必须每个元素带一次标签
                return b"".join(encode_string(tag, item) for item in items)
            if all(isinstance(item, (bytes, bytearray)) for item in items):
                # 「未建模的嵌套消息」用 bytes 占位：线格式与嵌套消息完全一样
                # （都是 length-delimited），所以原样发出去即可，不会丢数据。
                return b"".join(encode_bytes(tag, bytes(item)) for item in items)
            if all(isinstance(item, bool) for item in items):
                return b"".join(encode_bool(tag, item) for item in items)
            if all(isinstance(item, int) for item in items):
                if name in getattr(cls, "unpacked_fields", ()):
                    # 非 packed 形式：每个元素独立带标签（如 FightingResultPacket.starCount）
                    return encode_repeated_ints(tag, items)
                # protobuf-net 对 List<int> 默认发 packed
                return encode_packed_ints(tag, items)
            raise ProtoEncodeError(
                f"{name} 是列表，但元素类型 {type(items[0]).__name__} 无法编码"
                "（支持 int / bool / str / ProtoMessage）"
            )

        if isinstance(value, bool):
            return encode_bool(tag, value)
        if isinstance(value, int):
            return encode_int(tag, value)
        if isinstance(value, float):
            return encode_tag(tag, WIRE_FIXED64) + struct.pack("<d", value)
        if isinstance(value, str):
            return encode_string(tag, value)
        if isinstance(value, (bytes, bytearray)):
            return encode_bytes(tag, bytes(value))

        raise ProtoEncodeError(
            f"{name} 的类型 {type(value).__name__} 无法编码成 protobuf —— "
            "嵌套结构请继承 ProtoMessage，不要用 dict/自定义类"
        )

    # ------------------------------------------------------------------
    # 解码
    # ------------------------------------------------------------------
    @classmethod
    def decode(cls, data: bytes, *, tolerate_truncation: bool = False) -> Self:
        """从 protobuf 字节还原成模型实例。

        :param tolerate_truncation: 数据被截断时是否宽容处理（默认严格抛错）。
            置 ``True`` 时「能解多少解多少」，缺的字段用默认值 ——
            用于解析**只剩前几百字节**的抓包预览（见
            :func:`models.protobuf_wire.iter_fields` 的说明）。

        :raises ProtobufWireError: 字节流不是合法 protobuf 时（严格模式下也含截断）。
        :raises models.base.PayloadError: 字段类型与模型不符时（由 pydantic 抛出）。

        三条设计决定：

        1. **未知编号只记日志、不抛异常**：服务端版本比客户端新是常态，
           因为多了个不认识的字段就让整包解析失败，属于自找麻烦；
        2. **字段缺失用默认值**：响应里省略空字段是正常行为；
        3. **日志用 debug 级**：正常运行时不该刷屏，排查时把日志级别调低即可看到。
        """
        _ensure_model_complete(cls)
        tags = cls.field_tag_map()
        inverse = {tag: name for name, tag in tags.items()}
        declared = cls.model_fields

        raw: dict[str, Any] = {}
        unknown: list[int] = []
        for number, fields in fields_by_number(
            data, stop_at_truncation=tolerate_truncation
        ).items():
            name = inverse.get(number)
            if name is None or name not in declared:
                unknown.append(number)
                continue
            raw[name] = cls._decode_field(
                number,
                fields,
                declared[name].annotation,
                name,
                tolerate_truncation=tolerate_truncation,
            )

        if unknown:
            _LOGGER.debug(
                "%s 忽略了 %d 个未知字段编号：%s（服务端可能比客户端新）",
                cls.proto_class_name,
                len(unknown),
                unknown,
            )
        return cls.model_validate(raw)

    @classmethod
    def _decode_field(
        cls,
        number: int,
        fields: list[Field],
        annotation: Any,
        name: str,
        *,
        tolerate_truncation: bool = False,
    ) -> Any:
        """按注解类型解码一个字段。

        :param number: 字段编号（packed 列表要靠它把多条记录聚合起来）。
        :param fields: 同一编号下的**全部**记录（repeated 字段会有多条）。
        :param tolerate_truncation: **必须往下传给嵌套消息**。
            否则会出现「外层说可以截断、内层却严格要求完整」的矛盾：
            例如区服列表里最后那个被截断的区服会让整条响应解析失败 ——
            而前面那些完整的区服正是我们要的。
        """
        item_type = list_item_type(annotation)
        if item_type is not None:
            target = unwrap_optional(item_type)
            if is_message_type(target):
                return [
                    target.decode(field.as_bytes(), tolerate_truncation=tolerate_truncation)
                    for field in fields
                    if field.wire_type == WIRE_LENGTH_DELIMITED
                ]
            if target is str:
                return [
                    field.as_string()
                    for field in fields
                    if field.wire_type == WIRE_LENGTH_DELIMITED
                ]
            if target is bytes:
                return [field.as_bytes() for field in fields]
            # 整数 / 布尔列表：packed 与非 packed 两种形态都要认
            return read_int_list({number: fields}, number)

        target = unwrap_optional(annotation)
        if is_message_type(target):
            for field in fields:
                if field.wire_type == WIRE_LENGTH_DELIMITED:
                    return target.decode(field.as_bytes(), tolerate_truncation=tolerate_truncation)
            return None
        if target is str:
            return fields[0].as_string()
        if target is bytes:
            return fields[0].as_bytes()
        if target is bool:
            return fields[0].as_bool()
        if target is int:
            return fields[0].as_int32()
        if target is float:
            for field in fields:
                if field.wire_type == WIRE_FIXED64:
                    return struct.unpack("<d", int(field.value).to_bytes(8, "little"))[0]
            return None

        # 前向引用未解析（ForwardRef）：说明模型存在"字符串注解 + 类定义在后"的
        # 写法且 rebuild 没能解析。绝不能在这里静默返回原始 bytes —— 那会把
        # 类型问题变成两层之外的 pydantic 校验错误（2026-10-03 GetGiftListRes
        # 的真实教训，见 models/gift_package.py 的 warning）。
        if isinstance(target, ForwardRef) or isinstance(annotation, ForwardRef):
            raise PayloadError(
                f"{cls.proto_class_name}.{name} 的注解是未解析的前向引用"
                f"（{annotation!r}）—— 请把被引用的类移到本类之前定义"
                "（项目惯例：元素类先定义、注解不加引号）"
            )

        # 其它类型（例如用 int 表示的 enum）：把原始值交给 pydantic 校验，
        # 由它给出「哪个字段、期望什么、实际是什么」的完整错误信息。
        return fields[0].value

    # ------------------------------------------------------------------
    # 调试
    # ------------------------------------------------------------------
    def describe(self) -> str:
        """把本对象**编码后**渲染成可读文本。

        这是与抓包内容对照时最顺手的一件工具：输出里既有字段名（来自字段表）
        又有实际值，一眼就能看出「我发的包」与「抓到的包」在哪一位开始不一致。
        """
        from models.proto_fields import describe_message

        return describe_message(self.proto_class_name, self.encode())

    def to_hex(self) -> str:
        """当前编码结果的十六进制字符串（用来与抓包字节做逐字节比对）。"""
        return self.encode().hex()

