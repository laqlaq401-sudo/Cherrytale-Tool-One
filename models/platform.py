"""平台网关的响应信封模型（结构来自**实测响应**，不是猜测）。

【实测记录】
对 ``GET /api/v2/user`` 与 ``GET /api/v2/user/games/status/27`` 发起请求
（未携带 Token）时，网关实际返回：

.. code-block:: text

    HTTP/1.1 400 Bad Request
    Content-Type: text/plain;charset=UTF-8

    {"status": "FAIL", "message": "驗證失敗", "errorCode": 4001, "data": null}

由此确认三件对实现影响很大的事：

1. **业务信封固定为四个字段**：``status`` / ``message`` / ``errorCode`` / ``data``。
2. **鉴权失败不是 401，而是 HTTP 400**（配合 ``errorCode: 4001``）。
   因此判断「令牌有没有问题」**绝不能只看 HTTP 状态码**，
   否则会把「令牌无效」误报成「参数错误」，排查方向直接跑偏。
3. 响应体的 ``Content-Type`` 标注为 ``text/plain``，**内容却是 JSON**。
   所以必须按内容解析，不能靠 Content-Type 判断，否则会误判为「非 JSON 响应」。

.. note::
   实测已拿到**成功**与**失败**两种信封（字段集合不同，属于联合结构）：

   - 成功：``{"status":"SUCCESS","data":{...},"serverTime":1789893660724}``
     —— **没有** ``errorCode`` / ``message``；
   - 失败：``{"status":"FAIL","message":"驗證失敗","errorCode":4001,"data":null}``
     —— **没有** ``serverTime``。

   因此这里把所有字段都设为可选：一种响应缺字段不应该导致解析失败。
   另外注意成功响应的 ``Content-Type`` 是 ``application/json``，
   而失败响应却是 ``text/plain``（内容仍是 JSON）——
   所以解析必须**按内容**判断，绝不能依赖 Content-Type。
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final

from pydantic import Field

from models.base import ProtocolModel

#: 表示「业务成功」的 ``status`` 取值。
#: 实测失败值为 ``"FAIL"``；成功值尚未实测，目前按最常见的 ``"SUCCESS"``/``"OK"``
#: 兼容处理。等确认后应删掉多余的那一个，避免「宽到把错误也放行」。
PLATFORM_SUCCESS_STATUSES: Final[frozenset[str]] = frozenset({"SUCCESS", "OK"})

#: 与**登录态**相关的业务错误码集合。
#: 4001 = 「驗證失敗」，实测于不携带 Token 请求时返回。
#: 若后续发现令牌过期等其它登录态错误码，直接加进本集合即可，
#: 客户端会自动把它们统一翻译成 AuthenticationError。
PLATFORM_AUTH_ERROR_CODES: Final[frozenset[int]] = frozenset({4001})


class PlatformEnvelope(ProtocolModel):
    """平台网关统一响应信封。

    ``errorCode`` 使用别名映射：Python 侧写 ``error_code``（符合 PEP 8 下划线风格），
    JSON 侧仍是服务端要求的 ``errorCode``。这正是 ``ProtocolModel`` 里
    ``populate_by_name=True`` 在起作用的场景。
    """

    status: str | None = None
    message: str | None = None
    error_code: int | None = Field(default=None, alias="errorCode")
    data: Any = None
    server_time: int | None = Field(default=None, alias="serverTime")

    # ------------------------------------------------------------------
    # 服务端时间
    # ------------------------------------------------------------------
    def server_time_utc(self) -> datetime | None:
        """把 ``serverTime`` 转成 UTC ``datetime``。

        :return: 转换结果；字段缺失或数值明显异常时返回 ``None``。

        **单位是毫秒**（实测值 ``1789893660724``，对应的秒级时间戳是 1789893660）。
        这个字段很实用：它可以用来校准本地时钟偏移 ——
        将来做需要时间戳签名的接口时，本地时间与服务器差太多会直接导致签名失效。
        """
        if self.server_time is None:
            return None
        try:
            return datetime.fromtimestamp(self.server_time / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            # 数值离谱时不要抛异常炸掉调用方，返回 None 让它自行决定
            return None

    def clock_offset_seconds(self, *, now: float | None = None) -> float | None:
        """本地时钟与服务器时间的偏差（秒，正值表示本地偏快）。

        :return: 偏差；``serverTime`` 缺失时返回 ``None``。

        用途：接口若要求「请求时间戳必须在服务端 ±N 分钟内」，先用本方法
        自查一下，能避免大量「明明签名算对了却被拒」的困惑。
        """
        server_dt = self.server_time_utc()
        if server_dt is None:
            return None
        reference = time.time() if now is None else now
        return reference - server_dt.timestamp()

    # ------------------------------------------------------------------
    # 判定
    # ------------------------------------------------------------------
    def is_success(self) -> bool:
        """是否业务成功。

        ``status`` 缺失时返回 ``False``（视为失败），而不是「当作成功」。
        理由：结构不认识说明我们对响应的理解有偏差，
        继续拿里面的数据往下跑只会把错误扩散得更远。
        """
        return (self.status or "").upper() in PLATFORM_SUCCESS_STATUSES

    def is_failure(self) -> bool:
        """是否业务失败。"""
        return not self.is_success()

    def is_auth_failure(self) -> bool:
        """是否属于「登录态问题」（令牌缺失/无效/过期）。"""
        return self.error_code in PLATFORM_AUTH_ERROR_CODES

    # ------------------------------------------------------------------
    # 展示
    # ------------------------------------------------------------------
    def error_message(self) -> str:
        """可读的错误描述（缺失时给出占位文本，避免日志里出现 ``None``）。"""
        if self.message:
            return self.message
        return "<服务端未提供描述>"

    def summary(self) -> str:
        """一行摘要，适合直接写进日志。"""
        if self.is_success():
            return "status=SUCCESS"
        code = "?" if self.error_code is None else self.error_code
        return f"status={self.status or '?'} errorCode={code} message={self.error_message()}"


# ---------------------------------------------------------------------------
# 各接口的 data 结构（字段与取值均来自实测响应）
# ---------------------------------------------------------------------------
class PlatformUser(ProtocolModel):
    """``GET /api/v2/user`` 响应中 ``data`` 字段的结构。

    实测原文：

    .. code-block:: json

        {"status": "SUCCESS",
         "data": {"userId": "ER00000000-0000-0000-0000-000000000000", "coins": 30, "guest": false},
         "serverTime": 1789893660724}

    .. note::
       Python 侧一律用 snake_case，通过 ``Field(alias=...)`` 映射服务端的
       camelCase 字段名 —— 这正是 ``ProtocolModel`` 中
       ``populate_by_name=True`` 的用武之地：两种写法都能赋值，
       而序列化出去（``model_dump(by_alias=True)``）时用服务端要的名字。
    """

    user_id: str = Field(alias="userId")
    coins: int = 0
    guest: bool = False


class GameBindingStatus(ProtocolModel):
    """``GET /api/v2/user/games/status/{game_id}`` 响应中 ``data`` 字段的结构。

    实测原文：

    .. code-block:: json

        {"status": "SUCCESS",
         "data": {"isBound": true, "gameAccount": "ER00000000-0000-0000-0000-000000000000"},
         "serverTime": 1789893660810}
    """

    is_bound: bool = Field(alias="isBound")
    game_account: str = Field(alias="gameAccount")


# ---------------------------------------------------------------------------
# 账号密码登录（POST /api/v2/account/login）
# ---------------------------------------------------------------------------
class PlatformLoginData(ProtocolModel):
    """``POST /api/v2/account/login`` 响应中 ``data`` 字段的结构。

    实测原文（2026-09-21，本机对 Cherrytale 账号真跑通过；字段名照抄服务端 camelCase）：

    .. code-block:: json

        {"status": "SUCCESS",
         "data": {"accessToken": "eyJ0eXAiOiJK…(730 字符)",
                  "refreshToken": "eyJ0eXAiOiJK…",
                  "userId": "ER00000000-0000-0000-0000-000000000000",
                  "accountStatus": "…"}}

    .. note::
       ``access_token`` **没有默认值**：它是这一步存在的唯一理由，缺了就说明
       我们对响应的理解有偏差，应该当场报错（由 ``PlatformClient._parse_data``
       翻译成 ``ProtocolError``），而不是带着空令牌继续往下走。

       ``account_status`` 的语义**尚未确认**（只观测到存在），因此声明为 ``Any``
       并保持不解释 —— 等有需要时再用实测去定义它。
    """

    access_token: str = Field(alias="accessToken")
    refresh_token: str = Field(default="", alias="refreshToken")
    user_id: str = Field(default="", alias="userId")
    account_status: Any = Field(default=None, alias="accountStatus")


@dataclass(frozen=True)
class PlatformToken:
    """一次平台登录（或续期）换来的令牌集合。

    :param access_token: 访问令牌（JWT，实测 730 字符、24 小时有效）。
    :param refresh_token: 续期令牌；``POST /api/v2/token/access`` 用它换新令牌。
    :param user_id: 平台用户 ID（形如 ``ER00000000-…``）—— 游戏登录 1001 的
        ``socialAccount`` 要的就是它。
    :param obtained_at: 取得时刻（Unix 秒），用于算"还有多久过期"。
    :param source: 来源标记：``credentials``（账号密码）/ ``refresh``（续期）。

    .. warning::
       本对象**绝不打印明文令牌**：用 :meth:`describe`（只留前缀与长度）。
    """

    access_token: str
    refresh_token: str = ""
    user_id: str = ""
    obtained_at: float = 0.0
    source: str = "credentials"

    def expires_at(self) -> float | None:
        """从 JWT 的 ``exp`` 声明读过期时刻（**不校验签名、不记录令牌内容**）。

        :return: Unix 秒；令牌不是 JWT 或缺 ``exp`` 时返回 ``None``。

        【为什么值得单独写这个方法】
        实测令牌 24 小时有效，而"什么时候该续期"正是自动化最容易踩的坑：
        跑到第 13 小时才失败一次，你会先怀疑协议变了。有了它就能提前续期。
        """
        return platform_token_expires_at(self.access_token)

    def expires_in_seconds(self, *, now: float | None = None) -> float | None:
        """距离过期还有多少秒；读不出 ``exp`` 时返回 ``None``。"""
        expires = self.expires_at()
        if expires is None:
            return None
        return expires - (time.time() if now is None else now)

    def describe(self) -> str:
        """一行摘要（**令牌打码**），可直接进日志/终端。"""
        parts = [
            f"userId={self.user_id or '<未知>'}",
            f"source={self.source}",
            f"accessToken={_mask_secret(self.access_token)}",
        ]
        remaining = self.expires_in_seconds()
        if remaining is not None:
            parts.append(f"剩余 {int(remaining // 60)} 分钟")
        if self.refresh_token:
            parts.append("有 refreshToken")
        return "，".join(parts)

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return f"PlatformToken({self.describe()})"


def _mask_secret(value: str, *, visible: int = 6) -> str:
    """把凭据打码成 ``eyJ0eX…(730 字符)``（保留前缀便于区分，不构成可用凭据）。"""
    if not value:
        return "<空>"
    return f"{value[:visible]}…({len(value)} 字符)"


def platform_token_claims(token: str) -> dict[str, Any]:
    """只读 JWT 的载荷声明（**不验证签名、不落日志**）。

    :param token: JWT 字符串（``header.payload.signature``）。
    :return: 载荷字典；不是 JWT / 解不出来时返回空字典。

    【为什么"只读"也要单列一个函数】
    登录后我们经常只想回答两个问题："它什么时候过期"、"它是谁的"。
    为此去引入一个 JWT 库（还要验签、还要处理算法白名单）并不划算 ——
    ``exp`` / ``user_id`` 在**未验签**的载荷里就能读到，而这两个值只用于**本地提示**，
    从不用于放行任何权限。真正的权威永远在服务端（它会拒绝过期令牌）。
    """
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, UnicodeDecodeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def platform_token_expires_at(token: str) -> float | None:
    """读 JWT 的 ``exp`` 声明（Unix 秒）；读不出来返回 ``None``。"""
    try:
        return float(platform_token_claims(token)["exp"])
    except (KeyError, TypeError, ValueError):
        return None


def platform_token_user_id(token: str) -> str:
    """读 JWT 的 ``user_id`` 声明（形如 ``ER00000000-…``）；读不出来返回空串。"""
    return str(platform_token_claims(token).get("user_id") or "")
