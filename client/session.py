"""HTTP 通信层：基于 ``requests.Session`` 的封装。

【这一层负责什么、不负责什么】

负责：
- 维护一个 ``Session``（连接池 + Cookie 自动保持）；
- 拼 URL、拼 Header、附加令牌；
- 超时与重试策略；
- 把第三方库的异常**翻译**成本项目统一的异常（见 client/exceptions.py）。

不负责：
- 不解析业务语义。HTTP 200 不代表「操作成功」，游戏常把错误码放在
  响应体里（甚至放在自定义 Header 里），这属于 models/ 与 tasks/ 的职责。

【关于重试的一个重要安全提醒】
默认只对 ``GET`` 重试。为什么？因为游戏里大量接口是「有副作用的」：
例如「领取奖励」「购买道具」，一旦发生超时，你并不知道服务端到底处理了没有。
此时盲目重试可能**重复领取**，甚至触发风控。
所以 ``POST`` 的重试必须由调用方在确认「该接口幂等」后显式开启。
"""

from __future__ import annotations

import logging
from typing import Any, Final, Mapping

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import config
from client.exceptions import ApiError, AuthenticationError, NetworkError
from client.headers import build_headers, redact_headers
from client.tls import use_system_trust_store
from crypto.sign import TokenStore
from models.session_state import GameSessionState

#: 默认只重试幂等方法（见模块文档中的安全提醒）
DEFAULT_RETRY_METHODS: Final[frozenset[str]] = frozenset({"GET"})

#: 这些 HTTP 状态码说明「服务端暂时不行」，值得重试
RETRY_STATUS_CODES: Final[tuple[int, ...]] = (500, 502, 503, 504)

#: 报错信息里附带响应体时，最多截取多少字符（避免把整包数据刷满屏幕）
_ERROR_BODY_PREVIEW: Final[int] = 200

_LOGGER: Final[logging.Logger] = logging.getLogger("client.session")


class GameSession:
    """游戏 HTTP 会话。

    :param base_url: API 根地址。传 ``None`` 时会在**真正发请求时**才去读
        ``config.api_base_url()``——这样即使域名尚未确认，也可以先创建对象、
        跑单元测试，只有发出请求时才会明确报错。
    :param headers: 额外的基础 Header。
    :param timeout: 单次请求超时（秒）。``None`` 表示用 ``config.REQUEST_TIMEOUT``。
    :param max_retries: 重试次数。``None`` 表示用 ``config.MAX_RETRIES``。
    :param retry_backoff: 重试间隔的退避系数，``None`` 表示用配置值。
    :param verify_tls: 是否校验 TLS 证书。抓包调试时设 ``False``。
    :param proxy: 代理地址，例如 ``"http://127.0.0.1:8888"``（Charles / Fiddler）。
    :param token_store: 令牌容器，默认新建一个空的。
    :param token_field: 令牌对应的 Header 字段名。
        **必须从协议确认**；留 ``None`` 表示不自动附加令牌（此时需调用方自行传 header）。
    :param game_state: 游戏会话状态（1004 拿到的令牌 / playerId / 区服），默认空。
        它与 ``token_store`` **不是一回事**：``token_store`` 里的令牌走 HTTP 头（平台/门户），
        而 ``game_state`` 的令牌走 ``RootPacket`` 字段 2（游戏网关）。详见
        ``models/session_state.py``。
    :param retry_methods: 允许重试的 HTTP 方法，默认仅 GET。
    """

    def __init__(
        self,
        base_url: str | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        retry_backoff: float | None = None,
        verify_tls: bool | None = None,
        proxy: str | None = None,
        token_store: TokenStore | None = None,
        token_field: str | None = None,
        game_state: GameSessionState | None = None,
        retry_methods: frozenset[str] = DEFAULT_RETRY_METHODS,
        logger: logging.Logger | None = None,
    ) -> None:
        self.logger = logger or _LOGGER

        self._base_url = base_url
        self._extra_headers: dict[str, str] = dict(headers or {})
        self.timeout = config.REQUEST_TIMEOUT if timeout is None else timeout
        self.verify_tls = config.VERIFY_TLS if verify_tls is None else verify_tls
        self.proxy = proxy if proxy is not None else config.HTTP_PROXY

        self.token_store = token_store or TokenStore()
        self.token_field = token_field
        #: 游戏会话状态（1004）。**跨任务共享就靠它**：``tasks/daily.py`` 把同一个
        #: ``GameSession`` 交给每个子任务，子任务再用 ``BaseTask.open_game_client()``
        #: 把状态注入自己的 ``GameEnvelope``。
        self.game_state: GameSessionState | None = game_state

        # ---- TLS 信任库 ----
        # 必须在真正发请求之前完成注入：目标网关下发的证书链里含
        # 「交叉签名版 GTS Root R4」，Python 自带的 OpenSSL 无法建链，
        # 需要交给操作系统信任库处理。详见 client/tls.py 的排查记录。
        self.using_system_trust_store = use_system_trust_store()

        # ---- 组装底层 Session ----
        self._http = requests.Session()

        retries = config.MAX_RETRIES if max_retries is None else max_retries
        backoff = config.RETRY_BACKOFF if retry_backoff is None else retry_backoff
        self._mount_retry_adapter(retries, backoff, retry_methods)

    # ------------------------------------------------------------------
    # 内部装配
    # ------------------------------------------------------------------
    def _mount_retry_adapter(
        self,
        retries: int,
        backoff: float,
        retry_methods: frozenset[str],
    ) -> None:
        """把「自动重试」挂到 Session 上。

        ``raise_on_status=False`` 很关键：我们希望由自己判断状态码并翻译成
        本项目异常，而不是让 urllib3 抛出一个难以理解的原生异常。

        .. note::
           本机安装的是 urllib3 2.8.0，其 ``Retry`` **已移除** ``retry_on_timeout``
           参数（1.x 时代才有）。在 2.x 中读取超时由 ``read`` 重试次数覆盖，
           因此这里只配置 ``read`` 即可，不需要也不要再传 ``retry_on_timeout``。
        """
        retry_policy = Retry(
            total=retries,
            connect=retries,
            read=retries,
            status=retries,
            backoff_factor=backoff,
            status_forcelist=RETRY_STATUS_CODES,
            allowed_methods=frozenset(method.upper() for method in retry_methods),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry_policy)
        self._http.mount("https://", adapter)
        self._http.mount("http://", adapter)

    # ------------------------------------------------------------------
    # Header 维护
    # ------------------------------------------------------------------
    def update_headers(self, mapping: Mapping[str, str | None]) -> None:
        """就地更新会话的基础 Header。

        - 值为 ``None`` 或空字符串 → **删除**该 Header；
        - 其它值 → 新增或覆盖。

        为什么要支持「删除」？两个真实场景：
        1. 退出登录时要摘掉鉴权头，否则会带着失效令牌继续请求；
        2. 令牌刷新失败后要清空旧值，而不是留下一个过期的空壳。
        如果这里只是「忽略空值」，上面两种情况都无法表达，只能去动私有属性。

        典型用法：登录成功后给后续所有请求补上鉴权头，
        而不重新创建 Session（重建会丢掉连接池与已建立的 Cookie）。
        """
        for key, value in mapping.items():
            if value:
                self._extra_headers[key] = value
            else:
                self._extra_headers.pop(key, None)

    # ------------------------------------------------------------------
    # 游戏会话状态（1004 令牌）
    # ------------------------------------------------------------------
    def set_game_state(self, state: GameSessionState | None) -> None:
        """设置（或清空）游戏会话状态。

        :param state: 登录成功后构造的状态；``None`` 表示清空（例如切换账号）。

        调用方只有登录任务（``tasks/login.py``）。**其他任何地方都不该写它** ——
        状态只能来自服务端的 1004 响应，手写一个假的只会把问题推迟到发包那一刻。
        """
        self.game_state = state

    def require_game_state(self) -> GameSessionState:
        """取出可用的游戏会话状态；没有就**明确报错**。

        :raises AuthenticationError: 状态为空、或令牌为空时。

        报错信息刻意写清"为什么平台令牌不能代替它" —— 这是最常被搞混的一点：
        两处令牌看起来都是一串字符，但去向完全不同。
        """
        state = self.game_state
        if state is None or not state.is_valid():
            raise AuthenticationError(
                401,
                "当前没有可用的游戏会话状态（1004 令牌）。\n"
                "  常见原因：这个进程没登录过，或会话文件已被清掉。\n"
                "  处理方式：\n"
                "    1. python main.py run login"
                "      （登录成功会自动把会话写进 .session.json）\n"
                "    2. 或临时用环境变量：CHERRYTALE_SESSION_TOKEN=<令牌>\n"
                "  ⚠️ 平台令牌（.auth_token）不能代替它 —— 那个走 HTTP 头，"
                "游戏网关认的是 RootPacket 字段 2 里的令牌。",
            )
        return state

    def has_auth_context(self) -> bool:
        """是否具备登录态（供任务的前置检查使用）。

        :return: 游戏会话状态有效 → ``True``；否则看平台令牌是否有效。

        【为什么是"或"，而不是只认游戏会话状态】
        两个层面各自都有"登录态"：游戏网关认 ``game_state``（RootPacket 字段 2），
        平台/门户接口认 ``token_store``（HTTP 头）。任务的前置检查只是**挡住
        "完全没登录过"**这种情况，用"或"能同时兼容两类调用方（含既有测试与平台脚本）。

        真正的硬保证不在这里，而在 :meth:`client.game_client.GameClient.for_session`
        与 ``BaseTask.open_game_client()``：**没有有效状态就构造不出可发包的客户端**。
        """
        if self.game_state is not None and self.game_state.is_valid():
            return True
        return self.token_store.is_valid()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def close(self) -> None:
        """关闭连接池。用完请调用，或直接使用 ``with`` 语句。"""
        self._http.close()

    def __enter__(self) -> "GameSession":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # URL
    # ------------------------------------------------------------------
    def resolve_base_url(self) -> str:
        """取得 API 根地址。

        :raises config.ConfigError: 未显式传入 ``base_url`` 且配置里也没有域名时。
        """
        if self._base_url:
            return self._base_url
        return config.api_base_url()

    @staticmethod
    def is_absolute_url(path: str) -> bool:
        """判断是否已经是完整 URL（而不是相对路径）。"""
        return path.startswith(("http://", "https://"))

    def build_url(self, path: str) -> str:
        """把相对路径拼到根地址上。

        特意用字符串拼接而不是 ``urljoin``：``urljoin`` 遇到以 ``/`` 开头的路径时
        会丢掉根地址里的子路径（例如 ``/api/v2``），这在游戏协议里是常见坑。
        """
        if self.is_absolute_url(path):
            return path

        base = self.resolve_base_url().rstrip("/")
        return f"{base}/{path.lstrip('/')}"

    # ------------------------------------------------------------------
    # 请求组装（纯函数式：不发任何网络流量，因此可以放心单元测试）
    # ------------------------------------------------------------------
    def prepare_request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: bytes | str | None = None,
        json_body: Any | None = None,
        headers: Mapping[str, str] | None = None,
        content_type: str | None = None,
        use_token: bool = True,
    ) -> dict[str, Any]:
        """把一次请求的全部参数组装成字典，供 :meth:`send` 使用。

        :param data: 原始请求体字节。**protobuf 报文走这个参数**
            （本游戏协议是 protobuf，见 dump 中的 ``com.auer.game.protobuf``）。
        :param json_body: JSON 请求体。与 ``data`` 互斥。
        :param content_type: 覆盖 ``Content-Type``。
        :param use_token: 是否自动附加已保存的令牌。
        :return: 可直接展开传给 ``requests`` 的参数字典。
        :raises ValueError: 同时传了 ``data`` 与 ``json_body``，
            或「已有令牌却没配置 token_field」时。

        为什么已有令牌却没有 ``token_field`` 要报错？
        因为那意味着我们不知道该把令牌放进哪个 Header。
        此时静默丢弃令牌，会让你误以为「已经带着登录态发请求了」，
        后面排查起来极其痛苦。
        """
        if data is not None and json_body is not None:
            raise ValueError("data 与 json_body 不能同时使用，请二选一")

        token: str | None = None
        token_field: str | None = None
        if use_token and self.token_store.token:
            if self.token_field is None:
                raise ValueError(
                    "当前已保存令牌，但未配置 token_field，无法确定令牌该放进哪个 Header。\n"
                    "请从 dump.cs 或抓包确认字段名后，用 "
                    "GameSession(..., token_field='Authorization') 指定。"
                )
            token = self.token_store.token
            token_field = self.token_field

        merged_headers = build_headers(
            content_type=content_type,
            token=token,
            token_field=token_field,
            base=self._extra_headers,
            extra=headers,
        )

        prepared: dict[str, Any] = {
            "method": method.upper(),
            "url": self.build_url(path),
            "headers": merged_headers,
            "timeout": self.timeout,
            "verify": self.verify_tls,
        }
        if params:
            prepared["params"] = dict(params)
        if data is not None:
            prepared["data"] = data
        if json_body is not None:
            prepared["json"] = json_body
        if self.proxy:
            prepared["proxies"] = {"http": self.proxy, "https": self.proxy}
        return prepared

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------
    def send(self, prepared: Mapping[str, Any]) -> requests.Response:
        """真正发出请求，并把第三方异常翻译成本项目异常。

        :raises NetworkError: 超时或连接失败（重试已由底层自动完成）。
        """
        method = str(prepared.get("method", "GET"))
        url = str(prepared.get("url", ""))
        # 日志里绝不能出现明文令牌，因此先脱敏
        self.logger.debug(
            "→ %s %s headers=%s", method, url, redact_headers(prepared.get("headers") or {})
        )
        try:
            response = self._http.request(**prepared)
        except requests.Timeout as exc:
            raise NetworkError(
                f"请求超时（{self.timeout} 秒）：{method} {url}"
            ) from exc
        except requests.RequestException as exc:
            raise NetworkError(f"网络请求失败：{method} {url} —— {exc}") from exc

        self.logger.debug("← HTTP %s %s %s", response.status_code, method, url)
        return response

    def request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        """组装并发送一次请求（:meth:`prepare_request` + :meth:`send`）。

        .. note::
           本方法**不**自动判定业务成功与否。HTTP 200 与「操作成功」是两件事，
           是否成功要看响应体里的业务码，那是 models/ 与 tasks/ 的职责。
        """
        return self.send(self.prepare_request(method, path, **kwargs))

    def get(self, path: str, **kwargs: Any) -> requests.Response:
        """发起 GET 请求（默认允许自动重试）。"""
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> requests.Response:
        """发起 POST 请求。

        ⚠️ 默认**不会**自动重试 POST（见模块文档的安全提醒）。
        确认某接口幂等后，可传 ``retry_methods=frozenset({"GET", "POST"})`` 重新构造会话。
        """
        return self.request("POST", path, **kwargs)


# ---------------------------------------------------------------------------
# 模块级辅助函数
# ---------------------------------------------------------------------------
def raise_for_status(response: requests.Response) -> None:
    """把 HTTP 错误状态翻译成本项目的异常；成功（< 400）时什么也不做。

    :raises AuthenticationError: 401 / 403 —— 需要登录或令牌失效。
    :raises ApiError: 其它 4xx / 5xx。

    报错信息里会附带一小段响应体（最多 200 字符）便于定位，
    但**绝不会**拼进请求 Header，避免凭据出现在日志中。
    """
    if response.status_code < 400:
        return

    body = response.content or b""
    snippet = body[:_ERROR_BODY_PREVIEW].decode("utf-8", errors="replace")
    snippet = snippet or "<空响应体>"

    if response.status_code in (401, 403):
        raise AuthenticationError(response.status_code, snippet)
    raise ApiError(response.status_code, snippet)


def describe_response(response: requests.Response) -> str:
    """生成一行便于日志阅读的响应摘要。

    刻意**不**包含响应体内容：报文可能是二进制 protobuf，
    打印出来只会刷屏；更重要的是响应体可能含敏感字段。
    """
    elapsed = response.elapsed.total_seconds() if response.elapsed else 0.0
    return (
        f"HTTP {response.status_code} | {len(response.content)} 字节 | "
        f"耗时 {elapsed:.3f}s | {response.headers.get('Content-Type', '<无 Content-Type>')}"
    )

