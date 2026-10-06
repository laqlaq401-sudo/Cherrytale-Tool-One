"""平台网关连通性测试脚本 + 离线单元测试。

【这个文件有两种用法，别混淆】

1. **当脚本跑（会真的联网，这就是它的主业）**::

       python -m tests.test_platform

   实例化 ``PlatformClient``，依次请求两个接口，把状态码、响应头、
   响应体完整打印到终端，用来验证「网络能不能通、Token 对不对」。

2. **当测试跑（默认不联网，保证 pytest 永远快速且离线可跑）**::

       python -m pytest tests/test_platform.py        # 只跑离线用例
       CHERRYTALE_RUN_NETWORK_TESTS=1 python -m pytest tests/test_platform.py

   为什么要用环境变量把联网用例挡在外面？
   因为「依赖外部服务的测试」天然不稳定：服务器抖动、无网络、被墙、
   账号被限流都会让 CI 变红，久而久之你就学会无视红色——那才是最危险的。
   所以默认离线，想验证连通性时显式打开。

【关于打印响应数据】
Token 属于凭据，**绝不打印**；脚本里所有输出都经过脱敏处理。
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import time

import pytest
import requests

import config
from client.exceptions import (
    ApiError,
    AuthenticationError,
    NetworkError,
    ProtocolError,
)
from client.platform_client import PlatformClient

# 复用 main.py 里的「强制 UTF-8 输出」。本文件属于项目内的测试包，
# 与 tools/ 下的独立脚本不同，这里直接复用比再抄一份更合适。
from main import force_utf8_output
from models.base import PayloadError
from models.platform import (
    GameBindingStatus,
    PlatformEnvelope,
    PlatformUser,
)

# 复用 client 测试里的假传输层，避免两份「手工构造响应」的代码各写各的
from tests.test_client import FakeTransport, make_response

#: 是否允许跑真实网络用例。默认关闭，避免 pytest 因外部服务不稳定而变红。
NETWORK_TESTS_ENABLED = os.getenv("CHERRYTALE_RUN_NETWORK_TESTS", "0") == "1"

requires_network = pytest.mark.skipif(
    not NETWORK_TESTS_ENABLED,
    reason="默认不发起真实网络请求；需要时设 CHERRYTALE_RUN_NETWORK_TESTS=1",
)

#: 平台网关**失败响应**的实测结构（按实际观察到的一字不差地重建）。
#: 观察记录：不带 Token 请求 ``/api/v2/user`` 时返回 ``HTTP 400``，响应体为：
#: ``{"status":"FAIL","message":"驗證失敗","errorCode":4001,"data":null}``
OBSERVED_AUTH_FAILURE_BODY = json.dumps(
    {"status": "FAIL", "message": "驗證失敗", "errorCode": 4001, "data": None},
    ensure_ascii=False,
).encode("utf-8")

#: 成功响应实测原文：``GET /api/v2/user``（HTTP 200，2026-09-20 实测）
OBSERVED_USER_INFO_SUCCESS_BODY = json.dumps(
    {
        "status": "SUCCESS",
        "data": {
            "userId": "ER00000000-0000-0000-0000-000000000000",
            "coins": 30,
            "guest": False,
        },
        "serverTime": 1789893660724,
    },
    ensure_ascii=False,
).encode("utf-8")

#: 成功响应实测原文：``GET /api/v2/user/games/status/27``（HTTP 200，2026-09-20 实测）
OBSERVED_GAME_STATUS_SUCCESS_BODY = json.dumps(
    {
        "status": "SUCCESS",
        "data": {
            "isBound": True,
            "gameAccount": "ER00000000-0000-0000-0000-000000000000",
        },
        "serverTime": 1789893660810,
    },
    ensure_ascii=False,
).encode("utf-8")

#: 假传输层默认返回体 = **用户信息的真实成功响应**（最贴近真实场景）
SUCCESS_ENVELOPE_BODY = OBSERVED_USER_INFO_SUCCESS_BODY


# ---------------------------------------------------------------------------
# 打印辅助（仅脚本模式使用；所有输出均已脱敏）
# ---------------------------------------------------------------------------
def print_response(label: str, response: requests.Response) -> None:
    """把一次响应的关键信息与响应体打印到终端。

    注意：这里只打印**服务端返回的内容**。请求头里的 Token 属于凭据，
    一律不打印（要看请求头请用 ``PlatformClient.headers_preview()``，它带脱敏）。
    """
    method = response.request.method if response.request is not None else "GET"
    print(f"\n{'=' * 74}")
    print(f"【{label}】")
    print(f"{'=' * 74}")
    print(f"  请求        : {method} {response.url}")
    print(f"  HTTP 状态   : {response.status_code} {response.reason}")
    print(f"  耗时        : {response.elapsed.total_seconds():.3f}s")
    print(f"  内容长度    : {len(response.content)} 字节")
    print(f"  内容类型    : {response.headers.get('Content-Type', '<无>')}")

    body = response.content or b""
    if not body:
        print("  响应体      : <空>")
        return

    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        preview = body[:500].decode("utf-8", errors="replace")
        print("  响应体      : <非 JSON，下面显示前 500 字节>")
        for line in preview.splitlines():
            print(f"    {line}")
        return

    print("  响应体(JSON):")
    for line in json.dumps(data, ensure_ascii=False, indent=2).splitlines():
        print(f"    {line}")


# ---------------------------------------------------------------------------
# 配置离线测试
# ---------------------------------------------------------------------------
class TestPlatformConfig:
    """平台网关配置：常量、默认 Header、鉴权包装、快照不泄密。"""

    def test_base_url(self) -> None:
        assert config.PLATFORM_BASE_URL == "https://sadpki-portal-v2.ebuajk.com"

    def test_user_info_path(self) -> None:
        assert config.USER_INFO_PATH == "/api/v2/user"

    def test_game_status_path_template(self) -> None:
        assert config.GAME_STATUS_PATH == "/api/v2/user/games/status/{game_id}"

    def test_default_game_id_is_27(self) -> None:
        assert config.DEFAULT_GAME_ID == 27

    def test_game_status_path_uses_default_id(self) -> None:
        assert config.game_status_path() == "/api/v2/user/games/status/27"

    def test_game_status_path_accepts_custom_id(self) -> None:
        assert config.game_status_path(99) == "/api/v2/user/games/status/99"

    def test_user_agent_is_browser_style(self) -> None:
        """UA 必须是**浏览器**身份（认证请求实际由内嵌 WebView 发出）。

        依据：HAR 实测 UA 为 ``... Edg/153.0.0.0``，并带
        ``sec-ch-ua: "Microsoft Edge WebView2"``。
        若继续用 Unity 的 UA，网关可能直接按风控理由拒绝。
        """
        assert config.WEBVIEW_USER_AGENT.startswith("Mozilla/5.0")
        assert "Edg/" in config.WEBVIEW_USER_AGENT

    def test_unity_identity_kept_for_future_api_use(self) -> None:
        """Unity 身份常量保留备用（游戏 API 侧将来可能用到，但**不用于平台网关**）。"""
        assert config.UNITY_USER_AGENT.startswith("UnityPlayer/2020.3.49f1")
        assert config.UNITY_VERSION == "2020.3.49f1"

    def test_device_id_default_is_placeholder(self) -> None:
        """默认设备标识是**等长占位符**，不再是开发机真实值。

        真实设备标识属凭据类信息（与平台 userId 一起可复现登录报文），
        已在 2026-10-05 安全审计中脱敏。长度仍为 40，见下一条断言。
        """
        assert config.DEVICE_ID == "0" * 40
        assert config.DEVICE_ID == config._DEVICE_ID_DEFAULT

    def test_header_device_id_is_sha1_length(self) -> None:
        """请求头 ``deviceId`` 是 40 位十六进制（SHA-1 摘要长度）。"""
        assert len(config.DEVICE_ID) == 40

    def test_header_device_id_differs_from_jwt_claim(self) -> None:
        """请求头 ``deviceId`` 与 JWT 载荷里的 ``device_id`` **不是同一个值**。

        本用例存在的意义是**防止有人「顺手修好」这个看似的不一致**：
        一旦加上「校验二者相等」的逻辑，之后每次请求都会误报失败。

        .. note::
           这里刻意**从 Token 里现读** device_id，而不是写一个手抄的常量 ——
           手抄常量早晚会抄错（本用例的第一版就是把 64 位抄成了 63 位，
           被测试自己抓出来了）。需要 Token 才跑，没有就跳过。
        """
        if not config.has_auth_token():
            pytest.skip("未配置 Token，读不到 JWT 载荷；该断言需要真实 Token")

        payload = config.AUTH_TOKEN.split(".")[1]
        claims = json.loads(
            base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        )
        jwt_device_id = claims["device_id"]

        assert len(jwt_device_id) == 64  # SHA-256 十六进制
        assert len(config.DEVICE_ID) == 40  # SHA-1 十六进制
        assert jwt_device_id != config.DEVICE_ID

    def test_platform_headers_use_webview_identity(self) -> None:
        headers = config.platform_headers(include_auth=False)
        assert headers["User-Agent"] == config.WEBVIEW_USER_AGENT
        assert headers["sec-ch-ua"] == config.WEBVIEW_SEC_CH_UA
        assert headers["deviceId"] == config.DEVICE_ID

    def test_platform_headers_include_web_context(self) -> None:
        """网页上下文头必须齐全：缺 ``Origin``/``Referer`` 可能被网关按风控拒绝。"""
        headers = config.platform_headers(include_auth=False)
        assert headers["Origin"] == config.PLATFORM_BASE_URL
        assert headers["Referer"] == config.PLATFORM_LOGIN_REFERER
        assert headers["lang"] == config.PLATFORM_LANG
        assert headers["Accept"] == config.PLATFORM_ACCEPT
        assert headers["Accept-Language"] == config.PLATFORM_ACCEPT_LANGUAGE
        assert headers["Sec-Fetch-Site"] == "same-origin"

    def test_platform_headers_do_not_leak_unity_identity(self) -> None:
        """平台上**不应**出现 Unity 专有头：两套身份混用反而更可疑。"""
        headers = config.platform_headers(include_auth=False)
        assert "X-Unity-Version" not in headers
        assert "UnityPlayer" not in headers["User-Agent"]

    def test_referer_points_to_login_page(self) -> None:
        assert config.PLATFORM_LOGIN_REFERER.endswith("/login/loginPage")

    def test_content_type_present(self) -> None:
        """GET 上其实无意义，但实测客户端带了，为保真照抄。"""
        headers = config.platform_headers(include_auth=False)
        assert headers["Content-Type"] == "application/json"

    def test_no_auth_header_when_token_missing(self, monkeypatch) -> None:
        """没有令牌时**不要**发出空值 Header，否则 401 会变成 400，干扰判断。"""
        monkeypatch.setattr(config, "AUTH_TOKEN", "")
        headers = config.platform_headers()
        assert config.AUTH_HEADER_NAME not in headers

    def test_auth_headers_empty_without_token(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "AUTH_TOKEN", "")
        assert config.auth_headers() == {}

    def test_auth_headers_with_token(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "AUTH_TOKEN", "abc123")
        assert config.auth_headers() == {config.AUTH_HEADER_NAME: "Bearer abc123"}

    def test_format_auth_value_default_is_bearer(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "AUTH_VALUE_TEMPLATE", "Bearer {token}")
        assert config.format_auth_value("t0k") == "Bearer t0k"

    def test_format_auth_value_supports_bare_token(self, monkeypatch) -> None:
        """若抓包显示服务端要裸 token，只改配置模板即可，无需改代码。"""
        monkeypatch.setattr(config, "AUTH_VALUE_TEMPLATE", "{token}")
        assert config.format_auth_value("t0k") == "t0k"

    def test_summary_exposes_platform_fields(self) -> None:
        summary = config.summary()
        assert summary["platform_base_url"] == config.PLATFORM_BASE_URL
        assert summary["auth_header"] == config.AUTH_HEADER_NAME
        assert "auth_token_configured" in summary

    def test_summary_never_leaks_token(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "AUTH_TOKEN", "MY-SECRET-TOKEN-VALUE")
        assert "MY-SECRET-TOKEN-VALUE" not in repr(config.summary())
        assert config.summary()["auth_token_configured"] is True


# ---------------------------------------------------------------------------
# PlatformClient 离线测试（用假传输层，永不联网）
# ---------------------------------------------------------------------------
class TestPlatformClientOffline:
    """验证「请求怎么组装」「异常怎么翻译」，全部在离线状态下完成。

    做法与 tests/test_client.py 一致：把底层 ``requests.Session.request``
    换成记录参数、返回预设响应的假传输层。

    .. note::
       这里访问 ``client._session._http`` 属于测试专用的「白盒」手法。
       之所以能这么做，是因为 GameSession 把真正的传输对象收在 ``_http`` 里，
       换掉它就等价于换掉网线——生产代码不必为此再开一个公开接口。
    """

    def make_client(
        self,
        monkeypatch,
        *,
        token: str | None = None,
        config_token: str = "",
    ) -> tuple[PlatformClient, FakeTransport]:
        """构造被测客户端 + 假传输层。

        :param config_token: 预写入 ``config.AUTH_TOKEN`` 的值。默认清空，
            保证测试结果不受你本机 ``.auth_token`` 文件内容的影响。
        """
        monkeypatch.setattr(config, "AUTH_TOKEN", config_token)
        client = PlatformClient(token=token)
        fake = FakeTransport(
            response=make_response(status_code=200, content=SUCCESS_ENVELOPE_BODY)
        )
        monkeypatch.setattr(client._session._http, "request", fake.request)
        return client, fake

    # ---- 正常路径（用**实测响应原文**驱动）----
    def test_get_user_info_returns_typed_model(self, monkeypatch) -> None:
        """成功时应返回 ``PlatformUser`` 模型，而不是裸字典。"""
        client, _ = self.make_client(monkeypatch)
        user = client.get_user_info()

        assert isinstance(user, PlatformUser)
        assert user.user_id == "ER00000000-0000-0000-0000-000000000000"
        assert user.coins == 30
        assert user.guest is False

    def test_user_info_model_parses_camel_case_aliases(self, monkeypatch) -> None:
        """服务端发的是 ``userId``，Python 侧应能读成 ``user_id``。"""
        client, _ = self.make_client(monkeypatch)
        assert client.get_user_info().user_id == "ER00000000-0000-0000-0000-000000000000"

    def test_user_info_url(self, monkeypatch) -> None:
        client, fake = self.make_client(monkeypatch)
        client.get_user_info()
        assert fake.calls[0]["url"] == "https://sadpki-portal-v2.ebuajk.com/api/v2/user"

    def test_game_status_url_uses_default_game_id(self, monkeypatch) -> None:
        client, fake = self.make_client(monkeypatch)
        fake.response = make_response(
            status_code=200, content=OBSERVED_GAME_STATUS_SUCCESS_BODY
        )
        client.get_game_status()
        assert fake.calls[0]["url"].endswith("/api/v2/user/games/status/27")

    def test_game_status_url_accepts_custom_game_id(self, monkeypatch) -> None:
        client, fake = self.make_client(monkeypatch)
        fake.response = make_response(
            status_code=200, content=OBSERVED_GAME_STATUS_SUCCESS_BODY
        )
        status = client.get_game_status(99)

        assert isinstance(status, GameBindingStatus)
        assert status.is_bound is True
        assert status.game_account == "ER00000000-0000-0000-0000-000000000000"
        assert fake.calls[0]["url"].endswith("/api/v2/user/games/status/99")

    def test_game_status_parses_camel_case_aliases(self, monkeypatch) -> None:
        """服务端发 ``isBound`` / ``gameAccount``，Python 侧读作 ``is_bound`` / ``game_account``。"""
        client, fake = self.make_client(monkeypatch)
        fake.response = make_response(
            status_code=200, content=OBSERVED_GAME_STATUS_SUCCESS_BODY
        )
        assert client.get_game_status().is_bound is True

    def test_game_status_with_unbound_account(self, monkeypatch) -> None:
        """未绑定账号的场景：``isBound`` 为 false 也必须能正常解析。"""
        client, fake = self.make_client(monkeypatch)
        fake.response = make_response(
            status_code=200,
            content=json.dumps(
                {
                    "status": "SUCCESS",
                    "data": {"isBound": False, "gameAccount": ""},
                }
            ).encode("utf-8"),
        )
        assert client.get_game_status().is_bound is False

    # ---- Header 组装 ----
    def test_required_headers_are_sent(self, monkeypatch) -> None:
        """整套 WebView 身份头必须真的发出去（缺一项都可能被风控拦）。"""
        client, fake = self.make_client(monkeypatch)
        client.get_user_info()
        headers = fake.calls[0]["headers"]
        assert headers["User-Agent"] == config.WEBVIEW_USER_AGENT
        assert headers["deviceId"] == config.DEVICE_ID
        assert headers["Origin"] == config.PLATFORM_BASE_URL
        assert headers["Referer"] == config.PLATFORM_LOGIN_REFERER
        assert headers["lang"] == config.PLATFORM_LANG

    def test_auth_header_sent_when_token_given(self, monkeypatch) -> None:
        client, fake = self.make_client(monkeypatch, token="tok-abc")
        client.get_user_info()
        assert fake.calls[0]["headers"][config.AUTH_HEADER_NAME] == "Bearer tok-abc"

    def test_auth_header_absent_for_empty_token(self, monkeypatch) -> None:
        client, fake = self.make_client(monkeypatch, token="")
        client.get_user_info()
        assert config.AUTH_HEADER_NAME not in fake.calls[0]["headers"]

    # ---- 异常翻译 ----
    def test_401_raises_authentication_error(self, monkeypatch) -> None:
        client, fake = self.make_client(monkeypatch, token="bad-token")
        fake.response = make_response(
            status_code=401, content=b'{"msg":"invalid token"}'
        )
        with pytest.raises(AuthenticationError):
            client.get_user_info()

    def test_403_raises_authentication_error(self, monkeypatch) -> None:
        client, fake = self.make_client(monkeypatch, token="bad-token")
        fake.response = make_response(status_code=403, content=b"forbidden")
        with pytest.raises(AuthenticationError):
            client.get_game_status()

    def test_500_raises_api_error(self, monkeypatch) -> None:
        client, fake = self.make_client(monkeypatch)
        fake.response = make_response(status_code=500, content=b"oops")
        with pytest.raises(ApiError):
            client.get_user_info()

    def test_html_error_page_raises_protocol_error(self, monkeypatch) -> None:
        """被网关拦截时常常返回 HTML 错误页，必须给出可读提示。"""
        client, fake = self.make_client(monkeypatch)
        fake.response = make_response(
            status_code=200, content=b"<html><body>502 Bad Gateway</body></html>"
        )
        with pytest.raises(ProtocolError, match="不是合法 JSON"):
            client.get_user_info()

    def test_json_array_raises_protocol_error(self, monkeypatch) -> None:
        """接口本应返回对象，若回来的是数组，说明结构理解有偏差。"""
        client, fake = self.make_client(monkeypatch)
        fake.response = make_response(status_code=200, content=b"[1, 2, 3]")
        with pytest.raises(ProtocolError, match="期望返回 JSON 对象"):
            client.get_user_info()

    def test_timeout_becomes_network_error(self, monkeypatch) -> None:
        client, fake = self.make_client(monkeypatch)
        fake.error = requests.Timeout("read timed out")
        with pytest.raises(NetworkError, match="请求超时"):
            client.get_user_info()

    # ---- fetch：诊断专用的「不抛业务异常」入口 ----
    def test_fetch_returns_401_without_raising(self, monkeypatch) -> None:
        client, fake = self.make_client(monkeypatch, token="bad")
        fake.response = make_response(status_code=401, content=b'{"msg":"expired"}')
        assert client.fetch(config.USER_INFO_PATH).status_code == 401

    def test_fetch_keeps_error_body_for_diagnosis(self, monkeypatch) -> None:
        """诊断时最关键的信息就是错误响应体原文。"""
        client, fake = self.make_client(monkeypatch, token="bad")
        fake.response = make_response(status_code=401, content=b'{"msg":"expired"}')
        assert b"expired" in client.fetch(config.USER_INFO_PATH).content

    # ---- Token 与脱敏 ----
    def test_set_token_adds_auth_header(self, monkeypatch) -> None:
        client, fake = self.make_client(monkeypatch, token="")
        client.set_token("new-token")
        client.get_user_info()
        assert fake.calls[0]["headers"][config.AUTH_HEADER_NAME] == "Bearer new-token"

    def test_set_token_empty_removes_auth_header(self, monkeypatch) -> None:
        """退出登录 / 令牌失效后必须能摘掉鉴权头。"""
        client, fake = self.make_client(monkeypatch, token="old-token")
        client.set_token("")
        client.get_user_info()
        assert config.AUTH_HEADER_NAME not in fake.calls[0]["headers"]

    def test_headers_preview_masks_token(self, monkeypatch) -> None:
        client, _ = self.make_client(monkeypatch, token="super-secret")
        preview = client.headers_preview()
        assert preview[config.AUTH_HEADER_NAME] == "***"
        assert "super-secret" not in repr(preview)

    def test_headers_preview_keeps_device_id(self, monkeypatch) -> None:
        """设备号不是凭据，保留它才能确认「这个 Header 到底发了没有」。"""
        client, _ = self.make_client(monkeypatch)
        assert client.headers_preview()["deviceId"] == config.DEVICE_ID

    def test_describe_does_not_leak_token(self, monkeypatch) -> None:
        client, _ = self.make_client(monkeypatch, token="super-secret")
        assert "super-secret" not in client.describe()

    def test_describe_reports_token_state(self, monkeypatch) -> None:
        configured, _ = self.make_client(monkeypatch, token="t")
        missing, _ = self.make_client(monkeypatch, token="")
        assert "已配置" in configured.describe()
        assert "未配置" in missing.describe()

    def test_context_manager_closes_session(self, monkeypatch) -> None:
        client, _ = self.make_client(monkeypatch)
        flags = {"closed": False}
        monkeypatch.setattr(
            client._session, "close", lambda: flags.__setitem__("closed", True)
        )
        with client as handle:
            assert handle is client
        assert flags["closed"] is True

    # ---- 业务信封翻译：用**实测响应**做回归 ----
    def test_observed_400_plus_4001_becomes_auth_error(self, monkeypatch) -> None:
        """核心回归用例。

        实测：不带 Token 请求时网关返回 ``HTTP 400`` 加
        ``{"status":"FAIL","message":"驗證失敗","errorCode":4001,"data":null}``。

        本用例保证该组合**永远**被翻译成 ``AuthenticationError``，
        而不是笼统的 ``ApiError`` —— 否则你会跑去查参数问题，
        而真正的原因是令牌无效，排查方向直接跑偏。
        """
        client, fake = self.make_client(monkeypatch, token="")
        fake.response = make_response(
            status_code=400, content=OBSERVED_AUTH_FAILURE_BODY
        )

        with pytest.raises(AuthenticationError) as excinfo:
            client.get_user_info()

        assert excinfo.value.code == 4001
        assert "驗證失敗" in excinfo.value.message

    def test_http_200_with_fail_status_is_still_failure(self, monkeypatch) -> None:
        """HTTP 200 不等于业务成功 —— 这条防线必须一直有效。"""
        client, fake = self.make_client(monkeypatch, token="stale-token")
        fake.response = make_response(
            status_code=200, content=OBSERVED_AUTH_FAILURE_BODY
        )
        with pytest.raises(AuthenticationError):
            client.get_game_status()

    def test_business_error_with_other_code_is_api_error(self, monkeypatch) -> None:
        client, fake = self.make_client(monkeypatch, token="t")
        fake.response = make_response(
            status_code=200,
            content=json.dumps(
                {"status": "FAIL", "message": "參數錯誤", "errorCode": 2001},
                ensure_ascii=False,
            ).encode("utf-8"),
        )
        with pytest.raises(ApiError) as excinfo:
            client.get_user_info()
        assert excinfo.value.code == 2001

    def test_missing_status_field_is_treated_as_failure(self, monkeypatch) -> None:
        """结构不认识时宁可报错，也不要当成成功继续往下跑。"""
        client, fake = self.make_client(monkeypatch, token="t")
        fake.response = make_response(status_code=200, content=b'{"foo": "bar"}')
        with pytest.raises(ApiError):
            client.get_user_info()

    def test_get_envelope_returns_parsed_envelope(self, monkeypatch) -> None:
        client, fake = self.make_client(monkeypatch, token="t")
        fake.response = make_response(status_code=200, content=SUCCESS_ENVELOPE_BODY)
        assert client.get_envelope(config.USER_INFO_PATH).is_success() is True

    def test_envelope_summary_is_loggable(self, monkeypatch) -> None:
        client, fake = self.make_client(monkeypatch, token="t")
        fake.response = make_response(status_code=200, content=SUCCESS_ENVELOPE_BODY)
        assert "SUCCESS" in client.get_envelope(config.USER_INFO_PATH).summary()

    def test_success_with_null_data_raises_protocol_error(self, monkeypatch) -> None:
        """成功但 ``data`` 为 null，说明我们对成功结构的理解有偏差，应明确报错。

        注意报错信息要说清「成功但 data 为 null」—— 若只说「不是 JSON 对象」，
        你会去找解析问题，而真实原因是**响应结构变了**。
        """
        client, fake = self.make_client(monkeypatch, token="t")
        fake.response = make_response(
            status_code=200,
            content=json.dumps(
                {"status": "SUCCESS", "errorCode": 0, "data": None}
            ).encode("utf-8"),
        )
        with pytest.raises(ProtocolError, match="data 为 null"):
            client.get_user_info()

    def test_user_info_data_mismatch_raises_protocol_error(self, monkeypatch) -> None:
        """``data`` 结构与模型不符时要给出可操作提示（而非静默丢弃字段）。"""
        client, fake = self.make_client(monkeypatch, token="t")
        fake.response = make_response(
            status_code=200,
            content=json.dumps(
                {"status": "SUCCESS", "data": {"unexpectedField": 1}}
            ).encode("utf-8"),
        )
        with pytest.raises(ProtocolError, match="与 PlatformUser 不符"):
            client.get_user_info()


# ---------------------------------------------------------------------------
# PlatformEnvelope 模型（字段结构来自实测响应）
# ---------------------------------------------------------------------------
class TestPlatformEnvelope:
    """信封模型的解析与判定逻辑。"""

    def test_parses_observed_failure_body(self) -> None:
        envelope = PlatformEnvelope.from_json(OBSERVED_AUTH_FAILURE_BODY)
        assert envelope.status == "FAIL"
        assert envelope.message == "驗證失敗"
        assert envelope.error_code == 4001  # JSON 里的 errorCode 已正确映射
        assert envelope.data is None

    def test_alias_accepts_python_style_name(self) -> None:
        """``populate_by_name=True`` 的效果：两种写法都能赋值。"""
        assert PlatformEnvelope(error_code=4001).error_code == 4001
        assert PlatformEnvelope(errorCode=4001).error_code == 4001

    def test_failure_is_not_success(self) -> None:
        envelope = PlatformEnvelope.from_json(OBSERVED_AUTH_FAILURE_BODY)
        assert envelope.is_failure() is True
        assert envelope.is_success() is False

    def test_success_status(self) -> None:
        assert PlatformEnvelope(status="SUCCESS").is_success() is True

    def test_success_status_is_case_insensitive(self) -> None:
        assert PlatformEnvelope(status="success").is_success() is True

    def test_missing_status_is_failure(self) -> None:
        """缺字段宁可判失败，也不要放行。"""
        assert PlatformEnvelope(data={"a": 1}).is_success() is False

    def test_is_auth_failure_for_4001(self) -> None:
        assert PlatformEnvelope.from_json(OBSERVED_AUTH_FAILURE_BODY).is_auth_failure() is True

    def test_not_auth_failure_for_other_code(self) -> None:
        assert PlatformEnvelope(status="FAIL", errorCode=2001).is_auth_failure() is False

    def test_error_message_passthrough(self) -> None:
        envelope = PlatformEnvelope.from_json(OBSERVED_AUTH_FAILURE_BODY)
        assert envelope.error_message() == "驗證失敗"

    def test_error_message_fallback(self) -> None:
        assert PlatformEnvelope(status="FAIL").error_message() == "<服务端未提供描述>"

    def test_summary_contains_code_for_failure(self) -> None:
        summary = PlatformEnvelope.from_json(OBSERVED_AUTH_FAILURE_BODY).summary()
        assert "FAIL" in summary
        assert "4001" in summary

    def test_summary_for_success(self) -> None:
        assert PlatformEnvelope(status="SUCCESS").summary() == "status=SUCCESS"

    def test_unknown_fields_are_ignored(self) -> None:
        """网关将来新增字段不应导致解析失败。"""
        envelope = PlatformEnvelope.from_json('{"status":"SUCCESS","brandNewField":123}')
        assert envelope.is_success() is True

    # ---- serverTime（实测成功响应里才有）----
    def test_server_time_parsed_from_observed_success(self) -> None:
        envelope = PlatformEnvelope.from_json(OBSERVED_USER_INFO_SUCCESS_BODY)
        assert envelope.server_time == 1789893660724

    def test_server_time_utc_conversion(self) -> None:
        """毫秒时间戳必须按毫秒换算：1789893660724 → 2026-09-20 08:41:00 UTC。

        若错当秒处理，会得到 1970 年附近的时间，将来做时间戳签名时会一直失败。
        """
        envelope = PlatformEnvelope(serverTime=1789893660724)
        converted = envelope.server_time_utc()
        assert converted is not None
        assert converted.strftime("%Y-%m-%d %H:%M:%S") == "2026-09-20 08:41:00"

    def test_server_time_missing_returns_none(self) -> None:
        """失败响应里没有 serverTime，转换应返回 None 而不是抛异常。"""
        envelope = PlatformEnvelope.from_json(OBSERVED_AUTH_FAILURE_BODY)
        assert envelope.server_time_utc() is None
        assert envelope.clock_offset_seconds() is None

    def test_server_time_absurd_value_returns_none(self) -> None:
        """数值离谱时返回 None，绝不炸掉调用方。"""
        assert PlatformEnvelope(serverTime=10**30).server_time_utc() is None

    def test_clock_offset_is_small_for_fresh_response(self) -> None:
        """刚拿到的响应，本地与服务器时钟偏差应在秒级以内。

        这个能力日后很有用：若接口要求「请求时间戳必须在服务端 ±N 分钟内」，
        动手算签名之前先看一眼偏差，能避免大量「签名对着却一直被拒」的困惑。
        """
        envelope = PlatformEnvelope(serverTime=int(time.time() * 1000))
        offset = envelope.clock_offset_seconds()
        assert offset is not None
        assert abs(offset) < 5


# ---------------------------------------------------------------------------
# data 结构模型（字段全部来自实测响应）
# ---------------------------------------------------------------------------
class TestPlatformDataModels:
    """验证 camelCase → snake_case 映射、默认值与必填项。"""

    def test_platform_user_from_observed_response(self) -> None:
        user = PlatformUser.from_json(
            {
                "userId": "ER00000000-0000-0000-0000-000000000000",
                "coins": 30,
                "guest": False,
            }
        )
        assert user.user_id == "ER00000000-0000-0000-0000-000000000000"
        assert user.coins == 30
        assert user.guest is False

    def test_platform_user_accepts_python_field_names(self) -> None:
        """``populate_by_name=True``：Python 侧字段名同样可赋值。"""
        assert PlatformUser(user_id="u1").user_id == "u1"

    def test_platform_user_defaults(self) -> None:
        """只给必填的 userId 时，其余字段取安全默认值。"""
        user = PlatformUser(user_id="u1")
        assert user.coins == 0
        assert user.guest is False

    def test_platform_user_requires_user_id(self) -> None:
        """缺必填字段必须报错，不能静默产生一个没有身份的「用户对象」。"""
        with pytest.raises(PayloadError, match="字段校验失败"):
            PlatformUser.from_json({"coins": 1})

    def test_platform_user_serializes_with_alias_on_demand(self) -> None:
        """要按服务端字段名序列化时用 ``by_alias=True``（客户端默认输出 Python 名）。"""
        user = PlatformUser(user_id="u1", coins=5)
        assert user.model_dump(by_alias=True) == {
            "userId": "u1",
            "coins": 5,
            "guest": False,
        }

    def test_game_binding_status_from_observed_response(self) -> None:
        status = GameBindingStatus.from_json(
            {
                "isBound": True,
                "gameAccount": "ER00000000-0000-0000-0000-000000000000",
            }
        )
        assert status.is_bound is True
        assert status.game_account == "ER00000000-0000-0000-0000-000000000000"

    def test_game_binding_status_requires_both_fields(self) -> None:
        with pytest.raises(PayloadError, match="字段校验失败"):
            GameBindingStatus.from_json({"isBound": True})


# ---------------------------------------------------------------------------
# 联网测试（默认跳过，见模块文档的说明）
# ---------------------------------------------------------------------------
@pytest.mark.network
@requires_network
class TestPlatformNetwork:
    """对真实网关发起请求。只有显式设置 ``CHERRYTALE_RUN_NETWORK_TESTS=1`` 才运行。"""

    def test_gateway_is_reachable(self) -> None:
        """只验证「能拿到 HTTP 响应」。

        没有 Token 时返回 401/403 也算通过——因为我们要验证的是**网络可达**，
        而「服务端明确拒绝」恰恰证明请求确实到达了服务端。
        """
        with PlatformClient() as client:
            response = client.fetch(config.USER_INFO_PATH)
        assert 100 <= response.status_code < 600

    def test_user_info_rejects_missing_token(self) -> None:
        """没有令牌时会被网关拒绝，且异常类型必须是「鉴权」而不是「参数」。

        实测行为：HTTP 400 + ``{"status":"FAIL","errorCode":4001}``。
        注意**不是 401/403** —— 这正是「只看 HTTP 状态码」会出错的地方。
        """
        if config.has_auth_token():
            pytest.skip("本机已配置 Token，该断言只在「无 Token」场景下才有意义")

        with PlatformClient() as client:
            response = client.fetch(config.USER_INFO_PATH)
            assert response.status_code == 400

            envelope = PlatformEnvelope.from_json(response.content)
            assert envelope.is_failure() is True
            assert envelope.is_auth_failure() is True

            # 高层方法必须把它翻译成 AuthenticationError
            with pytest.raises(AuthenticationError):
                client.get_user_info()

    def test_get_user_info_with_token_returns_model(self) -> None:
        if not config.has_auth_token():
            pytest.skip("未配置 Token；请设 CHERRYTALE_AUTH_TOKEN 或写入 .auth_token")

        with PlatformClient() as client:
            user = client.get_user_info()

        assert isinstance(user, PlatformUser)
        assert user.user_id  # 真账号必有 userId
        assert isinstance(user.coins, int)

    def test_get_game_status_with_token_returns_model(self) -> None:
        if not config.has_auth_token():
            pytest.skip("未配置 Token；请设 CHERRYTALE_AUTH_TOKEN 或写入 .auth_token")

        with PlatformClient() as client:
            status = client.get_game_status()

        assert isinstance(status, GameBindingStatus)
        assert isinstance(status.is_bound, bool)

    def test_server_time_is_close_to_local_clock(self) -> None:
        """联网校验：服务器时间与本地时钟不应差太多（用于日后时间戳签名）。"""
        if not config.has_auth_token():
            pytest.skip("未配置 Token；请设 CHERRYTALE_AUTH_TOKEN 或写入 .auth_token")

        with PlatformClient() as client:
            envelope = client.get_envelope(config.USER_INFO_PATH)

        offset = envelope.clock_offset_seconds()
        assert offset is not None
        assert abs(offset) < 300  # 容忍 5 分钟，超过就说明本机时钟需要校准


# ---------------------------------------------------------------------------
# 脚本模式：python -m tests.test_platform
# ---------------------------------------------------------------------------
def _print_run_header(game_id: int) -> None:
    """打印本次要做什么、用什么配置（绝不打印 Token 本身）。"""
    print("=" * 74)
    print("Cherrytale 平台网关连通性测试（会发起真实网络请求）")
    print("=" * 74)
    print(f"  网关地址      : {config.PLATFORM_BASE_URL}")
    print(f"  用户信息接口  : GET {config.USER_INFO_PATH}")
    print(f"  绑定状态接口  : GET {config.game_status_path(game_id)}")
    print("  客户端身份    : WebView (Edge 153) —— 平台认证请求实际由内嵌浏览器发出")
    print(f"  来源页 Referer: {config.PLATFORM_LOGIN_REFERER}")
    print(f"  设备号        : {config.DEVICE_ID}")
    print(f"  鉴权字段名    : {config.AUTH_HEADER_NAME}")
    print(f"  Token 状态    : {'已配置' if config.has_auth_token() else '未配置 ⚠'}")

    if not config.has_auth_token():
        print()
        print("  ⚠ 尚未配置 Token —— 实测网关会返回 HTTP 400 且响应体为：")
        print('       {"status":"FAIL","message":"驗證失敗","errorCode":4001,"data":null}')
        print("    这仍能验证网络连通性：能拿到这段 JSON，就说明请求确实到达了服务端。")
        print("    配置方法（三选一）：")
        print("      export CHERRYTALE_AUTH_TOKEN='<your-token>'")
        print("      echo '<your-token>' > .auth_token")
        print("      在 config_local.py 中写：AUTH_TOKEN = '<your-token>'")
    print("=" * 74)


def main(argv: list[str] | None = None) -> int:
    """脚本入口：请求两个接口并打印响应。

    :return: ``0`` = 两个接口都拿到了 HTTP 响应（网络通）；
             ``1`` = 网络层失败（DNS/连接/超时，说明根本没通）。
    """
    force_utf8_output()  # 先修编码，否则中文在 Windows 管道里会变乱码

    parser = argparse.ArgumentParser(
        prog="tests.test_platform",
        description="平台网关连通性测试（会发起真实网络请求）",
    )
    parser.add_argument(
        "--game-id",
        type=int,
        default=None,
        help="查询绑定状态时使用的游戏 ID（默认取 config.DEFAULT_GAME_ID）",
    )
    parser.add_argument("--verbose", action="store_true", help="输出 DEBUG 级别日志")
    args = parser.parse_args(argv)

    game_id = config.DEFAULT_GAME_ID if args.game_id is None else args.game_id

    if args.verbose:
        logging.basicConfig(
            level=logging.DEBUG,
            format=config.LOG_FORMAT,
            datefmt=config.LOG_DATE_FORMAT,
        )

    _print_run_header(game_id)

    endpoints = (
        ("查询用户信息", config.USER_INFO_PATH),
        (f"查询游戏绑定（game_id={game_id}）", config.game_status_path(game_id)),
    )

    client = PlatformClient()
    print("\n将要发送的请求头（Token 已脱敏）：")
    for key, value in client.headers_preview().items():
        print(f"    {key}: {value}")

    network_ok = True
    try:
        for label, path in endpoints:
            try:
                response = client.fetch(path)
            except NetworkError as exc:
                # 关键区分：这一类是「根本没拿到响应」，与 401 完全不同
                network_ok = False
                print(f"\n{'=' * 74}")
                print(f"【{label}】")
                print(f"{'=' * 74}")
                print(f"  ✘ 网络层失败：{exc}")
                continue
            print_response(label, response)
    finally:
        client.close()

    print(f"\n{'=' * 74}")
    if network_ok:
        print("结论：网络连通 ✔")
        print("      说明：能拿到 HTTP 响应就代表域名解析、TLS 握手、请求发送全部正常。")
        print("      若响应为 HTTP 400 + errorCode 4001（驗證失敗），说明 Token 缺失或无效；")
        print("      配置有效 Token 后重跑本脚本，即可看到真实的用户信息与绑定状态。")
        return 0

    print("结论：网络层失败 ✘ —— 连 HTTP 响应都没拿到。")
    print("      排查方向：")
    print("        1. 本机网络 / 代理设置（含 config.HTTP_PROXY 或系统代理）；")
    print("        2. DNS 解析：该网关域名可能需自建 DNS 才能解析，")
    print("           参见 dump 中的 Game.Http.DnsUdpClient；")
    print("        3. 域名是否已变更（用抓包确认当前客户端的实际请求地址）。")
    return 1


if __name__ == "__main__":  # 支持 `python -m tests.test_platform`
    raise SystemExit(main())
