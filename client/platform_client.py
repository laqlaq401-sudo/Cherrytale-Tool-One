"""平台认证网关客户端（sadpki-portal-v2）。

【这一层解决什么问题】
游戏入口是「平台账号体系」：先用 Token 找网关确认「你是谁」以及
「你绑定过哪些游戏」，拿到这些信息之后才谈得上进游戏、跑业务接口。

本项目用到的两个接口（都由本文件封装）：

    GET /api/v2/user                      查询用户信息
    GET /api/v2/user/games/status/{id}    查询游戏绑定状态（默认 id=27）

【为什么直接复用 GameSession 而不是自己写 requests 调用】
网络层的琐碎细节（超时、重试、代理、异常翻译、日志脱敏）已经在
``client/session.py`` 里做过并写过测试。这里若另起炉灶，就会出现两套策略：
改了一处忘了另一处，最终表现是「有的接口会重试、有的不会」这种极难排查的问题。

【鉴权说明】
Token 的取值不在代码里硬编码，而是由 ``config.resolve_auth_token()`` 按
「环境变量 → config_local.py → .auth_token 文件」的顺序解析。
Header 字段名与包装格式（``Bearer xxx`` 还是裸 token）也都放在 config 里，
一旦抓包确认了真实格式，改配置即可，无需改本文件。
"""

from __future__ import annotations

import base64
import logging
import time
from pathlib import Path
from typing import Any, Final, Mapping, TypeVar

import requests
from pydantic import ValidationError

import config
from client.exceptions import ApiError, AuthenticationError, ProtocolError
from client.headers import redact_headers
from client.session import GameSession, describe_response, raise_for_status
from models.base import PayloadError, ProtocolModel
from models.common import parse_json_payload
from models.platform import (
    GameBindingStatus,
    PlatformEnvelope,
    PlatformLoginData,
    PlatformToken,
    PlatformUser,
    platform_token_user_id,
)

_LOGGER: Final[logging.Logger] = logging.getLogger("client.platform")

#: 供 :meth:`PlatformClient._parse_data` 使用：约束返回值与传入的模型类型一致，
#: 这样调用 ``get_user_info()`` 时静态检查就知道拿到的是 ``PlatformUser``。
_ModelT = TypeVar("_ModelT", bound=ProtocolModel)

#: 密码编码方式 → 实现。实测门户前端是 ``btoa(明文)``，即 base64（**不是** md5）。
_PASSWORD_ENCODERS: Final[dict[str, Any]] = {
    "base64": lambda raw: base64.b64encode(raw.encode("utf-8")).decode("ascii"),
    "plain": lambda raw: raw,
}


def encode_password(password: str, *, encoding: str | None = None) -> str:
    """按配置把明文密码编码成请求体里的取值。

    :param password: 明文密码（只在内存里停留，绝不写日志、绝不落盘）。
    :param encoding: 覆盖编码方式；``None`` 表示用
        ``config.PLATFORM_LOGIN_PASSWORD_ENCODING``。
    :return: 编码后的字符串。
    :raises ValueError: 配置了未知编码方式时 —— **宁可当场报错**，
        也不要带着一个错的密码去敲服务器（那会白白计一次失败登录）。

    .. note::
       ``btoa`` 遇到非 ASCII 字符会抛异常；这里统一走 UTF-8 编码，
       对 ASCII 与它逐字节等价、对非 ASCII 反而更稳。
    """
    resolved = (encoding or config.PLATFORM_LOGIN_PASSWORD_ENCODING).strip().lower()
    encoder = _PASSWORD_ENCODERS.get(resolved)
    if encoder is None:
        raise ValueError(
            f"未知的密码编码方式 {resolved!r}；可选：{sorted(_PASSWORD_ENCODERS)}"
        )
    return str(encoder(password))


def save_access_token(token: PlatformToken, *, path: Path | None = None) -> Path:
    """把 access token 写进 ``.auth_token``（**可选操作**，默认不调用）。

    :param token: 登录或续期拿到的令牌。
    :param path: 目标文件；``None`` 表示 ``config.AUTH_TOKEN_FILE``。
    :return: 实际写入的路径。

    【为什么值得有这个函数】
    ``config.resolve_auth_token()`` 的优先级是「环境变量 → config_local.py → 该文件」，
    写进去之后**整条既有链路立刻可用**（例如 ``python main.py run login`` 需要平台
    令牌才能取到 userId）。

    .. warning::
       文件里是**明文令牌**（已在 ``.gitignore`` 排除）。是否写入由调用方决定
       （脚本里要显式 ``--save``），否则"跑一次测试"就会把你在用的令牌覆盖掉。
    """
    target = Path(path) if path is not None else config.AUTH_TOKEN_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    target.write_text(
        f"# 由 Cherrytale tool 写入于 {stamp}"
        f"（source={token.source}，userId={token.user_id or '<未知>'}）\n"
        f"{token.access_token}\n",
        encoding="utf-8",
    )
    return target


class PlatformClient:
    """平台认证网关客户端。

    :param base_url: 网关根地址，默认取 ``config.PLATFORM_BASE_URL``。
    :param token: 鉴权 Token。传 ``None`` 表示使用 ``config.AUTH_TOKEN``
        （即按环境变量/本地文件的优先级解析）。
    :param session: 复用已有会话。传入时会**就地补上**平台所需的默认 Header，
        而不是重建 Session（重建会丢掉连接池）。
    :param timeout: 单次请求超时秒数，``None`` 表示用 config 的值。
    :param verify_tls: 是否校验 TLS 证书，抓包调试时设 ``False``。
    :param proxy: 代理地址，例如 ``"http://127.0.0.1:8888"``。
    :param logger: 自定义日志器。
    """

    def __init__(
        self,
        base_url: str | None = None,
        *,
        token: str | None = None,
        session: GameSession | None = None,
        timeout: float | None = None,
        verify_tls: bool | None = None,
        proxy: str | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.base_url = base_url or config.PLATFORM_BASE_URL
        self.token = config.AUTH_TOKEN if token is None else token
        self.logger = logger or _LOGGER

        # WebView 身份头（User-Agent / sec-ch-ua / Origin / Referer / lang / deviceId）
        # + 鉴权 Header；详见 config.platform_headers() 的说明
        headers = config.platform_headers()
        if token is not None:
            # 显式传入的 token 优先级最高，覆盖配置解析出来的那一个
            headers.update(config.auth_headers(token))

        if session is not None:
            self._session = session
            self._session.update_headers(headers)
        else:
            self._session = GameSession(
                base_url=self.base_url,
                headers=headers,
                timeout=timeout,
                verify_tls=verify_tls,
                proxy=proxy,
                logger=self.logger,
            )

    # ------------------------------------------------------------------
    # Token 与 Header 维护
    # ------------------------------------------------------------------
    def set_token(self, token: str) -> None:
        """更新鉴权 Token（例如重新登录后拿到新令牌）。

        :param token: 新令牌。传空字符串表示**摘掉**鉴权头，
            用于退出登录或令牌失效后的清理。
        """
        self.token = token
        value = config.format_auth_value(token) if token else None
        self._session.update_headers({config.AUTH_HEADER_NAME: value})
        self.logger.debug("平台 Token 已更新（长度=%d）", len(token))

    def headers_preview(self) -> dict[str, str]:
        """返回**脱敏后**的请求 Header，供诊断/打印使用。

        为什么要脱敏：Token 一旦被打印到屏幕或写进日志文件，就等于泄露；
        而保留字段名又能让你确认「这个 Header 到底发了没有」。
        """
        return redact_headers(config.platform_headers(include_auth=False)) | redact_headers(
            config.auth_headers(self.token)
        )

    def describe(self) -> str:
        """一行可读描述，用于日志与命令行输出（不含任何凭据）。"""
        token_state = "已配置" if self.token else "未配置"
        return f"PlatformClient(base_url={self.base_url}, token={token_state})"

    def __repr__(self) -> str:
        return self.describe()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def close(self) -> None:
        """关闭底层连接池。"""
        self._session.close()

    def __enter__(self) -> "PlatformClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 底层请求
    # ------------------------------------------------------------------
    def fetch(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> requests.Response:
        """发出一次 GET 并返回**原始响应**（不论状态码，不抛业务异常）。

        为什么需要一个「不抛业务异常」的方法？
        诊断连通性时，401/403 的响应体里往往就写着真实原因（例如
        ``{"code":401,"msg":"invalid token"}``）。若走抛异常的版本，
        这些内容会被压进异常消息里，不便于完整查看与比对。

        :raises client.exceptions.NetworkError: 网络层失败（超时 / DNS / 连接）。
            注意：这类失败意味着「根本没拿到响应」，与「服务端明确拒绝了请求」
            是两回事，必须区分开，否则你会把 Token 问题误判成网络问题。

        .. note::
           本方法**不**调用 ``raise_for_status``，因此 401/403/500 都会正常返回，
           由调用方自行判断。需要「失败即抛异常」时请用 :meth:`get_json`。
        """
        self.logger.debug("→ GET %s%s", self.base_url, path)
        response = self._session.get(path, params=params)
        self.logger.debug("← %s", describe_response(response))
        return response

    def get_json(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        """GET 并解析 JSON；HTTP 错误状态会转成本项目异常。

        :raises AuthenticationError: 401/403（令牌缺失或失效）。
        :raises ApiError: 其它 4xx / 5xx。
        :raises ProtocolError: 响应不是合法 JSON。

        .. note::
           重试策略继承自 ``GameSession``：默认只重试 GET。
           本网关的两个接口都是查询类、幂等，因此重试是安全的。
        """
        response = self.fetch(path, params=params)
        raise_for_status(response)
        return self._parse_json(response)

    def get_envelope(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> PlatformEnvelope:
        """请求并解析为**平台业务信封**；业务失败时抛异常。

        与 :meth:`get_json` 的区别：本方法理解平台的业务约定 ——
        **即使 HTTP 状态码是 200，只要 ``status`` 不是成功值也会抛异常**。

        为什么必须这样？因为网关完全可能用 ``200 + {"status":"FAIL"}``
        表示业务失败。若只看 HTTP 状态码，你会把失败当成功，
        然后拿着 ``data: null`` 继续往下跑，最后在一个完全不相关的地方报错。

        :raises AuthenticationError: 业务错误码属于登录态问题（如 4001 驗證失敗）。
        :raises ApiError: 其它业务失败。
        :raises ProtocolError: 响应不是预期的信封结构。
        """
        response = self.fetch(path, params=params)
        return self._parse_envelope(response)

    @staticmethod
    def _parse_envelope(response: requests.Response) -> PlatformEnvelope:
        """解析信封并翻译错误。

        **判定优先级（顺序很关键，写反了就会给出误导性的异常）**

        1. 先尝试解析响应体为 JSON 信封；
        2. 若 HTTP 状态是 4xx/5xx **且**信封里带有 ``status``/``errorCode``
           → 用**信封**里的业务错误码判定（这才是真实原因）；
        3. 若 HTTP 状态是 4xx/5xx 但响应体不是信封（例如网关返回 HTML 错误页）
           → 退回用 HTTP 状态码判定；
        4. 若 HTTP 正常，则**必须**解析出信封，否则报 ProtocolError。

        .. note::
           **为什么第 2 步要排在 HTTP 状态之前？** 因为本网关实测会用
           ``HTTP 400`` 承载业务信息：响应体里写着 ``errorCode 4001（驗證失敗）``。
           如果先按 HTTP 状态抛异常，就只能得出「400 = 参数错误」这个错误结论，
           你会去反复检查请求参数，而真正的原因是令牌无效。
           这一点是单元测试抓出来的 —— 它正是本文件里最值得保留的回归用例。
        """
        payload: Any = None
        body_is_json = False
        try:
            payload = PlatformClient._parse_json(response)
            body_is_json = True
        except ProtocolError:
            # 响应体不是 JSON（常见于网关拦截后返回的 HTML 错误页）
            body_is_json = False

        envelope: PlatformEnvelope | None = None
        if body_is_json and isinstance(payload, Mapping):
            try:
                envelope = PlatformEnvelope.model_validate(dict(payload))
            except ValidationError:
                envelope = None

        # ---- HTTP 错误状态 ----
        if response.status_code >= 400:
            if envelope is not None and (
                envelope.error_code is not None or envelope.status
            ):
                # 信封里有真实的业务原因，以它为准
                PlatformClient._raise_for_business_error(envelope)
            # 信封不可用，只能依据 HTTP 状态码给出结论
            raise_for_status(response)
            return envelope if envelope is not None else PlatformEnvelope()

        # ---- HTTP 正常：成功与否必须由信封决定 ----
        if not body_is_json:
            raise ProtocolError(
                f"{response.url} 返回的内容不是合法 JSON，"
                "无法解析平台业务信封。常见原因：① 被网关拦截返回 HTML 错误页；"
                "② 请求打到了错误的地址（返回了静态资源）。"
            )

        if envelope is None:
            raise ProtocolError(
                f"{response.url} 期望返回 JSON 对象，实际是 "
                f"{type(payload).__name__}：{str(payload)[:120]}"
            )

        PlatformClient._raise_for_business_error(envelope)
        return envelope

    @staticmethod
    def _raise_for_business_error(envelope: PlatformEnvelope) -> None:
        """把信封里的**业务失败**翻译成本项目异常。

        这里是「只看 HTTP 状态码」这一常见错误的防线：
        网关实测用 ``HTTP 400 + errorCode 4001`` 表示「驗證失敗」，
        因此必须读懂 ``errorCode`` 才能给出正确的异常类型。
        """
        if envelope.is_success():
            return

        code = envelope.error_code

        if envelope.is_auth_failure():
            # 登录态问题：调用方应该去重新登录，而不是重试请求
            raise AuthenticationError(
                code if code is not None else 401, envelope.error_message()
            )

        raise ApiError(code if code is not None else -1, envelope.error_message())

    @staticmethod
    def _parse_json(response: requests.Response) -> Any:
        """把响应体解析为 JSON，并把数据层异常翻译成通信层的 ProtocolError。"""
        body = response.content or b""
        try:
            return parse_json_payload(body)
        except PayloadError as exc:
            raise ProtocolError(
                f"{response.url} 返回的内容不是合法 JSON：\n  {exc}\n"
                "常见原因：① 请求被网关/防火墙拦截，返回了 HTML 错误页；"
                "② 该接口实际返回二进制（protobuf），而不是 JSON。"
            ) from exc

    @staticmethod
    def _ensure_mapping(data: Any, path: str) -> dict[str, Any]:
        """确认解析结果是一个 JSON 对象（而不是数组或裸标量）。"""
        if isinstance(data, Mapping):
            return dict(data)
        raise ProtocolError(
            f"{path} 期望返回 JSON 对象，实际是 {type(data).__name__}："
            f"{str(data)[:120]}"
        )

    # ------------------------------------------------------------------
    # 业务接口
    # ------------------------------------------------------------------
    def get_user_info(self) -> PlatformUser:
        """``GET /api/v2/user`` —— 查询当前 Token 对应的用户信息。

        :return: :class:`PlatformUser`；字段映射见 models/platform.py，
            结构**全部来自实测响应**（HTTP 200 原文）：
            ``{"status":"SUCCESS","data":{"userId":...,"coins":30,"guest":false},
            "serverTime":...}``
        :raises AuthenticationError: 令牌缺失/无效（实测为 HTTP 400 + ``errorCode 4001``）。
        :raises ApiError: 其它业务失败。
        :raises ProtocolError: ``data`` 缺失，或结构与模型不符。
        """
        envelope = self.get_envelope(config.USER_INFO_PATH)
        return self._parse_data(envelope, PlatformUser, config.USER_INFO_PATH)

    def get_game_status(self, game_id: int | None = None) -> GameBindingStatus:
        """``GET /api/v2/user/games/status/{game_id}`` —— 查询游戏绑定状态。

        :param game_id: 游戏 ID，默认 ``config.DEFAULT_GAME_ID``（27）。
        :return: :class:`GameBindingStatus`；实测原文为
            ``{"status":"SUCCESS","data":{"isBound":true,"gameAccount":"..."},
            "serverTime":...}``
        """
        path = config.game_status_path(game_id)
        return self._parse_data(self.get_envelope(path), GameBindingStatus, path)

    @staticmethod
    def _parse_data(
        envelope: PlatformEnvelope,
        model: type[_ModelT],
        path: str,
    ) -> _ModelT:
        """把信封里的 ``data`` 校验成指定模型。

        **为什么需要这一步**：``PlatformEnvelope.data`` 的类型是 ``Any``
        （成功与失败两种信封共用同一个模型），真正的结构约束直到这里才施加。
        在这里收口的价值是把「结构不符」的错误**当场**暴露，
        而不是让它漂到几层调用之后以奇怪的形式炸出来。
        """
        if envelope.data is None:
            raise ProtocolError(
                f"{path} 返回成功但 data 为 null，无法解析为 {model.__name__}。\n"
                "这通常说明我们对成功响应结构的理解有偏差，请核对实测响应后"
                "更新 models/platform.py。"
            )

        data = PlatformClient._ensure_mapping(
            envelope.data, f"{path} 的 data 字段"
        )
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            raise ProtocolError(
                f"{path} 的 data 结构与 {model.__name__} 不符：\n{exc}\n"
                "请核对实测响应并更新 models/platform.py。"
            ) from exc

    # ------------------------------------------------------------------
    # POST（与上面的 GET 系列对称）
    # ------------------------------------------------------------------
    def post_envelope(
        self,
        path: str,
        payload: Mapping[str, Any] | None = None,
    ) -> PlatformEnvelope:
        """POST JSON 并解析成平台业务信封（与 :meth:`get_envelope` 对称）。

        :param path: 相对路径（例如 ``/api/v2/system/config``）。
        :param payload: JSON 请求体；``None`` 表示**空 body**（续期接口就是这样）。
        :return: 平台业务信封。
        :raises AuthenticationError: 业务错误码属于登录态问题（如 4001）。
        :raises ApiError: 其它业务失败。
        :raises ProtocolError: 响应不是预期的信封结构。

        .. note::
           与 GET 共用 :meth:`_parse_envelope`，因此保留那条**实测判定**：
           ``HTTP 400 + errorCode 4001`` 才算"令牌无效"，不能只看 HTTP 状态码。
           另外 POST **不会自动重试**（``GameSession`` 的默认值）——
           这类请求多带副作用，重发可能重复建会话、也会让风控计数变难看。
        """
        return self._post_on(self._session, path, payload)

    def _post_on(
        self,
        session: GameSession,
        path: str,
        payload: Mapping[str, Any] | None,
    ) -> PlatformEnvelope:
        """用**指定会话**发一次 POST 并解析信封。

        单独抽出来是因为登录/续期流程要换超时、换 ``Authorization``（见
        :meth:`_flow_session`），不能直接用 ``self._session``。
        """
        prepared = session.prepare_request(
            "POST", path, json_body=dict(payload) if payload is not None else None
        )
        self.logger.debug("→ POST %s%s（body 已省略）", self.base_url, path)
        response = session.send(prepared)
        self.logger.debug("← %s", describe_response(response))
        return self._parse_envelope(response)

    def _flow_session(self, timeout: float, *, bearer: str = "Bearer") -> GameSession:
        """为"独立流程"临时开一个会话（登录 60s / 续期 30s）。

        :param timeout: 本次流程的超时（秒）。
        :param bearer: ``Authorization`` 头的取值。默认是**空的** ``Bearer`` ——
            抓包实测登录与验证码预检都带这个空值（不带旧令牌）。
        :return: 新建的会话（调用方用 ``with`` 关闭）。

        【为什么不复用 self._session】
        1. ``GameSession`` 的超时是**会话级**的（``prepare_request`` 没有
           per-request timeout），而这几步要求的超时差别很大；
        2. 也不该为了登录把整个客户端的默认超时改掉 —— 之后还要用它调只读接口。

        这里刻意复制了代理/TLS 设置，保证与主会话行为一致。
        """
        headers = config.platform_headers(include_auth=False)
        headers[config.AUTH_HEADER_NAME] = bearer
        return GameSession(
            base_url=self.base_url,
            headers=headers,
            timeout=timeout,
            verify_tls=self._session.verify_tls,
            proxy=self._session.proxy,
            logger=self.logger,
        )

    # ------------------------------------------------------------------
    # 账号密码登录（EROLABS V2 门户）
    # ------------------------------------------------------------------
    def login_by_credentials(
        self,
        account: str,
        password: str,
        *,
        game_id: int | None = None,
    ) -> PlatformToken:
        """用**账号密码**换取临时 access token（含验证码预检）。

        :param account: 平台账号（实测为邮箱形式）。
        :param password: 明文密码。**只在本方法内使用**：立刻 base64 编码进请求体，
            不写日志、不落盘、不进异常信息。
        :param game_id: 游戏 ID；``None`` 表示用 ``config.PLATFORM_LOGIN_GAME_ID``
            （实测 Cherrytale 为 ``27``）。
        :return: :class:`PlatformToken`（accessToken / refreshToken / userId）。
        :raises AuthenticationError: 账号或密码为空时（**一个字节都不会发**）。
        :raises ApiError: 服务端业务失败（``4006`` 传参错误 / ``4139`` 验证码失败…）。
        :raises ProtocolError: 响应结构不符（例如预检没给出 hashCode）。
        :raises NetworkError: 网络层失败（超时 / DNS / TLS）。

        【两步流程（本机实测通过，2026-09-21）】

        .. code-block:: text

            POST /api/v2/captcha/verify  {"captcha": [0, 0, 0, 1, 0]}   → data.hashCode
            POST /api/v2/account/login   {account, password(base64), gameId, hashCode}
                                        → data.accessToken / refreshToken / userId

        验证码那一步**不需要解滑块**（见 ``config`` 里对应常量的说明）。

        【两种失败会被明确定性的情形】
        - ``4006 傳入參數錯誤`` → ``hashCode`` 缺失或形状不对；
        - ``4139 Captcha 驗證失敗`` → 服务端开始真校验验证码（上游配方失效）。
          两者都由 ``ApiError`` 带出服务端原文，不用靠猜。
        """
        account = (account or "").strip()
        if not account or not password:
            raise AuthenticationError(401, "账号与密码都不能为空（未发出任何请求）")
        resolved_game_id = (
            config.PLATFORM_LOGIN_GAME_ID if game_id is None else int(game_id)
        )

        with self._flow_session(config.PLATFORM_LOGIN_TIMEOUT) as session:
            # ① 验证码预检：固定数组换 hashCode
            captcha = self._post_on(
                session,
                config.PLATFORM_LOGIN_CAPTCHA_PATH,
                config.PLATFORM_LOGIN_CAPTCHA_BODY,
            )
            hash_code = self._extract_hash_code(captcha)

            # ② 真正的登录
            payload: dict[str, Any] = {
                config.PLATFORM_LOGIN_ACCOUNT_FIELD: account,
                config.PLATFORM_LOGIN_PASSWORD_FIELD: encode_password(password),
                config.PLATFORM_LOGIN_GAME_ID_FIELD: resolved_game_id,
                config.PLATFORM_LOGIN_HASH_CODE_FIELD: hash_code,
            }
            envelope = self._post_on(session, config.PLATFORM_LOGIN_PATH, payload)

        data = self._parse_data(envelope, PlatformLoginData, config.PLATFORM_LOGIN_PATH)
        token = PlatformToken(
            access_token=data.access_token,
            refresh_token=data.refresh_token,
            user_id=data.user_id or platform_token_user_id(data.access_token),
            obtained_at=time.time(),
            source="credentials",
        )
        # 立即生效：内存态更新后，只读接口（get_user_info 等）可直接用。
        # 注意：本方法**不**去调用它们 —— 拿令牌就到此为止。
        self.set_token(token.access_token)
        self.logger.info("平台登录成功：%s", token.describe())
        return token

    @staticmethod
    def _extract_hash_code(envelope: PlatformEnvelope) -> int:
        """从验证码预检响应里取出 ``data.hashCode``（**整数**）。

        :raises ProtocolError: 字段缺失或不是数字 —— 说明上游那套配方变了，
            此时应当停下来核对报文，而不是猜一个值继续发。
        """
        raw = (
            envelope.data.get("hashCode")
            if isinstance(envelope.data, Mapping)
            else None
        )
        if raw is None:
            raise ProtocolError(
                "验证码预检响应缺少 data.hashCode —— 「固定 captcha 数组换票据」的配方"
                "可能已失效（服务端收紧）。\n"
                "  处理：抓一次门户前端的验证码预检请求，核对 body 形状后更新 "
                "config.PLATFORM_LOGIN_CAPTCHA_BODY（或环境变量 "
                "CHERRYTALE_LOGIN_CAPTCHA_BODY）。"
            )
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise ProtocolError(f"验证码 hashCode 不是整数：{raw!r}") from exc

    def refresh_access_token(self, refresh_token: str) -> PlatformToken:
        """用 refresh token 换一组新令牌（**不需要验证码**）。

        :param refresh_token: 上次登录响应里的 ``data.refreshToken``。
        :return: 新的令牌集合（``source="refresh"``）。
        :raises AuthenticationError: 未提供 refresh token（不发请求）。
        :raises ApiError: 服务端拒绝（例如 refresh token 也已过期）。

        实测：请求体**为空**，身份靠头里的 ``Authorization: Bearer <refreshToken>``；
        响应结构与登录一致，且 ``refreshToken`` 可能被**轮换**
        （所以要用返回值覆盖旧的那个）。
        """
        value = (refresh_token or "").strip()
        if not value:
            raise AuthenticationError(401, "缺少 refresh token（未发出任何请求）")

        with self._flow_session(
            config.PLATFORM_LOGIN_REFRESH_TIMEOUT, bearer=f"Bearer {value}"
        ) as session:
            envelope = self._post_on(session, config.PLATFORM_LOGIN_REFRESH_PATH, None)

        data = self._parse_data(
            envelope, PlatformLoginData, config.PLATFORM_LOGIN_REFRESH_PATH
        )
        token = PlatformToken(
            access_token=data.access_token,
            refresh_token=data.refresh_token or value,
            user_id=data.user_id or platform_token_user_id(data.access_token),
            obtained_at=time.time(),
            source="refresh",
        )
        self.set_token(token.access_token)
        self.logger.info("平台令牌已续期：%s", token.describe())
        return token
