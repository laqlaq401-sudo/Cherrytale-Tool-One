"""HTTP Header 的拼装与脱敏。

【Header 到底是什么，为什么这么重要】
Header 是 HTTP 请求里「信封上的字」：数据内容之外的附加信息，
例如内容类型、客户端版本、登录凭据。

对游戏协议还原来说，Header 有三类，处理策略完全不同：

1. **固定的**（如 Content-Type）：所有请求都一样，可以写死在模板里；
2. **动态的**（如 Token、时间戳、签名）：每次请求都变，由代码在运行时生成；
3. **设备指纹类的**（如渠道号、机型、版本号）：服务端**可能**会校验，
   写错会直接报错或风控。

⚠️ 正因为第 3 类存在，本模块**刻意不预设任何「看起来很像真的」Header**。
   真实字段名与取值必须从 dump.cs 的 BestHTTP 调用链中还原，
   或通过抓包比对确认后再填入。目前 ``BASE_HEADERS`` 是空字典。
"""

from __future__ import annotations

from typing import Final, Mapping

#: 需要脱敏的 Header 字段名关键词（小写比较，子串匹配）。
#: 日志或报错信息里出现这些字段时，值必须被打成 ``***``，
#: 否则一次截图或一段日志就可能泄露你的账号凭据。
SENSITIVE_HEADER_KEYWORDS: Final[tuple[str, ...]] = (
    "token",
    "authorization",
    "cookie",
    "session",
    "sign",
    "secret",
    "passwd",
    "password",
)

#: 基础 Header 模板。
#: TODO(protocol): 待确认。需要分析 dump.cs 中 BestHTTP 的请求构造过程，
#:     或抓包比对，才能填入真实的固定字段（版本号 / 渠道 / 设备信息等）。
BASE_HEADERS: Final[dict[str, str]] = {}


def build_headers(
    *,
    content_type: str | None = None,
    token: str | None = None,
    token_field: str | None = None,
    extra: Mapping[str, str] | None = None,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """拼装一次请求所需的 Header。

    :param content_type: 内容类型，例如 ``"application/json"``。
        protobuf 报文通常用 ``"application/octet-stream"``，但**要以实际抓包为准**。
    :param token: 登录后拿到的令牌。
    :param token_field: 令牌对应的 Header 字段名。**必须与 token 同时提供**，
        因为字段名究竟是 ``Authorization`` 还是 ``token`` 必须从协议确认，
        不能由本模块臆测。
    :param extra: 额外或需要覆盖的字段（优先级最高）。
    :param base: 基础模板，默认使用 :data:`BASE_HEADERS`。
    :return: 组装好的 Header 字典，值为空字符串或 ``None`` 的字段会被丢弃。
    :raises ValueError: 只给了 token 却没给 token_field（或反之）。

    **优先级**：``extra`` > ``base``，即 ``extra`` 会覆盖同名的基础字段。
    """
    if (token is None) != (token_field is None):
        raise ValueError(
            "token 与 token_field 必须同时提供：\n"
            "  - token_field 是协议里约定的 Header 字段名（如 Authorization / token）；\n"
            "  - 该字段名必须从 dump.cs 或抓包确认，不能由代码猜测。"
        )

    template: dict[str, str] = dict(BASE_HEADERS if base is None else base)

    if content_type is not None:
        template["Content-Type"] = content_type
    if token is not None and token_field is not None:
        template[token_field] = token
    if extra:
        template.update(extra)

    # 丢弃空值：空字符串的 Header 在部分服务端会被判为非法请求
    return {key: value for key, value in template.items() if value}


def is_sensitive_header(field_name: str) -> bool:
    """判断某个 Header 字段是否属于敏感字段（用于决定是否脱敏）。"""
    lowered = field_name.lower()
    return any(keyword in lowered for keyword in SENSITIVE_HEADER_KEYWORDS)


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """生成一份可安全打印的 Header 副本：敏感字段的值替换为 ``***``。

    用法：记录调试日志或把请求信息写进报错消息时，**务必先经过本函数**。

    注意：这里保留字段名，只隐藏值。字段名本身通常不敏感，
    而保留它能让你排查时清楚「这个 Header 到底发了没有」。
    """
    return {
        key: ("***" if is_sensitive_header(key) else value)
        for key, value in headers.items()
    }
