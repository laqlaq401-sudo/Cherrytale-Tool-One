"""tests/test_release_tool.py - tools/release.py 的单元测试。

验证发版打包工具的版本解析、版本检查、原子版本升级与环境体检逻辑。
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.release import (
    parse_semver_tuple,
    read_current_versions,
    RX_GRADLE_VCODE,
    RX_GRADLE_VNAME,
    RX_WEBAPI_VER,
    RX_INFO_FILEVERS,
    RX_INFO_PRODVERS,
    RX_INFO_FILE_STR,
    RX_INFO_PROD_STR,
    VersionSnapshot,
)


class TestVersionParsing:
    def test_parse_semver_tuple_beta(self) -> None:
        assert parse_semver_tuple("0.9.3-beta") == (0, 9, 3, 0)
        assert parse_semver_tuple("0.9.4-alpha.1") == (0, 9, 4, 0)

    def test_parse_semver_tuple_release(self) -> None:
        assert parse_semver_tuple("1.0.0") == (1, 0, 0, 0)
        assert parse_semver_tuple("2.1.15") == (2, 1, 15, 0)

    def test_parse_semver_tuple_invalid(self) -> None:
        with pytest.raises(ValueError, match="无法解析为语义化数字版本"):
            parse_semver_tuple("invalid_version")


class TestVersionSnapshot:
    def test_consistent_snapshot(self) -> None:
        snap = VersionSnapshot(
            gradle_code=4,
            gradle_name="0.9.3-beta",
            webapi_desktop="0.9.3-beta",
            webapi_android="0.9.3-beta",
            filevers=(0, 9, 3, 0),
            prodvers=(0, 9, 3, 0),
            file_version_str="0.9.3.0",
            prod_version_str="0.9.3-beta",
        )
        assert snap.is_consistent is True

    def test_inconsistent_snapshot(self) -> None:
        snap = VersionSnapshot(
            gradle_code=4,
            gradle_name="0.9.3-beta",
            webapi_desktop="0.9.2-beta",  # mismatched
            webapi_android="0.9.3-beta",
            filevers=(0, 9, 3, 0),
            prodvers=(0, 9, 3, 0),
            file_version_str="0.9.3.0",
            prod_version_str="0.9.3-beta",
        )
        assert snap.is_consistent is False


class TestCurrentWorkspaceVersions:
    def test_read_real_project_versions(self) -> None:
        snap = read_current_versions()
        assert snap.gradle_code >= 4
        assert snap.gradle_name == snap.webapi_desktop
        assert snap.gradle_name == snap.webapi_android
        assert snap.gradle_name == snap.prod_version_str
        assert snap.is_consistent is True


class TestAtomicBumpLogic:
    def test_bump_updates_all_contents(self, tmp_path: Path) -> None:
        gradle_file = tmp_path / "build.gradle"
        webapi_app = tmp_path / "app.py"
        webapi_android = tmp_path / "android_app.py"
        version_info = tmp_path / "file_version_info.txt"

        gradle_file.write_text('versionCode 4\nversionName "0.9.3-beta"', encoding="utf-8")
        webapi_app.write_text('APP_VERSION: Final[str] = "0.9.3-beta"', encoding="utf-8")
        webapi_android.write_text('APP_VERSION: Final[str] = "0.9.3-beta"', encoding="utf-8")
        version_info.write_text(
            "filevers=(0, 9, 3, 0)\n"
            "prodvers=(0, 9, 3, 0)\n"
            "StringStruct('FileVersion', '0.9.3.0')\n"
            "StringStruct('ProductVersion', '0.9.3-beta')",
            encoding="utf-8",
        )

        with patch("tools.release.FILE_GRADLE", gradle_file), \
             patch("tools.release.FILE_WEBAPI_APP", webapi_app), \
             patch("tools.release.FILE_WEBAPI_ANDROID", webapi_android), \
             patch("tools.release.FILE_VERSION_INFO", version_info):

            import tools.release as release_module
            ret = release_module.cmd_bump_version("0.9.4-beta", new_code=5, dry_run=False)
            assert ret == 0

            # 验证修改后内容
            g_txt = gradle_file.read_text(encoding="utf-8")
            assert "versionCode 5" in g_txt
            assert 'versionName "0.9.4-beta"' in g_txt

            w_txt = webapi_app.read_text(encoding="utf-8")
            assert 'APP_VERSION: Final[str] = "0.9.4-beta"' in w_txt

            a_txt = webapi_android.read_text(encoding="utf-8")
            assert 'APP_VERSION: Final[str] = "0.9.4-beta"' in a_txt

            v_txt = version_info.read_text(encoding="utf-8")
            assert "filevers=(0, 9, 4, 0)" in v_txt
            assert "prodvers=(0, 9, 4, 0)" in v_txt
            assert "StringStruct('FileVersion', '0.9.4.0')" in v_txt
            assert "StringStruct('ProductVersion', '0.9.4-beta')" in v_txt


class TestBumpVersionCalculation:
    def test_bump_patch_z(self) -> None:
        from tools.release import compute_bump_version
        assert compute_bump_version("0.9.4-beta", "z") == "0.9.5-beta"
        assert compute_bump_version("0.9.4-beta", "1") == "0.9.5-beta"
        assert compute_bump_version("0.9.4-beta", "patch") == "0.9.5-beta"
        assert compute_bump_version("1.2.3", "z") == "1.2.4"

    def test_bump_minor_y(self) -> None:
        from tools.release import compute_bump_version
        assert compute_bump_version("0.9.4-beta", "y") == "0.10.0-beta"
        assert compute_bump_version("0.9.4-beta", "2") == "0.10.0-beta"
        assert compute_bump_version("0.9.4-beta", "minor") == "0.10.0-beta"
        assert compute_bump_version("1.2.3", "y") == "1.3.0"

    def test_bump_major_x(self) -> None:
        from tools.release import compute_bump_version
        assert compute_bump_version("0.9.4-beta", "x") == "1.0.0-beta"
        assert compute_bump_version("0.9.4-beta", "3") == "1.0.0-beta"
        assert compute_bump_version("0.9.4-beta", "major") == "1.0.0-beta"
        assert compute_bump_version("1.2.3", "x") == "2.0.0"

    def test_bump_invalid_part(self) -> None:
        from tools.release import compute_bump_version
        with pytest.raises(ValueError, match="未知版本升级部位"):
            compute_bump_version("0.9.4-beta", "unknown")


class TestPythonDiscoveryAndProcess:
    def test_find_python_cmd_types(self) -> None:
        from tools.release import find_python_cmd
        cmd_314 = find_python_cmd("3.14")
        assert isinstance(cmd_314, list) and len(cmd_314) > 0
        cmd_311 = find_python_cmd("3.11")
        assert isinstance(cmd_311, list) and len(cmd_311) > 0

    def test_safe_run_handles_missing_command_gracefully(self) -> None:
        from tools.release import run_process_safely
        res = run_process_safely(["non_existent_command_12345_xyz"])
        assert res.returncode == 127
        assert "失败" in res.stderr

