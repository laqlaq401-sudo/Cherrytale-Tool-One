"""``tools/safe_run.py`` 的行为测试 —— 它是"命令永不挂住终端"的**机器保证**。

运行方式：

    python -m pytest tests/test_tools_safe_run.py -q

【为什么这个工具必须有测试】
它的价值全在"**不会挂住**"这四个字上，而"不会挂住"是**无法靠人工观察**验证的
（观察到的是"这次没挂"，不是"永远不会挂"）。所以这里用三条可断言的性质把它钉住：

1. **stdin 必须是 DEVNULL**：读 stdin 的命令会立刻拿到 EOF，而不是把终端吊住
   —— 这是本次"终端永久 busy"事故最直接的根因，必须有一条回归断言；
2. **硬超时真的会杀进程**：超时返回 124 且**总耗时接近超时值**（而不是无限等）；
3. **子进程输出不进终端**：包装器自己的 stdout 只有十几行摘要（输出越少，PTY 越安全）。

另外 ``--bg`` 的登记/清点/收尾也要能跑通 —— 那是"服务器与无头浏览器怎么安全启动"的答案。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

#: 工程根（``tests/`` 的上一级）
PROJECT_ROOT = Path(__file__).resolve().parents[1]
#: 被测试的工具
TOOL = PROJECT_ROOT / "tools" / "safe_run.py"
#: 最近一次运行的摘要（工具保证会写它 —— 终端看不见输出时的逃生门）
LAST_SUMMARY = PROJECT_ROOT / "tmp" / "runs" / "last.json"
#: 解释器（用**同一个**解释器跑子进程，避免与 .venv 版本不一致）
PYTHON = sys.executable


def load_tool_module():
    """把 ``tools/safe_run.py`` 当模块加载（``tools/`` 不是包，只能按路径加载）。

    用途：直接检查**不启动子进程**的纯逻辑（例如 Windows 启动标志位），
    这样"会不会弹系统错误框"这类问题可以**零副作用**地回归。
    """
    from importlib.util import module_from_spec, spec_from_file_location

    spec = spec_from_file_location("safe_run_tool", TOOL)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_tool(*args: str, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    """调用工具本体（真实 CLI，不 mock —— 被测的就是这条路径）。

    .. important::
       **必须显式指定 UTF-8 解码**。Windows 上 ``text=True`` 默认按系统 ANSI
       代码页（本机是 GBK）解码，而本工具刻意统一输出 UTF-8（与 ``main.py``
       的 ``force_utf8_output()`` 一致）—— 不指定就会读到
       ``UnicodeDecodeError: 'gbk' codec can't decode byte …``，
       测试会以一种与业务毫无关系的姿势失败（本文件第一版就踩了这个坑）。
    """
    return subprocess.run(
        [PYTHON, str(TOOL), *args],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def last_summary() -> dict:
    """读工具写的摘要（同时验证"结果一定会落盘"这件事）。"""
    return json.loads(LAST_SUMMARY.read_text(encoding="utf-8"))


def log_text(payload: dict) -> str:
    """读摘要里指向的日志全文。"""
    return Path(str(payload["log"])).read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# ① 前台：退出码 / 输出落盘 / 摘要落盘
# ---------------------------------------------------------------------------
class TestForeground:
    def test_exit_code_and_log_are_captured(self) -> None:
        """子进程的退出码原样透传，输出**只进日志**（不刷到终端）。"""
        result = run_tool(
            "--label", "pytest-ok",
            "--",
            PYTHON, "-c", "print('HELLO-FROM-CHILD'); raise SystemExit(3)",
        )

        assert result.returncode == 3
        payload = last_summary()
        assert payload["exit_code"] == 3
        assert payload["timed_out"] is False
        # finished_at 是"这条命令到底跑了没有"的自证（终端失联时靠它判断）
        assert payload["finished_at"]
        assert "HELLO-FROM-CHILD" in log_text(payload)
        # 子进程的输出**不在**包装器的 stdout 里（它只回摘要 + 日志尾部几行）
        assert "HELLO-FROM-CHILD" not in result.stdout.split("[safe_run] log=")[0]

    def test_wrapper_stdout_stays_tiny(self) -> None:
        """包装器自己的 stdout 必须很短：它往 PTY 写得越少，shell 集成越不容易坏。"""
        result = run_tool(
            "--label", "pytest-loud",
            "--",
            PYTHON, "-c", "print('x' * 200); [print('line %d' % i) for i in range(500)]",
        )

        assert result.returncode == 0
        assert len(result.stdout.splitlines()) <= 15

    def test_stdin_is_devnull_so_readers_never_hang(self) -> None:
        """★ 根因回归：命令想读 stdin 时会**立刻**拿到 EOF，绝不吊住终端。

        【为什么这条最重要】
        本次事故里"终端被永久标记 busy"的直接原因就是有子进程在等输入。
        只要 stdin 是 ``DEVNULL``，"等输入"这件事在物理上就不可能发生。
        """
        result = run_tool(
            "--label", "pytest-stdin",
            "--",
            PYTHON, "-c",
            "import sys; data = sys.stdin.read(); print('READ[%r]' % data)",
            timeout=30.0,
        )

        assert result.returncode == 0, result.stderr
        assert "READ['']" in log_text(last_summary())

    def test_hard_timeout_kills_and_reports_124(self) -> None:
        """★ 超时必须**真的杀掉**进程并返回 124（而不是无限等下去）。"""
        result = run_tool(
            "--timeout", "2",
            "--label", "pytest-timeout",
            "--",
            PYTHON, "-c",
            # flush=True 是刻意加上的：子进程输出到**文件**时是块缓冲，
            # 被 taskkill 杀掉时还没落盘的字节会一起消失。这里要断言的是
            # "它确实跑起来了"，所以必须让它先把那行刷进日志。
            "import time; print('start', flush=True); time.sleep(120)",
        )

        assert result.returncode == 124
        payload = last_summary()
        assert payload["timed_out"] is True
        # 2 秒超时 + 杀进程收尾 ⇒ 远小于 sleep 的 120 秒
        assert payload["seconds"] < 30
        assert "start" in log_text(payload)  # 被杀之前确实跑起来了

    def test_json_mode_is_single_line(self) -> None:
        """``--json`` 只回一行 JSON（给程序/脚本用）。"""
        result = run_tool("--json", "--", PYTHON, "-c", "print('ok')")

        assert result.returncode == 0
        payload = json.loads(result.stdout.strip())
        assert payload["exit_code"] == 0

    def test_missing_command_is_rejected(self) -> None:
        """没给命令时要**明确报错**，而不是悄悄什么都不做。"""
        result = run_tool("--label", "pytest-empty")

        assert result.returncode != 0
        assert "缺少要执行的命令" in (result.stderr + result.stdout)


# ---------------------------------------------------------------------------
# ② 常驻进程（--bg / --list / --kill）：服务器与无头浏览器的正确起法
# ---------------------------------------------------------------------------
def _kill_quietly(pid: int) -> None:
    """测试收尾用：无论断言是否失败，都要把后台进程清掉。"""
    run_tool("--kill", str(pid))


class TestBackground:
    def test_bg_records_pid_and_kill_stops_it(self) -> None:
        """``--bg`` 立刻返回 PID；``--kill`` 之后该进程必须真的消失。"""
        started = run_tool(
            "--bg",
            "--json",
            "--label", "pytest-bg",
            "--",
            PYTHON, "-c", "import time; time.sleep(120)",
        )
        assert started.returncode == 0, started.stderr
        payload = json.loads(started.stdout.strip())
        pid = int(payload["pid"])
        assert pid > 0

        try:
            listed = run_tool("--list", "--json")
            items = json.loads(listed.stdout.strip())
            hit = next(item for item in items if int(item["pid"]) == pid)
            assert hit["alive"] is True
        finally:
            killed = run_tool("--kill", str(pid))

        assert "alive_after=False" in killed.stdout
        listed = run_tool("--list", "--json")
        items = json.loads(listed.stdout.strip())
        assert all(int(item["pid"]) != pid for item in items)

    def test_kill_all_clears_the_registry(self) -> None:
        """``--kill-all`` 把登记表里的常驻进程一次清干净。"""
        for label in ("pytest-bg-a", "pytest-bg-b"):
            started = run_tool(
                "--bg",
                "--json",
                "--label", label,
                "--",
                PYTHON, "-c", "import time; time.sleep(120)",
            )
            assert started.returncode == 0, started.stderr

        killed = run_tool("--kill-all")
        assert killed.returncode == 0

        listed = run_tool("--list", "--json")
        items = json.loads(listed.stdout.strip())
        assert [item for item in items if item.get("alive")] == []

    def test_list_without_registry_is_not_an_error(self) -> None:
        """没有登记表时 ``--list`` 也要正常退出（首跑就会遇到这种情况）。"""
        result = run_tool("--list")

        assert result.returncode == 0


# ---------------------------------------------------------------------------
# ③ 参数与路径卫生
# ---------------------------------------------------------------------------
class TestArgumentHandling:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("pytest run/../x", "pytest-run----x"),
            ("web:edge", "web-edge"),
            ("", "run"),
        ],
    )
    def test_label_is_sanitised(self, raw: str, expected: str) -> None:
        """日志文件名里的标签必须被洗过（不能靠"命令行里没人乱填"来保证）。"""
        module = load_tool_module()

        assert module._safe_label(raw) == expected

    def test_explicit_log_path_is_honoured(self, tmp_path: Path) -> None:
        """``--log`` 指到哪就写哪（便于把某个长期跑的命令单独留档）。"""
        target = tmp_path / "explicit.log"
        result = run_tool(
            "--log", str(target),
            "--",
            PYTHON, "-c", "print('EXPLICIT-LOG')",
        )

        assert result.returncode == 0
        assert "EXPLICIT-LOG" in target.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# ④ 启动标志（★ 2026-10-02 的"弹窗"事故回归）
# ---------------------------------------------------------------------------
class TestSpawnFlags:
    """★ 这一组**不启动任何子进程**：只检查 Windows 启动标志位。

    【它守的是什么】
    第一版 ``--bg`` 用了 ``DETACHED_PROCESS``（"彻底不带控制台"），结果在
    Windows 上每次后台启动都弹出系统错误框：

        出现错误 2147942632 (0x800700e8)（启动 ".venv\\Scripts\\python.exe" … 时）

    ``0x800700E8 = ERROR_NO_DATA``：venv 的 ``python.exe`` 是个启动器，
    没有控制台时它会自己报错。正确做法是 ``CREATE_NO_WINDOW``（隐藏的新控制台）。
    这类问题的表现是**弹窗**而不是断言失败，所以必须用"查标志位"的方式钉住。
    """

    def test_foreground_never_detaches_and_has_own_group(self) -> None:
        """前台：自成一个进程组（便于整棵树收尾），但**绝不**脱离/新建窗口。"""
        module = load_tool_module()
        kwargs = module.spawn_kwargs(detach=False)

        assert kwargs["stdin"] is subprocess.DEVNULL
        if os.name == "nt":
            flags = kwargs["creationflags"]
            assert flags & subprocess.CREATE_NEW_PROCESS_GROUP
            assert flags & subprocess.DETACHED_PROCESS == 0
            assert flags & subprocess.CREATE_NEW_CONSOLE == 0
        else:
            assert kwargs["start_new_session"] is True

    def test_background_uses_no_window_not_detached(self) -> None:
        """后台：``CREATE_NO_WINDOW``（有隐藏控制台）而不是 ``DETACHED_PROCESS``。"""
        module = load_tool_module()
        kwargs = module.spawn_kwargs(detach=True)

        assert kwargs["stdin"] is subprocess.DEVNULL
        if os.name == "nt":
            flags = kwargs["creationflags"]
            # ★ 元凶：一旦这个位被点亮，用户电脑上就会弹 0x800700e8
            assert flags & subprocess.DETACHED_PROCESS == 0
            # 必须给"隐藏控制台"，否则 venv 启动器会报 ERROR_NO_DATA
            assert flags & subprocess.CREATE_NO_WINDOW
            # 仍然要独立进程组：收尾时能一次杀干净
            assert flags & subprocess.CREATE_NEW_PROCESS_GROUP
            # 不能真的开一个可见控制台窗口
            assert flags & subprocess.CREATE_NEW_CONSOLE == 0
        else:
            assert kwargs["start_new_session"] is True


# ---------------------------------------------------------------------------
# ⑤ 体检（--doctor / --clean）：把"终端为什么卡"的证据机器化
# ---------------------------------------------------------------------------
#: 体检报告（与工具里的 ``DOCTOR_REPORT`` 对应 —— 故意重写一遍：测试不依赖实现细节）
DOCTOR_REPORT = PROJECT_ROOT / "tmp" / "runs" / "doctor.json"


class TestJsoncParsing:
    """设置文件是 JSONC —— 剥注释时绝不能误伤字符串里的 ``//``。"""

    def test_urls_survive_comment_stripping(self) -> None:
        module = load_tool_module()
        text = '{\n  // 行注释\n  "a": "https://example.com/x", /* 块注释 */\n  "b": [1, 2,],\n}'

        parsed = json.loads(module._strip_jsonc(text))

        assert parsed["a"] == "https://example.com/x"
        assert parsed["b"] == [1, 2]

    def test_escaped_quote_does_not_break_state_machine(self) -> None:
        """``"say \\"//\\" ok"`` 里的引号是转义过的，不能被当成字符串结束。"""
        module = load_tool_module()

        parsed = json.loads(module._strip_jsonc('{"a": "say \\"//\\" ok"}'))

        assert parsed["a"] == 'say "//" ok'


class TestPruneRegistry:
    def test_dead_entries_are_removed_alive_kept(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """★ ``--clean`` 只清**已死**的登记：把活着的进程清掉等于"管理工具杀错人"。"""
        module = load_tool_module()
        registry = tmp_path / "bg_pids.json"
        monkeypatch.setattr(module, "BG_REGISTRY", registry)
        registry.write_text(
            json.dumps(
                [
                    {"pid": os.getpid(), "label": "self", "argv": [], "log": "", "started": ""},
                    {"pid": 999999, "label": "ghost", "argv": [], "log": "", "started": ""},
                ]
            ),
            encoding="utf-8",
        )

        removed = module.prune_registry()

        assert removed == 1
        remaining = json.loads(registry.read_text(encoding="utf-8"))
        assert [item["label"] for item in remaining] == ["self"]


class TestDoctor:
    """``--doctor`` 必须**永远能给出结论**（连"设置读不到"都要降级成建议，而不是崩）。"""

    def test_json_report_is_written_and_readable(self) -> None:
        result = run_tool("--doctor", "--json")

        # 0 = 未命中已知坑；3 = 有告警（本机大概率是 3：Cline 默认 4000ms 本身就是"坑"）
        assert result.returncode in (0, 3), result.stderr
        payload = json.loads(result.stdout.strip())
        assert payload["spawn_probe"]["ok"] is True
        assert set(payload) >= {
            "checked_at",
            "spawn_probe",
            "leftovers",
            "cline",
            "vscode",
            "advice",
        }
        assert isinstance(payload["advice"], list)
        # 报告必须落盘：终端回显不可靠时，这是唯一的结论来源
        report = json.loads(DOCTOR_REPORT.read_text(encoding="utf-8"))
        assert report["checked_at"] == payload["checked_at"]
        assert report["exit_code"] == payload["exit_code"]

    def test_workspace_settings_are_effective(self) -> None:
        """★ 回归"工程级修法"：venv 靠环境变量注入 + 关掉持久化会话。

        这两项是 2026-10-03 终端事故的**工程侧**对策（见 ``notes/terminal_env.md``）：
        少了任何一条，``--doctor`` 都会重新报出对应建议。
        """
        result = run_tool("--doctor", "--json")

        payload = json.loads(result.stdout.strip())
        assert payload["vscode"]["venv_env_injected"] is True
        assert payload["vscode"]["persistent_sessions"] is False

    def test_clean_command_prunes_dead_records(self) -> None:
        """``--clean`` 走 CLI 也要能跑通（真实登记表里只剩已死条目时它必须无副作用）。"""
        result = run_tool("--clean")

        assert result.returncode == 0
        assert "clean" in result.stdout

