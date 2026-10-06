"""client 包单元测试：异常体系 / Header / 会话。

运行方式：

    python -m tests.test_client
    python -m pytest tests/test_client.py -q

【设计原则：本测试文件绝不产生任何网络流量】

做法是把底层传输层 ``session._http.request`` 替换成「假传输层」，
它只记录「你让我发什么」，然后返回一个手工构造的响应对象。

这样测试有三个好处：
1. 秒级完成，不需要联网；
2. 结果稳定，不会因为网络抖动而偶发失败；
3. 最重要的是——**不会真的向游戏服务器发包**，避免误操作账号。
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

import pytest
import requests

import config
from client.exceptions import (
    ApiError,
    AuthenticationError,
    CherrytaleError,
    NetworkError,
    NotConfirmedError,
    ProtocolError,
)
from client.headers import (
    build_headers,
    is_sensitive_header,
    redact_headers,
)
from client.session import (
    GameSession,
    describe_response,
    raise_for_status,
)
from crypto.sign import TokenStore


def make_response(
    *,
    status_code: int = 200,
    content: bytes = b"",
    headers: dict[str, str] | None = None,
) -> requests.Response:
    """手工构造一个 ``requests.Response``，不经过网络。

    这里直接给私有属性 ``_content`` 赋值是社区通行做法：
    ``Response.content`` 会原样返回 ``_content``，因此可以完全离线伪造响应。
    """
    response = requests.Response()
    response.status_code = status_code
    response._content = content
    response.headers.update(headers or {})
    response.elapsed = datetime.timedelta(milliseconds=12)
    return response


class FakeTransport:
    """假传输层：记录调用参数，返回预设响应或抛出预设异常。"""

    def __init__(
        self,
        response: requests.Response | None = None,
        error: Exception | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def request(self, **kwargs: Any) -> requests.Response:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        assert self.response is not None, "FakeTransport 需要预设一个响应对象"
        return self.response


# ---------------------------------------------------------------------------
# 异常体系
# ---------------------------------------------------------------------------
class TestExceptions:
    """异常必须能分层捕获，且报错信息要能直接指向排查方向。"""

    def test_all_inherit_from_base(self) -> None:
        for exc_type in (NetworkError, ProtocolError, NotConfirmedError):
            assert issubclass(exc_type, CherrytaleError)

    def test_authentication_error_is_api_error(self) -> None:
        """令牌失效本质也是一种业务错误，因此继承关系要成立。"""
        assert issubclass(AuthenticationError, ApiError)

    def test_api_error_message_contains_code(self) -> None:
        error = ApiError(1001, "参数错误")
        assert "1001" in str(error)
        assert "参数错误" in str(error)
        assert error.code == 1001

    def test_api_error_without_message(self) -> None:
        error = ApiError(500)
        assert "<无描述>" in str(error)

    def test_base_exception_catches_children(self) -> None:
        with pytest.raises(CherrytaleError):
            raise AuthenticationError(401, "token expired")


# ---------------------------------------------------------------------------
# Header 拼装与脱敏
# ---------------------------------------------------------------------------
class TestHeaders:
    def test_content_type_added(self) -> None:
        headers = build_headers(content_type="application/octet-stream")
        assert headers["Content-Type"] == "application/octet-stream"

    def test_token_requires_field_name(self) -> None:
        """只给 token 不给字段名必须报错——字段名不能猜。"""
        with pytest.raises(ValueError, match="必须同时提供"):
            build_headers(token="abc123")

    def test_token_field_without_token_also_errors(self) -> None:
        with pytest.raises(ValueError, match="必须同时提供"):
            build_headers(token_field="Authorization")

    def test_token_placed_into_given_field(self) -> None:
        headers = build_headers(token="abc123", token_field="X-Token")
        assert headers == {"X-Token": "abc123"}

    def test_empty_values_dropped(self) -> None:
        """值为空的 Header 会被部分服务端判为非法请求，直接丢弃。"""
        headers = build_headers(extra={"X-Empty": "", "X-Keep": "1"})
        assert "X-Empty" not in headers
        assert headers["X-Keep"] == "1"

    def test_extra_overrides_base(self) -> None:
        headers = build_headers(
            base={"Version": "1.0"},
            extra={"Version": "2.0"},
        )
        assert headers["Version"] == "2.0"

    def test_base_is_not_mutated(self) -> None:
        """基础模板不能被调用方意外污染。"""
        base = {"A": "1"}
        build_headers(base=base, extra={"B": "2"})
        assert base == {"A": "1"}

    def test_default_base_headers_is_empty_for_now(self) -> None:
        """真实 Header 尚未确认，默认模板必须是空的，避免误以为已经配好。"""
        from client.headers import BASE_HEADERS

        assert BASE_HEADERS == {}

    @pytest.mark.parametrize(
        "field",
        ["Authorization", "X-Token", "Cookie", "X-Sign", "X-App-Secret"],
    )
    def test_sensitive_detection(self, field: str) -> None:
        assert is_sensitive_header(field) is True

    def test_normal_field_is_not_sensitive(self) -> None:
        assert is_sensitive_header("Content-Type") is False

    def test_redact_hides_values_only(self) -> None:
        redacted = redact_headers(
            {"Authorization": "Bearer abc", "Content-Type": "application/json"}
        )
        assert redacted["Authorization"] == "***"
        assert redacted["Content-Type"] == "application/json"

    def test_redact_keeps_key_names(self) -> None:
        """字段名要保留，否则排查时无法判断「这个 Header 到底发了没有」。"""
        assert "Authorization" in redact_headers({"Authorization": "x"})


# ---------------------------------------------------------------------------
# URL 拼装
# ---------------------------------------------------------------------------
class TestSessionUrl:
    def test_relative_path_joined(self) -> None:
        session = GameSession("https://example.invalid")
        assert session.build_url("/api/login") == "https://example.invalid/api/login"

    def test_absolute_url_passthrough(self) -> None:
        """已经是完整 URL 时不应再拼接，否则会出现双域名。"""
        session = GameSession("https://example.invalid")
        url = session.build_url("https://cdn.invalid/asset")
        assert url == "https://cdn.invalid/asset"

    def test_subpath_in_base_is_preserved(self) -> None:
        """``urljoin`` 会吃掉 ``/api/v2`` 这类子路径，所以本模块用字符串拼接。"""
        session = GameSession("https://example.invalid/api/v2")
        assert session.build_url("login") == "https://example.invalid/api/v2/login"

    def test_trailing_slash_not_duplicated(self) -> None:
        session = GameSession("https://example.invalid/")
        assert session.build_url("/login") == "https://example.invalid/login"

    def test_missing_base_url_raises_clear_error(self, monkeypatch) -> None:
        """域名未配置时，必须在真正发请求前给出可操作的错误提示。"""
        monkeypatch.setattr(config, "API_HOST", "")
        session = GameSession()
        with pytest.raises(config.ConfigError, match="尚未配置服务端域名"):
            session.resolve_base_url()

    def test_explicit_base_url_overrides_config(self) -> None:
        session = GameSession("https://example.invalid")
        assert session.resolve_base_url() == "https://example.invalid"


# ---------------------------------------------------------------------------
# 请求组装（不产生网络流量）
# ---------------------------------------------------------------------------
class TestPrepareRequest:
    def make_session(self, **kwargs: Any) -> GameSession:
        kwargs.setdefault("timeout", 5.0)
        kwargs.setdefault("verify_tls", True)
        return GameSession("https://example.invalid", **kwargs)

    def test_method_is_uppercased(self) -> None:
        prepared = self.make_session().prepare_request("post", "/api/x")
        assert prepared["method"] == "POST"

    def test_url_and_timeout_included(self) -> None:
        prepared = self.make_session().prepare_request("GET", "/api/x")
        assert prepared["url"] == "https://example.invalid/api/x"
        assert prepared["timeout"] == 5.0
        assert prepared["verify"] is True

    def test_data_and_json_are_mutually_exclusive(self) -> None:
        with pytest.raises(ValueError, match="不能同时使用"):
            self.make_session().prepare_request(
                "POST", "/api/x", data=b"\x01", json_body={"a": 1}
            )

    def test_binary_data_supported(self) -> None:
        """protobuf 报文是二进制，必须能原样放进请求体。"""
        payload = bytes([0x08, 0x96, 0x01])
        prepared = self.make_session().prepare_request("POST", "/api/x", data=payload)
        assert prepared["data"] == payload

    def test_params_omitted_when_none(self) -> None:
        prepared = self.make_session().prepare_request("GET", "/api/x")
        assert "params" not in prepared

    def test_params_included_when_given(self) -> None:
        prepared = self.make_session().prepare_request(
            "GET", "/api/x", params={"page": 1}
        )
        assert prepared["params"] == {"page": 1}

    def test_no_token_header_when_store_empty(self) -> None:
        prepared = self.make_session(token_field="Authorization").prepare_request(
            "GET", "/api/x"
        )
        assert "Authorization" not in prepared["headers"]

    def test_token_header_added_when_configured(self) -> None:
        store = TokenStore()
        store.update(uid="u", token="tok123")
        session = self.make_session(token_store=store, token_field="X-Token")
        prepared = session.prepare_request("GET", "/api/x")
        assert prepared["headers"]["X-Token"] == "tok123"

    def test_token_without_field_raises(self) -> None:
        """有令牌却不知放哪个 Header —— 必须报错，不能静默丢弃。"""
        store = TokenStore()
        store.update(uid="u", token="tok123")
        session = self.make_session(token_store=store)  # 未给 token_field
        with pytest.raises(ValueError, match="未配置 token_field"):
            session.prepare_request("GET", "/api/x")

    def test_use_token_false_skips_token(self) -> None:
        store = TokenStore()
        store.update(uid="u", token="tok123")
        session = self.make_session(token_store=store, token_field="X-Token")
        prepared = session.prepare_request("GET", "/api/x", use_token=False)
        assert "X-Token" not in prepared["headers"]

    def test_extra_headers_merged(self) -> None:
        prepared = self.make_session(headers={"X-App": "cherry"}).prepare_request(
            "GET", "/api/x", headers={"X-One-Off": "1"}
        )
        assert prepared["headers"]["X-App"] == "cherry"
        assert prepared["headers"]["X-One-Off"] == "1"

    def test_content_type_passed_through(self) -> None:
        prepared = self.make_session().prepare_request(
            "POST", "/api/x", data=b"", content_type="application/octet-stream"
        )
        assert prepared["headers"]["Content-Type"] == "application/octet-stream"

    def test_no_proxy_by_default(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "HTTP_PROXY", "")
        prepared = self.make_session().prepare_request("GET", "/api/x")
        assert "proxies" not in prepared

    def test_proxy_applied_when_configured(self) -> None:
        """抓包调试需要把请求指向本机代理（Charles / Fiddler）。"""
        session = self.make_session(proxy="http://127.0.0.1:8888")
        prepared = session.prepare_request("GET", "/api/x")
        assert prepared["proxies"] == {
            "http": "http://127.0.0.1:8888",
            "https": "http://127.0.0.1:8888",
        }


# ---------------------------------------------------------------------------
# 发送与异常翻译（用假传输层，绝不联网）
# ---------------------------------------------------------------------------
class TestSend:
    def make_session(self, **kwargs: Any) -> GameSession:
        kwargs.setdefault("timeout", 5.0)
        kwargs.setdefault("verify_tls", True)
        return GameSession("https://example.invalid", **kwargs)

    def test_returns_response(self, monkeypatch) -> None:
        session = self.make_session()
        fake = FakeTransport(response=make_response(status_code=200, content=b"ok"))
        monkeypatch.setattr(session._http, "request", fake.request)

        response = session.get("/api/ping")
        assert response.status_code == 200
        assert response.content == b"ok"

    def test_transport_receives_prepared_kwargs(self, monkeypatch) -> None:
        session = self.make_session(headers={"X-App": "cherry"})
        fake = FakeTransport(response=make_response())
        monkeypatch.setattr(session._http, "request", fake.request)

        session.post(
            "/api/login", data=b"\x08\x01", content_type="application/octet-stream"
        )

        assert len(fake.calls) == 1
        call = fake.calls[0]
        assert call["method"] == "POST"
        assert call["url"] == "https://example.invalid/api/login"
        assert call["data"] == b"\x08\x01"
        assert call["headers"]["X-App"] == "cherry"
        assert call["timeout"] == 5.0

    def test_timeout_translated_to_network_error(self, monkeypatch) -> None:
        session = self.make_session()
        fake = FakeTransport(error=requests.Timeout("read timed out"))
        monkeypatch.setattr(session._http, "request", fake.request)

        with pytest.raises(NetworkError, match="请求超时"):
            session.get("/api/ping")

    def test_connection_error_translated_to_network_error(self, monkeypatch) -> None:
        session = self.make_session()
        fake = FakeTransport(error=requests.ConnectionError("dns failure"))
        monkeypatch.setattr(session._http, "request", fake.request)

        with pytest.raises(NetworkError, match="网络请求失败"):
            session.get("/api/ping")

    def test_network_error_keeps_original_cause(self, monkeypatch) -> None:
        """用 ``from exc`` 保留异常链，便于追溯底层真实原因。"""
        session = self.make_session()
        fake = FakeTransport(error=requests.ConnectionError("boom"))
        monkeypatch.setattr(session._http, "request", fake.request)

        with pytest.raises(NetworkError) as excinfo:
            session.get("/api/ping")
        assert isinstance(excinfo.value.__cause__, requests.ConnectionError)

    def test_missing_base_url_fails_before_sending(self, monkeypatch) -> None:
        """域名未配置时必须在发包前就报错 —— 一个包都不该发出去。"""
        monkeypatch.setattr(config, "API_HOST", "")
        session = GameSession(timeout=1.0)
        fake = FakeTransport(response=make_response())
        monkeypatch.setattr(session._http, "request", fake.request)

        with pytest.raises(config.ConfigError):
            session.get("/api/ping")
        assert fake.calls == []

    def test_debug_log_does_not_leak_token(self, monkeypatch, caplog) -> None:
        """调试日志必须脱敏：否则一次截图就会泄露登录凭据。"""
        store = TokenStore()
        store.update(uid="u", token="super-secret-token")
        session = self.make_session(token_store=store, token_field="X-Token")
        fake = FakeTransport(response=make_response())
        monkeypatch.setattr(session._http, "request", fake.request)

        with caplog.at_level(logging.DEBUG, logger="client.session"):
            session.get("/api/ping")

        assert caplog.text  # 确认确实产生了日志，否则本测试形同虚设
        assert "super-secret-token" not in caplog.text

    def test_context_manager_calls_close(self, monkeypatch) -> None:
        session = self.make_session()
        flags = {"closed": False}
        monkeypatch.setattr(
            session, "close", lambda: flags.__setitem__("closed", True)
        )

        with session as handle:
            assert handle is session
        assert flags["closed"] is True


# ---------------------------------------------------------------------------
# HTTP 状态码 → 本项目异常
# ---------------------------------------------------------------------------
class TestRaiseForStatus:
    def test_success_passes_silently(self) -> None:
        raise_for_status(make_response(status_code=200))  # 不抛异常即通过

    def test_redirect_passes(self) -> None:
        """小于 400 都算「拿到了响应」，3xx 由 requests 自行跟随。"""
        raise_for_status(make_response(status_code=302))

    @pytest.mark.parametrize("status", [401, 403])
    def test_auth_errors_raise_authentication_error(self, status: int) -> None:
        with pytest.raises(AuthenticationError):
            raise_for_status(
                make_response(status_code=status, content=b"token expired")
            )

    def test_server_error_raises_api_error(self) -> None:
        with pytest.raises(ApiError, match="code=500"):
            raise_for_status(make_response(status_code=500, content=b"oops"))

    def test_body_preview_is_truncated(self) -> None:
        """错误信息里只带一小段响应体，避免把整包数据刷满屏幕。"""
        with pytest.raises(ApiError) as excinfo:
            raise_for_status(make_response(status_code=400, content=b"x" * 500))
        assert len(str(excinfo.value)) < 300

    def test_empty_body_placeholder(self) -> None:
        with pytest.raises(ApiError, match="空响应体"):
            raise_for_status(make_response(status_code=400, content=b""))

    def test_binary_body_does_not_crash(self) -> None:
        """protobuf 响应体是二进制，即使无法解码也不能让程序崩掉。"""
        with pytest.raises(ApiError):
            raise_for_status(
                make_response(status_code=400, content=b"\xff\xfe\x00\x01")
            )


# ---------------------------------------------------------------------------
# 响应摘要
# ---------------------------------------------------------------------------
class TestDescribeResponse:
    def test_summary_contains_key_facts(self) -> None:
        response = make_response(
            status_code=200,
            content=b"12345",
            headers={"Content-Type": "application/octet-stream"},
        )
        text = describe_response(response)
        assert "HTTP 200" in text
        assert "5 字节" in text
        assert "application/octet-stream" in text

    def test_missing_content_type_placeholder(self) -> None:
        assert "<无 Content-Type>" in describe_response(make_response())

    def test_summary_does_not_include_body(self) -> None:
        """摘要绝不能包含响应体内容（里面可能有敏感字段）。"""
        text = describe_response(make_response(content=b"SECRET-BODY-CONTENT"))
        assert "SECRET-BODY-CONTENT" not in text


if __name__ == "__main__":  # 支持 `python -m tests.test_client`
    raise SystemExit(pytest.main([__file__, "-v", "--no-header"]))
