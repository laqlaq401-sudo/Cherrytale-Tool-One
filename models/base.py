"""协议数据模型的公共基类。

【为什么要用 Pydantic 而不是普通字典】
用字典收发数据时，字段名写错、类型不对都不会报错，直到服务端返回一句
「参数错误」你才发现——而且你根本不知道错在哪个字段。
Pydantic 模型会在**数据进入程序的那一刻**就检查类型与必填项，
并把错误精确指向具体字段，这对逆向调试的价值非常大。

⚠️ 本文件只提供**通用能力**（序列化、反序列化、调试打印）。
   具体报文（LoginReq / LoginRes 等）必须等 dump.cs 分析完成后再编写，
   详见 models/__init__.py 中的说明。
"""

from __future__ import annotations

import json
from typing import Any, ClassVar, Mapping, Self

from pydantic import BaseModel, ConfigDict, ValidationError


class PayloadError(ValueError):
    """把一段数据装进模型时失败（不是合法 JSON、字段类型不符、缺必填项等）。

    :param raw_preview: 出错数据的开头片段，便于你判断「收到的到底是什么」。

    与 ``client.exceptions.ProtocolError`` 的分工：

    - ``ProtocolError`` 属于**通信层**判断：「这个响应整体不符合预期」；
    - ``PayloadError`` 属于**数据层**判断：「这段数据塞不进这个模型」。
    """

    def __init__(self, message: str, raw_preview: str = "") -> None:
        self.raw_preview = raw_preview
        detail = f"\n原始数据开头：{raw_preview}" if raw_preview else ""
        super().__init__(f"{message}{detail}")


class ProtocolModel(BaseModel):
    """所有协议报文的基类。

    三项配置及其原因（都是逆向场景踩出来的经验）：

    - ``extra="ignore"``：**逆向时最关键的一条**。
      服务端随时可能上线新字段。若严格校验，你会因为「多了个不认识的字段」
      导致整个响应解析失败，排查方向会被彻底带偏。
    - ``populate_by_name=True``：允许同时用字段名或别名赋值。
      方便把 dump.cs 里的 C# 字段名（多为 PascalCase）映射到 Python 的 snake_case。
      例如 ``login_name: str = Field(alias="LoginName")``。
    - ``validate_assignment=False``：赋值时不重复校验。
      协议报文常常要「先构造、再逐步填字段」（签名往往最后才算），
      每步赋值都校验只会添乱。
    """

    model_config = ConfigDict(
        extra="ignore",
        populate_by_name=True,
        validate_assignment=False,
    )

    #: 需要在调试输出中打码的字段名。子类按需覆盖，例如：
    #: ``masked_fields: ClassVar[frozenset[str]] = frozenset({"token", "password"})``
    masked_fields: ClassVar[frozenset[str]] = frozenset()

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------
    def to_payload(self, *, exclude_none: bool = True) -> dict[str, Any]:
        """转成可直接发送的字典。

        :param exclude_none: 是否剔除值为 ``None`` 的字段。默认剔除——
            很多服务端会把「字段存在但为空」判为非法参数。
        """
        return self.model_dump(exclude_none=exclude_none)

    def to_json(self, *, ensure_ascii: bool = False) -> str:
        """转成 JSON 文本。

        :param ensure_ascii: 默认 ``False``，让中文按原样输出而不是 ``\\uXXXX``，
            便于你看日志时直接读懂内容（JSON 本身支持 UTF-8）。
        """
        return json.dumps(self.to_payload(), ensure_ascii=ensure_ascii)

    def to_json_bytes(self) -> bytes:
        """转成 UTF-8 编码的 JSON 字节，可直接作为 HTTP 请求体。"""
        return self.to_json().encode("utf-8")

    # ------------------------------------------------------------------
    # 反序列化
    # ------------------------------------------------------------------
    @classmethod
    def from_json(cls, raw: str | bytes | bytearray | Mapping[str, Any]) -> Self:
        """从 JSON 文本（或已是字典的数据）构造模型。

        :raises PayloadError: 不是合法 JSON，或字段类型与模型不符时。
            异常信息里会附带原始数据开头片段，方便你判断收到了什么。
        """
        if isinstance(raw, Mapping):
            data: Any = dict(raw)
        else:
            if isinstance(raw, (bytes, bytearray)):
                raw_text = bytes(raw).decode("utf-8", errors="replace")
            else:
                raw_text = raw
            try:
                data = json.loads(raw_text)
            except json.JSONDecodeError as exc:
                raise PayloadError(
                    f"不是合法的 JSON（{exc.msg}，位置 {exc.pos}）",
                    raw_preview=raw_text[:120],
                ) from exc

        try:
            return cls.model_validate(data)
        except ValidationError as exc:
            raise PayloadError(
                f"{cls.__name__} 字段校验失败：\n{exc}",
                raw_preview=str(data)[:120],
            ) from exc

    # ------------------------------------------------------------------
    # 便利方法
    # ------------------------------------------------------------------
    def with_updates(self, **changes: Any) -> Self:
        """返回一个「改过若干字段」的**副本**（原对象不变）。

        典型用法：算好签名后填进报文，而不是就地修改原对象，
        这样出问题时可以对比改前改后两份数据。
        """
        return self.model_copy(update=changes)

    def __str__(self) -> str:
        """生成便于阅读的调试文本，自动对 ``masked_fields`` 中的字段打码。

        .. note::
           这里用 ``type(self).model_fields`` 而不是 ``self.model_fields``：
           Pydantic 2.11 起，从**实例**访问 ``model_fields`` 已被标记为弃用，
           从类访问才是推荐写法（Pydantic 3.0 将移除前者）。
        """
        parts = []
        for name in sorted(type(self).model_fields):
            value = getattr(self, name, None)
            shown = "'***'" if name in self.masked_fields else repr(value)
            parts.append(f"{name}={shown}")
        return f"{type(self).__name__}({', '.join(parts)})"
