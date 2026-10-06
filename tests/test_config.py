"""config 模块单元测试：路径推导、缺配置报错、机密不外泄。

运行方式：

    python -m tests.test_config
    python -m pytest tests/test_config.py -q

【为什么「配置」也值得写测试】
配置最容易出的问题不是崩溃，而是**静默出错**：
路径指向别处、机密值被打印进日志、缺了配置却假装成功。
这类问题往往几周后才以「奇怪结果」的形式暴露，届时极难定位。
所以下面重点验证三件事：路径推导正确、缺配置必须报错、快照绝不泄密。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import config


# ---------------------------------------------------------------------------
# 路径推导
# ---------------------------------------------------------------------------
class TestPaths:
    def test_project_root_is_config_directory(self) -> None:
        assert (config.PROJECT_ROOT / "config.py").is_file()

    def test_project_root_is_absolute(self) -> None:
        assert config.PROJECT_ROOT.is_absolute()

    def test_material_root_defaults_to_project_root(self) -> None:
        assert config.MATERIAL_ROOT == config.PROJECT_ROOT

    def test_dump_cs_path_shape(self) -> None:
        expected = config.MATERIAL_ROOT / "Cherrytale IL2CPP" / "dump.cs"
        assert config.DUMP_CS_PATH == expected

    def test_dummy_dll_dir_shape(self) -> None:
        expected = config.MATERIAL_ROOT / "Cherrytale IL2CPP" / "DummyDll"
        assert config.DUMMY_DLL_DIR == expected

    def test_text_asset_dir_shape(self) -> None:
        expected = config.MATERIAL_ROOT / "Cherrytale Asset" / "TextAsset"
        assert config.TEXT_ASSET_DIR == expected

    def test_material_status_keys(self) -> None:
        assert set(config.material_status()) == {"dump.cs", "DummyDll", "TextAsset"}

    def test_material_status_values_are_bool(self) -> None:
        for value in config.material_status().values():
            assert isinstance(value, bool)


# ---------------------------------------------------------------------------
# 缺配置必须报错（而不是静默用错误的值）
# ---------------------------------------------------------------------------
class TestApiBaseUrl:
    def test_raises_when_host_missing(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "API_HOST", "")
        with pytest.raises(config.ConfigError, match="尚未配置服务端域名"):
            config.api_base_url()

    def test_error_message_contains_actionable_hint(self, monkeypatch) -> None:
        """报错必须说明「该怎么办」，否则等于没说。"""
        monkeypatch.setattr(config, "API_HOST", "")
        with pytest.raises(config.ConfigError, match="CHERRYTALE_API_HOST"):
            config.api_base_url()

    def test_formats_url_when_configured(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "API_HOST", "api.example.com")
        monkeypatch.setattr(config, "API_SCHEME", "https")
        monkeypatch.setattr(config, "API_PORT", 443)
        assert config.api_base_url() == "https://api.example.com:443"

    def test_config_error_is_runtime_error(self) -> None:
        """继承 RuntimeError，便于调用方与普通 ValueError 区分开。"""
        assert issubclass(config.ConfigError, RuntimeError)


# ---------------------------------------------------------------------------
# 账号凭据
# ---------------------------------------------------------------------------
class TestCredentials:
    def test_no_credentials_by_default(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "ACCOUNT_UID", "")
        monkeypatch.setattr(config, "ACCOUNT_TOKEN", "")
        assert config.has_credentials() is False

    def test_requires_both_uid_and_token(self, monkeypatch) -> None:
        """只填一半不算已配置，避免带着不完整凭据去发包。"""
        monkeypatch.setattr(config, "ACCOUNT_UID", "u1")
        monkeypatch.setattr(config, "ACCOUNT_TOKEN", "")
        assert config.has_credentials() is False

    def test_configured_when_both_present(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "ACCOUNT_UID", "u1")
        monkeypatch.setattr(config, "ACCOUNT_TOKEN", "tok")
        assert config.has_credentials() is True


# ---------------------------------------------------------------------------
# 配置快照绝不泄密
# ---------------------------------------------------------------------------
class TestSummary:
    def test_summary_contains_expected_keys(self) -> None:
        expected = {
            "project_root",
            "material_root",
            "materials",
            "api_host",
            "api_base_url",
            "custom_dns_ip",
            "timeout",
            "max_retries",
            "verify_tls",
            "http_proxy",
            "credentials_configured",
            "debug",
            "log_level",
        }
        assert expected <= set(config.summary())

    def test_summary_never_leaks_token(self, monkeypatch) -> None:
        """最关键的一条：快照会被打印到屏幕/日志，绝不能含明文凭据。"""
        monkeypatch.setattr(config, "ACCOUNT_UID", "my-real-uid")
        monkeypatch.setattr(config, "ACCOUNT_TOKEN", "my-real-token")

        text = repr(config.summary())
        assert "my-real-uid" not in text
        assert "my-real-token" not in text
        assert config.summary()["credentials_configured"] is True

    def test_summary_marks_missing_host(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "API_HOST", "")
        assert config.summary()["api_host"] == "<未配置>"


# ---------------------------------------------------------------------------
# 环境变量覆盖（用子进程验证，避免污染当前进程的模块状态）
# ---------------------------------------------------------------------------
class TestMaterialRootEnvOverride:
    """素材换目录时，应能靠环境变量指定，而不必改代码。"""

    PROBE = "import config; print(config.MATERIAL_ROOT)"

    def test_material_root_can_be_overridden(self) -> None:
        custom = Path("D:/custom-material-root")
        env = {**os.environ, "CHERRYTALE_MATERIAL_ROOT": str(custom)}

        result = subprocess.run(
            [sys.executable, "-c", self.PROBE],
            env=env,
            cwd=str(config.PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )

        assert result.returncode == 0, result.stderr
        assert str(custom) in result.stdout

    def test_default_used_without_env_var(self) -> None:
        env = dict(os.environ)
        env.pop("CHERRYTALE_MATERIAL_ROOT", None)

        result = subprocess.run(
            [sys.executable, "-c", self.PROBE],
            env=env,
            cwd=str(config.PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )

        assert result.returncode == 0, result.stderr
        assert str(config.PROJECT_ROOT) in result.stdout


if __name__ == "__main__":  # 支持 `python -m tests.test_config`
    raise SystemExit(pytest.main([__file__, "-v", "--no-header"]))
