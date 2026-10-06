"""main 入口的单元测试：子命令分发、退出码、中文输出编码。

运行方式：

    python -m tests.test_main
    python -m pytest tests/test_main.py -q

【为什么要测「退出码」】
退出码是脚本之间沟通的语言。以后你要把登录写进 .bat / cron / GitHub Actions 时，
判断成败靠的就是它：0 表示成功、非 0 表示失败。
所以每个子命令的退出码都必须被钉死，不能随手改成别的数字。

【为什么要测「中文输出编码」】
Windows 默认 GBK，输出被重定向到管道时会写成 GBK 字节，
而 Git Bash 按 UTF-8 解析，于是满屏乱码。本项目的输出几乎全中文，
这个坑必须用测试挡住，否则以后有人删掉 ``force_utf8_output()`` 也没人发现。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import config
import main


def run_cli(*args: str) -> subprocess.CompletedProcess[bytes]:
    """在**子进程**里运行 main.py，返回原始字节输出。

    用子进程而不是直接调用 ``main.main()`` 的原因：编码问题只有真正
    跨越进程边界、经过管道时才会暴露，进程内调用是测不出来的。
    同时刻意剔除 ``PYTHONIOENCODING``，模拟用户最朴素的使用环境。
    """
    env = {key: value for key, value in os.environ.items() if key != "PYTHONIOENCODING"}
    return subprocess.run(
        [sys.executable, "main.py", *args],
        cwd=str(Path(main.__file__).resolve().parent),
        env=env,
        capture_output=True,
        timeout=120,
    )


# ---------------------------------------------------------------------------
# 命令行解析
# ---------------------------------------------------------------------------
class TestParser:
    def test_selftest_subcommand(self) -> None:
        assert main.build_parser().parse_args(["selftest"]).command == "selftest"

    def test_tasks_subcommand(self) -> None:
        assert main.build_parser().parse_args(["tasks"]).command == "tasks"

    def test_run_subcommand_takes_task_name(self) -> None:
        assert main.build_parser().parse_args(["run", "login"]).task == "login"

    def test_run_subcommand_accepts_account_selector(self) -> None:
        """``--account`` 是多账号的入口（序号 / 邮箱）。"""
        parsed = main.build_parser().parse_args(["run", "login", "--account", "2"])
        assert parsed.account == "2"
        assert main.build_parser().parse_args(["run", "login"]).account is None

    def test_accounts_subcommand_is_registered(self) -> None:
        assert main.build_parser().parse_args(["accounts"]).command == "accounts"

    def test_no_command_prints_help(self, capsys) -> None:
        """不给子命令时应打印帮助并返回 0，而不是报错。"""
        assert main.main([]) == 0
        assert "selftest" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 子命令行为与退出码
# ---------------------------------------------------------------------------
class TestCommands:
    def test_tasks_lists_placeholders(self, capsys) -> None:
        assert main.main(["tasks"]) == 0
        out = capsys.readouterr().out
        assert "login" in out
        assert "daily_box" in out
        assert "需登录" in out  # 每日领取类任务（mail / 通行证…）都需要登录态

    def test_run_unknown_task_returns_2(self, capsys) -> None:
        assert main.main(["run", "no-such-task"]) == 2
        assert "未找到任务" in capsys.readouterr().out

    def test_accounts_lists_saved_account(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        """``accounts`` 要能列出已保存账号（序号 + 邮箱 + 令牌状态）。

        这里用 **临时账号库**：绝不碰项目根目录那份真实记录。
        """
        from client.account_store import AccountStore, SavedAccount

        monkeypatch.setattr(config, "PLATFORM_ACCOUNTS_FILE", tmp_path / "accounts.json")
        AccountStore().upsert(
            SavedAccount(
                account="a@example.com",
                user_id="ER-TEST",
                access_token="tok",
                refresh_token="rt",
                game={"player_id": 1, "server_id": 167, "server_name": "宮本武藏"},
            )
        )

        assert main.main(["accounts"]) == 0
        out = capsys.readouterr().out
        assert "a@example.com" in out
        assert "1) " in out
        assert "#167 宮本武藏" in out

    def test_accounts_on_empty_store_hints(self, tmp_path, monkeypatch, capsys) -> None:
        """空的账号库不是错误：给出"先怎么登录"的提示并返回 0。"""
        monkeypatch.setattr(config, "PLATFORM_ACCOUNTS_FILE", tmp_path / "none.json")
        assert main.main(["accounts"]) == 0
        assert "账号库为空" in capsys.readouterr().out

    def test_run_dry_run_returns_0_and_sends_nothing(self, capsys) -> None:
        """``--dry-run`` 必须**离线**成功：只打印将要发送的字节。

        这条同时守着两件事：

        1. 退出码 0（演练成功也算成功，否则脚本里没法用它做前置检查）；
        2. **不发包** —— 它在本地和 CI 上都应该是"零网络流量"的。

        .. note::
           原先这条测的是「跑未实现的任务应返回 1」。但 login 与 handshake
           现在都已实现，剩下的 daily 需要登录态（会在更早的前置检查被拦），
           所以「未实现的占位任务」这个场景已经不存在了 ——
           改测演练模式，反而更有价值。
        """
        assert main.main(["run", "handshake", "--dry-run"]) == 0
        out = capsys.readouterr().out
        assert "未发送" in out
        assert "application/octet-stream" in out

    def test_run_unknown_task_returns_2(self, capsys) -> None:
        """任务名写错时返回 ``2``（与"任务失败"的 ``1`` 区分开）。"""
        assert main.main(["run", "不存在的任务"]) == 2
        out = capsys.readouterr().out
        assert "未找到任务" in out

    def test_selftest_returns_zero_when_environment_ready(self, capsys) -> None:
        if not all(config.material_status().values()):
            pytest.skip("素材未就位，跳过「环境就绪」整体判定")
        assert main.main(["selftest"]) == 0
        assert "环境就绪" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 各项检查函数本身
# ---------------------------------------------------------------------------
class TestChecks:
    def test_python_version_ok(self) -> None:
        ok, detail = main.check_python_version()
        assert ok is True
        assert "3." in detail

    def test_dependencies_ok(self) -> None:
        ok, detail = main.check_dependencies()
        assert ok is True
        assert "requests" in detail
        assert "cryptography" in detail

    def test_crypto_vectors_ok(self) -> None:
        ok, detail = main.check_crypto()
        assert ok is True
        assert "标准向量" in detail

    def test_materials_ok(self) -> None:
        ok, _ = main.check_materials()
        assert ok is True

    def test_config_check_does_not_fail_on_missing_host(self) -> None:
        """域名未配置只是「后续工作」，不该让自检失败。"""
        ok, _ = main.check_config()
        assert ok is True

    def test_task_registry_reports_placeholders(self) -> None:
        ok, detail = main.check_tasks()
        assert ok is True
        assert "login" in detail
        assert "daily_box" in detail


# ---------------------------------------------------------------------------
# 中文输出必须为 UTF-8
# ---------------------------------------------------------------------------
class TestUtf8Output:
    def test_tasks_output_decodes_as_utf8(self) -> None:
        result = run_cli("tasks")
        assert result.returncode == 0
        # 若不是 UTF-8 字节，这一步会直接抛 UnicodeDecodeError
        text = result.stdout.decode("utf-8")
        assert "已注册任务" in text

    def test_selftest_output_decodes_as_utf8(self) -> None:
        result = run_cli("selftest")
        text = result.stdout.decode("utf-8")
        assert "离线自检" in text

    def test_force_utf8_output_is_callable(self) -> None:
        """保护性测试：防止有人顺手删掉入口处的编码修复。"""
        assert callable(main.force_utf8_output)


if __name__ == "__main__":  # 支持 `python -m tests.test_main`
    raise SystemExit(pytest.main([__file__, "-v", "--no-header"]))
