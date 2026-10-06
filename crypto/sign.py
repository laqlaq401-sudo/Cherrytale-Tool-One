"""签名拼装与 Token 封装。

【本模块的诚实声明】
游戏协议里「参数怎么排序、盐(salt)是什么、拼出来长什么样」属于**业务规则**，
必须从 dump.cs 里对应的 C# 实现反推出来，**不能靠猜**。

所以本模块严格分成两半：

- **可以确定的部分（已实现）**：参数字典 → 待签名串的通用拼装、通用加盐哈希、
  恒定时间校验、Token 保存与过期判断。这些逻辑在任何项目里都一样，可以放心用。
- **必须待确认的部分（占位）**：``SIGN_SALT`` 与 ``sign_request()``。
  它们会抛出带 TODO 说明的异常，避免你在规则未确认时误发请求、污染账号数据。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Collection, Final, Mapping

from crypto.hash import (
    BytesLike,
    HashAlgorithm,
    constant_time_compare,
    hash_hex,
    to_bytes,
)

#: TODO(protocol): 签名盐值尚未确认！
#: 需要先在 dump.cs 中定位签名相关方法（建议关键词见 :func:`sign_request`），
#: 确认后再填入。在此之前保持 ``None``。
SIGN_SALT: Final[str | None] = None

#: TODO(protocol): 签名算法尚未确认。常见为 md5，也可能是 sha1 / sha256。
DEFAULT_SIGN_ALGORITHM: Final[HashAlgorithm] = "md5"


class SignError(RuntimeError):
    """签名规则未确认、或参数不合法时抛出。"""


def _stringify(value: object) -> str:
    """把参数值转成参与签名的字符串。

    三条约定（都是为了和 C# / JSON 的常见写法对齐）：

    - ``None`` → 空字符串，表示「该字段无值」；
    - ``bool`` → ``"true"`` / ``"false"``（不是 Python 的 ``True`` / ``False``），
      因为 C# 与 JSON 序列化出来都是小写；
    - 其它类型 → ``str()``。

    ⚠️ 浮点数要特别小心：``str(1.0)`` 得到 ``"1.0"``，而 C# 的 ``ToString()``
    可能给出 ``"1"``。等确认协议里的数值格式后，这里可能需要按需调整。
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def build_sign_base_string(
    params: Mapping[str, object],
    *,
    exclude: Collection[str] = (),
    sort_keys: bool = True,
    pair_separator: str = "&",
    kv_separator: str = "=",
    skip_empty: bool = False,
) -> str:
    """把参数字典拼成「待签名串」。

    :param params: 请求参数字典。
    :param exclude: 要排除的字段名集合。**务必把自己即将算出的签名字段排除掉**，
        否则就成了「用签名算签名」的死循环。
    :param sort_keys: 是否按键名升序排列。绝大多数服务端按字典序处理，
        但仍需以 dump 中的实现为准。
    :param pair_separator: 键值对之间的分隔符，默认 ``&``。
    :param kv_separator: 键与值之间的分隔符，默认 ``=``。
    :param skip_empty: 是否跳过值为 ``None`` 或空字符串的字段。
    :return: 形如 ``"a=1&b=2&c="`` 的字符串。

    :raises SignError: 参数字典为空时（空字典算出来的签名没有意义，属于调用方错误）。
    """
    if not params:
        raise SignError("参与签名的参数字典不能为空")

    excluded = set(exclude)
    items = [
        (key, _stringify(value)) for key, value in params.items() if key not in excluded
    ]
    if skip_empty:
        items = [(key, value) for key, value in items if value != ""]

    if sort_keys:
        items.sort(key=lambda item: item[0])

    return pair_separator.join(f"{key}{kv_separator}{value}" for key, value in items)


def sign_with_salt(
    payload: BytesLike,
    salt: BytesLike,
    algorithm: HashAlgorithm = DEFAULT_SIGN_ALGORITHM,
) -> str:
    """通用「拼接后再哈希」签名：``hash(payload + salt)``。

    这是最常见的签名形式，但**顺序也可能反过来**（``salt + payload``）。
    等看清 dump 中的实现再决定是否需要调整，切勿凭感觉改。
    """
    combined = to_bytes(payload) + to_bytes(salt)
    return hash_hex(combined, algorithm)


def verify_signature(
    params: Mapping[str, object],
    received_sign: str,
    salt: BytesLike,
    *,
    sign_field: str = "sign",
    algorithm: HashAlgorithm = DEFAULT_SIGN_ALGORITHM,
) -> bool:
    """用同样规则重算签名，并与收到的签名做**恒定时间**比较。

    :param received_sign: 报文中携带的签名值。
    :param sign_field: 签名自身的字段名，会自动从参数字典中排除。
    :return: 一致返回 ``True``。
    """
    base = build_sign_base_string(params, exclude={sign_field})
    expected = sign_with_salt(base, salt, algorithm)
    return constant_time_compare(expected, received_sign)


def sign_request(params: Mapping[str, object]) -> str:
    """为真实请求计算签名。

    :raises SignError: 目前**必然抛出**，因为签名规则尚未从 dump.cs 确认。

    为什么宁可抛异常，也不给个「差不多的实现」？
    签名错一个字符服务端就会拒绝；更糟的是，错误的实现会让你误以为
    「逻辑已经写好了」，从而在错误方向上浪费大量排查时间。
    """
    if SIGN_SALT is None:
        raise SignError(
            "签名规则尚未确认（crypto.sign.SIGN_SALT 仍为 None）。\n"
            "下一步应在 dump.cs 中定位签名方法，建议检索：\n"
            "    bash tools/search_dump.sh 'AppSecret|Salt|CalcSign|GetSign' -C 25 -i\n"
            "确认盐值与拼接顺序后，再实现本函数并补上单元测试。"
        )
    raise SignError("签名实现尚未落地：请先在 dump.cs 中确认真实规则再补全本函数。")


# ---------------------------------------------------------------------------
# Token 管理
# ---------------------------------------------------------------------------
@dataclass
class TokenStore:
    """登录后拿到的会话令牌容器。

    设计要点：

    - ``__repr__`` 会**给 Token 打码**，避免调试时把凭据打印进日志或被截图带走；
    - 过期时间为 Unix 时间戳（秒），由调用方从登录响应里解析后填入；
      若服务端不下发过期时间，保持 ``None`` 表示「不主动判断过期」。
    """

    uid: str = ""
    token: str = ""
    expires_at: float | None = None
    extra: dict[str, str] = field(default_factory=dict)

    def is_expired(self, *, now: float | None = None) -> bool:
        """令牌是否已过期。未设置过期时间时返回 ``False``（即不主动作废）。"""
        if self.expires_at is None:
            return False
        current = time.time() if now is None else now
        return current >= self.expires_at

    def is_valid(self, *, now: float | None = None) -> bool:
        """是否具备可用令牌（非空且未过期）。"""
        return bool(self.token) and not self.is_expired(now=now)

    def update(self, *, uid: str, token: str, expires_in: float | None = None) -> None:
        """写入或刷新令牌。

        :param expires_in: **剩余有效秒数**。传 ``None`` 表示不记录过期时间。
        """
        self.uid = uid
        self.token = token
        self.expires_at = None if expires_in is None else time.time() + expires_in

    def clear(self) -> None:
        """清空令牌（退出登录或令牌失效时调用）。"""
        self.uid = ""
        self.token = ""
        self.expires_at = None

    def as_header(self, field_name: str) -> dict[str, str]:
        """按指定字段名生成请求头。

        :param field_name: 请求头字段名。**刻意要求调用方显式传入**，
            因为游戏用的是 ``Authorization``、``token`` 还是 ``X-Token``
            必须从 dump.cs 确认，不能由我们臆测。
        :raises SignError: 当前没有可用令牌时。
        """
        if not self.token:
            raise SignError("当前没有可用令牌，请先完成登录流程")
        return {field_name: self.token}

    def __repr__(self) -> str:
        """调试友好且**不泄露凭据**的表示形式。"""
        masked = f"{self.token[:4]}***" if self.token else "<空>"
        expire_text = "None" if self.expires_at is None else f"{self.expires_at:.0f}"
        return (
            f"TokenStore(uid={self.uid!r}, token={masked!r}, "
            f"expires_at={expire_text}, extra_keys={sorted(self.extra)})"
        )

