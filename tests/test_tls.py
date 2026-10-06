"""client.tls 单元测试：系统信任库注入的开关、幂等性与容错。

运行方式：

    python -m tests.test_tls
    python -m pytest tests/test_tls.py -q

【为什么这个小模块值得单独写测试】
因为它的失败方式是「静默」的：注入没生效时，代码照样能跑，
只有等到某个证书链恰好需要系统信任库时才报 SSL 错误 ——
而那时你会以为是网络问题。用测试把「开关、幂等、依赖缺失」三种情形钉死，
下次出问题就能立刻排除这一层。
"""

from __future__ import annotations

import builtins
import logging

import pytest

import client.tls as tls_module
import config
from client.tls import is_using_system_trust_store, use_system_trust_store


@pytest.fixture(autouse=True)
def reset_injection_flag(monkeypatch: pytest.MonkeyPatch):
    """每个用例前后都重置「已注入」标记。

    为什么必须重置：注入标记是模块级全局状态，一旦某个用例把它置为 True，
    后面的用例就会在「看似已注入」的假状态下运行，测出的结论全部不可信。
    """
    monkeypatch.setattr(tls_module, "_INJECTED", False)
    yield
    monkeypatch.setattr(tls_module, "_INJECTED", False)


class TestConfigFlag:
    def test_flag_enabled_by_default(self) -> None:
        assert config.USE_SYSTEM_TRUST_STORE is True

    def test_summary_exposes_flag(self) -> None:
        assert config.summary()["use_system_trust_store"] is True


class TestUseSystemTrustStore:
    def test_disabled_by_config_returns_false(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "USE_SYSTEM_TRUST_STORE", False)
        assert use_system_trust_store() is False
        assert is_using_system_trust_store() is False

    def test_enabled_returns_true(self) -> None:
        assert use_system_trust_store() is True
        assert is_using_system_trust_store() is True

    def test_is_idempotent(self) -> None:
        """重复调用应直接返回 True，不重复替换 ssl 对象。"""
        assert use_system_trust_store() is True
        assert use_system_trust_store() is True
        assert is_using_system_trust_store() is True

    def test_missing_truststore_only_warns(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``truststore`` 缺失时应「警告 + 返回 False」，绝不抛异常。

        理由：对不涉及那条证书链的请求，certifi 依然够用；
        让整个程序起不来属于过度反应。
        """
        real_import = builtins.__import__

        def fake_import(name: str, *args: object, **kwargs: object):
            if name == "truststore":
                raise ImportError("simulated missing truststore")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)

        with caplog.at_level(logging.WARNING, logger="client.tls"):
            assert use_system_trust_store() is False

        assert "未安装 truststore" in caplog.text
        assert is_using_system_trust_store() is False

    def test_game_session_records_flag(self) -> None:
        """会话对象应把结果记录下来，方便诊断时一眼看出走的是哪套信任库。"""
        from client.session import GameSession

        session = GameSession("https://example.invalid", timeout=1.0)
        try:
            assert session.using_system_trust_store is True
        finally:
            session.close()

    def test_verify_tls_still_enabled(self) -> None:
        """改用系统信任库**不等于**关闭校验 —— 证书仍然会被严格验证。"""
        assert config.VERIFY_TLS is True


if __name__ == "__main__":  # 支持 `python -m tests.test_tls`
    raise SystemExit(pytest.main([__file__, "-v", "--no-header"]))
