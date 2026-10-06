"""统一命令执行器：让"命令**永不**挂住终端"成为默认行为。

【它修的到底是什么问题（2026-10-02 实测）】
Cline / VS Code 靠 **shell 集成**（bash 在命令结束后发出的 OSC 633 标记）判断
"这条命令跑完了没有"。只要有**任何进程还挂在同一条 PTY 上**，或者命令在**等输入**
（pager、`y/n` 提示、REPL、读 stdin），这个标记就永远不来 ⇒ 该终端被
**永久标记为忙**，随后每条命令都变成：

    Command completion could not be observed; ...
    [Shell integration did not report command completion ...]

而 Cline 只会"复用或新建"终端，于是**不停开新终端**，VS Code 越用越卡。
本次会话的触发点很明确：为了跑无头 Edge 与 web_server，用了
``(timeout 240 python web_server.py &)`` / ``(msedge --headless=new ... &)``
这类写法 —— **`> log 2>&1 &` 只把 fd 指向文件，子进程仍然是这条 shell 的子进程**
（同一控制台 / 同一进程组），GUI 程序与常驻服务会一直留着那个句柄。

【为什么这个工具能根治】
它把子进程**真正脱离**出去，再让一个**短命的父进程**负责等待与收尾：

======================  ========================================================
``stdin=DEVNULL``       命令不可能"等输入"（这正是最常见的隐形挂起）
``stdout→日志文件``     子进程的输出**从不进 PTY**，终端输出恒为十几行摘要
新会话 / 新进程组       子进程不在这条 shell 的进程组里，退出时不会拖住 PTY
硬超时 + 杀进程树       超时（默认 300s）自动 ``taskkill /T`` / ``killpg``
``--bg``                常驻进程（服务器 / 浏览器）立刻返回 PID，不留句柄
结果落盘 ``last.json``  即使终端完全不可见，也能用"读文件"拿到退出码与日志
======================  ========================================================

【用法（**所有可能挂住的命令都该走它**）】
::

    # ① 前台跑：输出只进日志，终端只回一行摘要（推荐用于 pytest / npx / 抓包脚本）
    python tools/safe_run.py --timeout 600 --label pytest -- .venv/Scripts/python.exe -m pytest tests -q

    # ② 常驻服务 / 无头浏览器：立刻返回，进程被真正脱离
    python tools/safe_run.py --bg --label web -- .venv/Scripts/python.exe web_server.py --port 8767

    # ③ 收尾：看清有哪些常驻进程、按 PID 关掉（含子进程）
    python tools/safe_run.py --list
    python tools/safe_run.py --kill 12345
    python tools/safe_run.py --kill-all
    python tools/safe_run.py --doctor     # 体检：进程启动自查 + 终端设置 + 遗留进程
    python tools/safe_run.py --clean      # 清掉登记表里已经死掉的条目

【退出码约定】
与原命令一致；**超时返回 124**（与 GNU ``timeout`` 相同），说明"该命令没跑完就被杀了"。

``--doctor`` 单独一套：``0`` = 没命中已知坑；``3`` = 有告警（设置偏小 / 带 --login …）；
``1`` = 硬故障（它自己都起不来子进程）。报告同时落盘 ``tmp/runs/doctor.json``。

.. note::
   本工具**刻意不做**的事：不读 stdin、不转发 stdin、不把子进程输出实时打到终端。
   这三件事正是"终端被永久标记 busy"的来源。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Sequence

#: 工程根目录（``tools/`` 的上一级；本脚本可以被任意 cwd 调用）
PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[1]

#: 运行产物目录（``tmp/`` 已被 .gitignore 忽略）
RUN_DIR: Final[Path] = PROJECT_ROOT / "tmp" / "runs"

#: 最近一次运行的摘要（**终端看不见时就读它**）
LAST_SUMMARY: Final[Path] = RUN_DIR / "last.json"

#: 所有 ``--bg`` 启动过的进程登记表（含 PID / 命令 / 日志路径）
BG_REGISTRY: Final[Path] = RUN_DIR / "bg_pids.json"

#: 摘要里回显的日志尾部行数（再多就是噪音）
DEFAULT_TAIL: Final[int] = 8

#: 默认硬超时（秒）—— 宁可被杀，也不让终端挂住
DEFAULT_TIMEOUT: Final[float] = 300.0

#: 超时的退出码（与 GNU timeout 一致）
TIMEOUT_EXIT_CODE: Final[int] = 124


def _force_utf8() -> None:
    """让本脚本自己的输出在 Windows 控制台也不炸（与 ``main.force_utf8_output`` 同理）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def _timestamp() -> str:
    """``20261002-221530`` 形式的时间戳（日志文件名用）。"""
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _safe_label(label: str) -> str:
    """把任意标签洗成安全的文件名片段（避免路径穿越与非法字符）。"""
    cleaned = "".join(ch if (ch.isalnum() or ch in "-_") else "-" for ch in label)
    return cleaned.strip("-")[:40] or "run"


def spawn_kwargs(*, detach: bool) -> dict[str, Any]:
    """子进程的启动参数：**这是"不挂住终端"的全部秘密**。

    :param detach: ``True`` 表示常驻进程（服务器 / 浏览器）。

    【逐项说明】
    - ``stdin=DEVNULL``：命令读不到输入 ⇒ 想"等你按回车"的命令会立刻 EOF 失败，
      而不是把终端挂在那儿；
    - ``stdout`` / ``stderr`` 由调用方指向日志文件（永远不指向 PTY）；
    - ``CREATE_NEW_PROCESS_GROUP``（Windows）/ ``start_new_session=True``（POSIX）：
      子进程自成会话 / 进程组，收尾时能**整棵树**一起杀；
    - ``cwd=PROJECT_ROOT``：与工程规则"命令首部统一 cd 到工程根"一致，
      免得 cwd 不同导致相对路径行为漂移。

    【★ 2026-10-02 修复：``detach`` 绝不能翻译成 ``DETACHED_PROCESS``】
    第一版为了"彻底不带控制台"用了 ``DETACHED_PROCESS``，结果在用户电脑上
    **每次后台启动都弹一个系统错误框**：

        出现错误 2147942632 (0x800700e8)
        （启动 "…\\.venv\\Scripts\\python.exe" -c "import time; time.sleep(120)" 时）

    ``0x800700E8`` 就是 ``ERROR_NO_DATA``（"没有可用的控制台 / 管道"）。
    为什么会炸：venv 里的 ``python.exe`` 不是真正的解释器，而是一个**启动器**
    （它还要再 ``CreateProcess`` 一次 base 解释器），在没有控制台的环境下启动时
    就会报这个错 —— 所以这是**"我们少给了一个控制台"**，不是用户的机器有问题。

    正确做法是 ``CREATE_NO_WINDOW``：给子进程**一个隐藏的新控制台**。
    它同样**不继承**我们的控制台（照样脱离终端、不会吊住 PTY），
    但不会因为没有控制台而报错，也不会在屏幕上显示任何窗口。
    """
    kwargs: dict[str, Any] = {
        "cwd": str(PROJECT_ROOT),
        "stdin": subprocess.DEVNULL,
        "stderr": subprocess.STDOUT,
        "close_fds": True,
    }
    if os.name == "nt":
        # ★ 只加"新进程组"，绝不加 DETACHED_PROCESS（理由见上面的 docstring）
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        if detach:
            flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True  # setsid()
    return kwargs



# ---------------------------------------------------------------------------
# 进程控制
# ---------------------------------------------------------------------------
def kill_tree(pid: int) -> None:
    """杀掉一个进程**及其整棵子进程树**（超时收尾与 ``--kill`` 共用）。"""
    if pid <= 0:
        return
    if os.name == "nt":
        # 同 is_alive：taskkill 的输出也随系统区域编码，解码失败会抛异常。
        # 这里不消费输出，故显式指定 utf-8 + replace，任何字节都能安全吞下。
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        return
    time.sleep(0.3)
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def is_alive(pid: int) -> bool:
    """进程是否还活着。

    .. warning::
       Windows 上**不能**用 ``os.kill(pid, 0)`` 判活 —— 在 Windows 里
       ``os.kill`` 的"信号"其实是**退出码**，会真的把进程杀掉。所以这里查
       ``tasklist``；POSIX 才用 ``os.kill(pid, 0)``。
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        # 【为什么不用 text=True 解码】
        # tasklist 的输出编码随系统区域而变（中文 Windows 是 GBK/CP936）。
        # text=True 会让 Python 按 locale（本机实际走 UTF-8 模式）去解码，
        # 一遇到中文提示行（如「没有运行的任务匹配指定标准。」）就抛
        # UnicodeDecodeError，使 is_alive 直接崩溃 —— 而它被 --list / --kill /
        # --kill-all / --clean 与 prune_registry 共用，一崩就是整片功能失效。
        # PID 恒为 ASCII 数字，故直接做 bytes 子串比对：无需解码、不受区域影响。
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            check=False,
        )
        return str(pid).encode("ascii") in result.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def tail_lines(log_path: Path, count: int) -> list[str]:
    """读日志尾部若干行（文件不存在时返回空列表，绝不抛错）。"""
    if count <= 0 or not log_path.exists():
        return []
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = text.splitlines()
    return lines[-count:]


def write_summary(payload: dict[str, Any]) -> None:
    """把本次运行的结论写进 ``tmp/runs/last.json``（原子替换，随时可读）。

    【为什么一定要落盘】
    本工具存在的理由就是"终端可能根本看不到输出"。把退出码与日志路径写进文件，
    无论 PTY 出了什么问题，都能用"读文件"拿到结论 —— 这是整个方案的逃生门。
    """
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    tmp_file = LAST_SUMMARY.with_suffix(".json.tmp")
    tmp_file.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    tmp_file.replace(LAST_SUMMARY)


def _print_line(text: str) -> None:
    """打印一行（编码失败时退化成 ASCII，绝不因为控制台编码而崩）。"""
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"))


def report(payload: dict[str, Any], *, tail: int, as_json: bool) -> None:
    """把摘要写到 stdout —— **刻意只输出十几行**：输出越少，PTY 越不可能被写坏。"""
    if as_json:
        _print_line(json.dumps(payload, ensure_ascii=False))
        return
    seconds = payload.get("seconds")
    duration = f"{float(seconds):.2f}s" if isinstance(seconds, (int, float)) else "?"
    _print_line(
        f"[safe_run] label={payload.get('label')} exit={payload.get('exit_code')} "
        f"timed_out={payload.get('timed_out')} {duration}"
    )
    _print_line(f"[safe_run] log={payload.get('log')}")
    if payload.get("pid"):
        _print_line(
            f"[safe_run] pid={payload['pid']} "
            f"(stop: python tools/safe_run.py --kill {payload['pid']})"
        )
    for line in tail_lines(Path(str(payload.get("log", ""))), tail):
        _print_line(f"  | {line}")



# ---------------------------------------------------------------------------
# 前台执行
# ---------------------------------------------------------------------------
def run_foreground(
    argv: Sequence[str], *, timeout: float, log_path: Path
) -> dict[str, Any]:
    """前台跑一条命令：输出只进日志，硬超时后**杀整棵树**。

    :return: 摘要字典（``exit_code`` / ``timed_out`` / ``seconds`` / ``log``）。

    【为什么"前台"也不会挂住终端】
    本函数是**唯一**的等待者，且 ``proc.wait(timeout=…)`` 保证等待有上界；
    子进程的 stdin 是 ``DEVNULL``、stdout 是文件 —— 它没有任何办法向这条 PTY
    索取或写入"需要人回应"的东西。
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        proc = subprocess.Popen(list(argv), stdout=log, **spawn_kwargs(detach=False))
        timed_out = False
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_tree(proc.pid)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                pass
            code = TIMEOUT_EXIT_CODE
    return {
        "exit_code": int(code),
        "timed_out": timed_out,
        "seconds": time.monotonic() - started,
        "log": str(log_path),
        #: 结束时刻。**它存在的唯一理由**：终端失联时（命令其实压根没被派发到
        #: 一条能用的终端上），只要读 ``last.json`` 看这个字段有没有更新，
        #: 就能立刻判断"这条命令到底跑了没有" —— 不要凭终端里那行
        #: "could not be observed" 猜。
        "finished_at": datetime.now().isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------------------
# 常驻进程（--bg）：登记表 + 清点 + 收尾
# ---------------------------------------------------------------------------
def load_registry() -> list[dict[str, Any]]:
    """读常驻进程登记表（不存在或损坏时返回空表）。"""
    if not BG_REGISTRY.exists():
        return []
    try:
        data = json.loads(BG_REGISTRY.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [item for item in data if isinstance(item, dict)]


def save_registry(items: Sequence[dict[str, Any]]) -> None:
    """写回登记表。"""
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    BG_REGISTRY.write_text(
        json.dumps(list(items), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def start_background(
    argv: Sequence[str], *, label: str, log_path: Path
) -> dict[str, Any]:
    """``--bg``：启动常驻进程并立刻返回（**不等待、不继承控制台**）。"""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("w", encoding="utf-8", errors="replace")
    try:
        proc = subprocess.Popen(list(argv), stdout=log, **spawn_kwargs(detach=True))
    finally:
        # 父进程关掉**自己的**文件句柄：子进程持有自己的副本，日志照常写。
        log.close()
    items = load_registry()
    items.append(
        {
            "pid": int(proc.pid),
            "label": label,
            "argv": list(argv),
            "log": str(log_path),
            "started": datetime.now().isoformat(timespec="seconds"),
        }
    )
    save_registry(items)
    return {
        "exit_code": 0,
        "timed_out": False,
        "seconds": 0.0,
        "log": str(log_path),
        "pid": int(proc.pid),
        "label": label,
        "background": True,
        #: 常驻进程没有"结束时刻"（它就该一直活着）；这里给 None 是为了让
        #: ``last.json`` 的形状统一 —— 读它的人不必为两种形状写两套代码。
        "finished_at": None,
    }


def list_background() -> list[dict[str, Any]]:
    """登记表里每个进程的当前状态（``alive`` 现场查，不缓存）。"""
    return [
        {**item, "alive": is_alive(int(item.get("pid", 0)))}
        for item in load_registry()
    ]


def kill_background(pid: int) -> bool:
    """关掉一个登记的常驻进程（连子进程树），并从登记表移除。

    :return: ``True`` 表示关完确认已经不在（进程本来就不在了也算 ``True``）。
    """
    kill_tree(pid)
    still = [item for item in load_registry() if int(item.get("pid", 0)) != pid]
    save_registry(still)
    return not is_alive(pid)


# ---------------------------------------------------------------------------
# 体检（--doctor / --clean）：把"终端为什么卡"的证据一次收齐
# ---------------------------------------------------------------------------
#: 体检报告落盘路径 —— 与 ``last.json`` 同理：终端回显不可靠时靠**读文件**拿结论
DOCTOR_REPORT: Final[Path] = RUN_DIR / "doctor.json"

#: Cline 扩展 id。VS Code 把每个扩展的 global-state 整块存进 ``ItemTable``，
#: key 就是它 —— ``shellIntegrationTimeout`` / ``terminalReuseEnabled`` 都在里面，
#: **不在** ``~/.cline/data/settings/global-settings.json``。
CLINE_EXTENSION_ID: Final[str] = "saoudrizwan.claude-dev"

#: Cline 的 shell integration 超时**默认值**：源码写死 ``r||4e3``（没配置就是它）。
#: 语义 = "新终端必须在这么多毫秒内把 shell integration 准备好"；等不到，就再也不会有
#: "命令完成"事件（症状与取证见 ``notes/terminal_env.md``）。
CLINE_DEFAULT_SHELL_TIMEOUT_MS: Final[int] = 4000

#: 建议值。为什么比默认大这么多：本机 Git Bash 是 ``--login -i``（要跑 /etc/profile），
#: 每个终端还要再执行一次 venv 激活，就绪经常越过 4 秒。
CLINE_RECOMMENDED_SHELL_TIMEOUT_MS: Final[int] = 15000

#: 候选的 VS Code 系编辑器用户目录名（按顺序取第一个存在的 ``state.vscdb``）
_VSCODE_APP_DIRS: Final[tuple[str, ...]] = (
    "Code",
    "Code - Insiders",
    "Cursor",
    "VSCodium",
    "Windsurf",
)


def _strip_jsonc(text: str) -> str:
    """剥掉 JSONC 的注释与尾逗号，**字符串里的 ``//`` 一个都不许动**。

    【为什么值得写这一段状态机】
    VS Code 的 ``settings.json`` 允许注释，而设置里出现 ``"https://…"`` 是常事。
    用朴素正则砍 ``//`` 会把 URL 变成 ``"https:`` —— 那种错误会让"Git Bash 是否带
    --login"这类判断悄悄变成噪声，反而更难查。
    """
    out: list[str] = []
    index, size = 0, len(text)
    in_string = False
    while index < size:
        char = text[index]
        if in_string:
            out.append(char)
            if char == "\\" and index + 1 < size:  # 转义：连下一个字符一起原样吃
                out.append(text[index + 1])
                index += 2
                continue
            if char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < size and text[index + 1] == "/":
            while index < size and text[index] not in "\r\n":
                index += 1
            continue
        if char == "/" and index + 1 < size and text[index + 1] == "*":
            index += 2
            while index + 1 < size and not (
                text[index] == "*" and text[index + 1] == "/"
            ):
                index += 1
            index += 2
            continue
        out.append(char)
        index += 1
    # 尾逗号：``{"a": 1,}`` 在 JSONC 合法、在 ``json.loads`` 不合法
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def _load_jsonc(path: Path | None) -> dict[str, Any]:
    """读一个可能是 JSONC 的设置文件；读不到就返回空 dict。

    「读不到不该让体检崩」是刻意的：体检的价值在于**报出剩下的问题**，
    而不是因为某个路径不存在就整体失败。
    """
    if path is None or not path.exists():
        return {}
    text = path.read_text(encoding="utf-8", errors="replace")
    for candidate in (text, _strip_jsonc(text)):
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return {}


def _user_settings_path() -> Path | None:
    """``%APPDATA%/Code/User/settings.json``（拿不到 APPDATA 时返回 ``None``）。"""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return Path(appdata) / "Code" / "User" / "settings.json"


def _state_db_candidates() -> list[Path]:
    """候选的 VS Code global-state 数据库（扩展的 global-state 就在里面）。"""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return []
    root = Path(appdata)
    return [
        root / name / "User" / "globalStorage" / "state.vscdb"
        for name in _VSCODE_APP_DIRS
    ]


def read_cline_terminal_settings() -> dict[str, Any]:
    """读 Cline 里与本事故直接相关的几个终端设置（**只读**，绝不写用户编辑器状态）。

    :return: ``{"state_db", "values", "file_values", "error", "global_settings_file"}``。
        ``values`` 里缺某个键 = **用户没设置过**（例如缺 ``shellIntegrationTimeout``
        就意味着生效值是 :data:`CLINE_DEFAULT_SHELL_TIMEOUT_MS` = 4000ms）。
    """
    report: dict[str, Any] = {
        "state_db": None,
        "values": {},
        "error": None,
        "global_settings_file": None,
    }
    for db_path in _state_db_candidates():
        if not db_path.exists():
            continue
        report["state_db"] = str(db_path)
        try:
            # ``mode=ro``：只读打开，绝不是"去改 VS Code 的状态数据"
            connection = sqlite3.connect(
                f"file:{db_path}?mode=ro", uri=True, timeout=1.0
            )
        except sqlite3.Error as exc:
            report["error"] = f"打开失败：{exc}"
            continue
        try:
            row = connection.execute(
                "SELECT value FROM ItemTable WHERE key = ?", (CLINE_EXTENSION_ID,)
            ).fetchone()
        except sqlite3.Error as exc:
            report["error"] = f"查询失败：{exc}"
            row = None
        finally:
            connection.close()
        if not row:
            report["error"] = "该扩展还没有条目（可能从未打开过 Cline）"
            continue
        try:
            state = json.loads(row[0])
        except (TypeError, json.JSONDecodeError) as exc:
            report["error"] = f"global-state 不是合法 JSON：{exc}"
            continue
        if isinstance(state, dict):
            report["values"] = {
                key: state[key]
                for key in (
                    "shellIntegrationTimeout",
                    "terminalReuseEnabled",
                    "terminalOutputLineLimit",
                    "defaultTerminalProfile",
                )
                if key in state
            }
        break
    cline_global = Path.home() / ".cline" / "data" / "settings" / "global-settings.json"
    file_values: dict[str, Any] = {}
    if cline_global.exists():
        report["global_settings_file"] = str(cline_global)
        # 也读这份（Cline v4 的"全局设置"文件）：不同版本/入口写的地方不一样，
        # 两个来源都看，才能保证"用户改完 doctor 一定看得见"，而不是只能靠 UI 猜。
        file_data = _load_jsonc(cline_global)
        file_values = {
            key: file_data[key]
            for key in (
                "shellIntegrationTimeout",
                "terminalReuseEnabled",
                "terminalOutputLineLimit",
                "defaultTerminalProfile",
            )
            if key in file_data
        }
    report["file_values"] = file_values
    if file_values:
        # 扩展 global-state 优先（那是 IDE 真正生效的一份），文件只作补充
        report["values"] = {**file_values, **(report["values"] or {})}
    return report


def prune_registry() -> int:
    """把登记表里**已经死掉**的条目清掉（存活的一律保留），返回清掉的条数。"""
    items = load_registry()
    alive = [item for item in items if is_alive(int(item.get("pid", 0)))]
    removed = len(items) - len(alive)
    if removed:
        save_registry(alive)
    return removed


def _git_bash_uses_login(settings: dict[str, Any]) -> bool:
    """用户设置里的 Git Bash profile 是否带 ``--login``（要跑 ``/etc/profile``，拖慢就绪）。"""
    profiles = settings.get("terminal.integrated.profiles.windows")
    if not isinstance(profiles, dict):
        return False
    for name, profile in profiles.items():
        if "bash" not in str(name).lower() or not isinstance(profile, dict):
            continue
        args = profile.get("args")
        if isinstance(args, list) and "--login" in [str(item) for item in args]:
            return True
    return False


def _workspace_settings_path() -> Path:
    """本工程的 ``.vscode/settings.json``（体检要看"venv 注入"有没有配上）。"""
    return PROJECT_ROOT / ".vscode" / "settings.json"


def run_doctor(*, as_json: bool) -> int:
    """``--doctor``：自查进程启动 + 环境体检。

    :return: ``0``（没命中已知坑）/ ``3``（有告警）/ ``1``（硬故障：连子进程都起不来）。

    【为什么"起一个子进程"也算体检项】
    终端事故里最难判断的是"这条命令到底跑了没有"。本工具既然号称"一定能跑"，
    就该**当场自证**：起一个 ``print`` 子进程，把退出码 / 耗时 / 日志路径写进报告。
    它的输出同样只进日志 —— 体检本身也不许往 PTY 里倒东西。
    """
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    probe_log = RUN_DIR / f"{_timestamp()}-doctor-probe.log"
    probe = run_foreground(
        [sys.executable, "-c", "print('doctor-probe-ok')"],
        timeout=60.0,
        log_path=probe_log,
    )
    probe_ok = int(probe["exit_code"]) == 0

    leftovers = list_background()
    alive = [item for item in leftovers if item.get("alive")]

    user_path = _user_settings_path()
    user_settings = _load_jsonc(user_path)
    workspace_settings = _load_jsonc(_workspace_settings_path())
    cline = read_cline_terminal_settings()
    values = cline.get("values") or {}
    configured_timeout = values.get("shellIntegrationTimeout")
    reuse = values.get("terminalReuseEnabled")

    # VS Code 默认 true；工作区设置优先于用户设置
    persistent = workspace_settings.get(
        "terminal.integrated.enablePersistentSessions",
        user_settings.get("terminal.integrated.enablePersistentSessions", True),
    )
    env_injection = workspace_settings.get("terminal.integrated.env.windows")
    git_bash_login = _git_bash_uses_login(user_settings)

    advice: list[str] = []
    if configured_timeout is None:
        advice.append(
            "Cline「Shell integration timeout」未设置 → 生效默认 "
            f"{CLINE_DEFAULT_SHELL_TIMEOUT_MS}ms；建议在 Cline 设置里搜 shell，"
            f"改成 {CLINE_RECOMMENDED_SHELL_TIMEOUT_MS}ms"
        )
    elif int(configured_timeout) < CLINE_RECOMMENDED_SHELL_TIMEOUT_MS:
        advice.append(
            f"Cline「Shell integration timeout」={configured_timeout}ms 偏小，"
            f"建议 ≥ {CLINE_RECOMMENDED_SHELL_TIMEOUT_MS}ms"
        )
    if reuse is None or bool(reuse):
        advice.append(
            "Cline「Terminal reuse」在开启（默认 true）：忙终端被复用会把输入吃掉"
            "（实测 cd 只进去 d）→ 建议关掉"
        )
    if git_bash_login:
        advice.append(
            "VS Code 的 Git Bash profile 带 --login（要跑 /etc/profile）→ 建议只留 -i"
        )
    if not env_injection:
        advice.append(
            "本工程没有 terminal.integrated.env.windows → 每个终端都跑一次 activate，"
            "会拖慢 shell integration 就绪"
        )
    if persistent is not False:
        advice.append(
            "terminal.integrated.enablePersistentSessions 不是 false → 重载窗口会复活一堆终端"
        )
    if alive:
        pids = "、".join(str(item.get("pid")) for item in alive)
        advice.append(
            f"有 {len(alive)} 个存活的常驻进程（{pids}）→ 需要时 python tools/safe_run.py --kill-all"
        )
    if not probe_ok:
        advice.append(
            "自身启动子进程失败 → 先 --kill-all，再执行 Terminal: Kill All Terminals 后重试"
        )

    payload: dict[str, Any] = {
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "spawn_probe": {
            "ok": probe_ok,
            "exit_code": probe["exit_code"],
            "seconds": round(float(probe["seconds"]), 2),
            "log": probe["log"],
        },
        "leftovers": {
            "alive": [
                {"pid": item.get("pid"), "label": item.get("label")} for item in alive
            ],
            "dead_records": len(leftovers) - len(alive),
        },
        "cline": cline,
        "vscode": {
            "user_settings": str(user_path or ""),
            "workspace_settings": str(_workspace_settings_path()),
            "git_bash_uses_login": git_bash_login,
            "persistent_sessions": persistent,
            "venv_env_injected": bool(env_injection),
        },
        "advice": advice,
        "exit_code": 1 if not probe_ok else (3 if advice else 0),
    }
    DOCTOR_REPORT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    if as_json:
        _print_line(json.dumps(payload, ensure_ascii=False))
        return int(payload["exit_code"])

    probe_state = "OK" if probe_ok else "FAIL"
    _print_line(
        f"[doctor] 自查子进程启动：{probe_state}（{payload['spawn_probe']['seconds']}s，"
        f"日志 {payload['spawn_probe']['log']}）"
    )
    _print_line(
        f"[doctor] 常驻遗留：存活 {len(alive)} / 已死登记 {payload['leftovers']['dead_records']}"
    )
    _print_line(
        f"[doctor] Cline：shellIntegrationTimeout={configured_timeout}"
        f"（未设置则默认 {CLINE_DEFAULT_SHELL_TIMEOUT_MS}） terminalReuseEnabled={reuse}"
    )
    _print_line(f"[doctor] 报告：{DOCTOR_REPORT}")
    if advice:
        _print_line(f"[doctor] ⚠️ {len(advice)} 条建议：")
        for line in advice:
            _print_line(f"  - {line}")
    else:
        _print_line("[doctor] ✅ 未命中已知坑")
    return int(payload["exit_code"])


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器（用法见模块 docstring）。"""
    parser = argparse.ArgumentParser(
        prog="safe_run.py",
        description=(
            "在**脱离终端**的子进程里执行命令：输出只进日志、硬超时、"
            "结果写 tmp/runs/last.json（防止终端被永久标记 busy）"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"硬超时秒数，超时杀整棵进程树并返回 {TIMEOUT_EXIT_CODE}（默认 {DEFAULT_TIMEOUT:g}）",
    )
    parser.add_argument(
        "--label",
        default="run",
        help="这次运行的标签（日志文件名用，例如 pytest / web / edge）",
    )
    parser.add_argument(
        "--log",
        default=None,
        help="日志文件路径（默认 tmp/runs/<时间戳>-<标签>.log）",
    )
    parser.add_argument(
        "--tail",
        type=int,
        default=DEFAULT_TAIL,
        help=f"摘要里回显日志末尾几行（默认 {DEFAULT_TAIL}）",
    )
    parser.add_argument(
        "--json", action="store_true", help="只输出一行 JSON 摘要（便于程序解析）"
    )
    parser.add_argument(
        "--bg",
        action="store_true",
        help="后台常驻（服务器 / 浏览器）：立刻返回 PID，进程完全脱离终端",
    )
    parser.add_argument("--list", action="store_true", help="列出登记过的常驻进程")
    parser.add_argument(
        "--kill",
        type=int,
        action="append",
        default=[],
        metavar="PID",
        help="关掉某个常驻进程（可给多次）",
    )
    parser.add_argument(
        "--kill-all", action="store_true", help="关掉登记表里的全部常驻进程"
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help=(
            "环境体检：子进程启动自查 + 遗留常驻进程 + Cline/VS Code 终端设置"
            "（报告 tmp/runs/doctor.json；退出码 0 正常 / 3 有告警 / 1 硬故障）"
        ),
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="把登记表里已经死掉的条目清掉（存活的不动）",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="要执行的命令（建议写成：-- <命令> <参数…>）",
    )
    return parser


def parse_command(raw: Sequence[str]) -> list[str]:
    """把 ``REMAINDER`` 洗成纯命令（吃掉分隔用的 ``--``）。"""
    args = list(raw)
    if args and args[0] == "--":
        args = args[1:]
    return args


def _log_path(label: str, explicit: str | None) -> Path:
    """决定日志路径：显式给了就用它，否则 ``tmp/runs/<时间戳>-<标签>.log``。"""
    if explicit:
        return Path(explicit).expanduser().resolve()
    return RUN_DIR / f"{_timestamp()}-{_safe_label(label)}.log"


def _report_list(as_json: bool) -> int:
    """``--list``：打印登记表（含 alive 状态）。"""
    items = list_background()
    if as_json:
        _print_line(json.dumps(items, ensure_ascii=False))
    else:
        if not items:
            _print_line("[safe_run] 没有登记过的常驻进程")
        for item in items:
            flag = "alive" if item.get("alive") else "dead "
            _print_line(
                f"[safe_run] pid={item.get('pid')} {flag} label={item.get('label')} "
                f"log={item.get('log')}"
            )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """入口：解析参数并执行（返回值 = 退出码）。"""
    _force_utf8()
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    # ① 收尾类动作：不需要命令
    if args.doctor:
        return run_doctor(as_json=args.json)
    if args.clean:
        removed = prune_registry()
        _print_line(f"[safe_run] clean：移除 {removed} 条已死登记")
        return 0
    if args.list:
        return _report_list(args.json)
    if args.kill or args.kill_all:
        pids = list(args.kill)
        if args.kill_all:
            pids.extend(int(item.get("pid", 0)) for item in list_background())
        for pid in dict.fromkeys(pids):
            ok = kill_background(int(pid))
            _print_line(f"[safe_run] kill pid={pid} alive_after={not ok}")
        return 0

    # ② 执行类动作：必须有命令
    command = parse_command(args.command)
    if not command:
        parser.error("缺少要执行的命令（写法：python tools/safe_run.py -- <命令> <参数…>）")

    log_path = _log_path(args.label, args.log)
    if args.bg:
        payload = start_background(command, label=args.label, log_path=log_path)
    else:
        payload = run_foreground(command, timeout=args.timeout, log_path=log_path)
        payload["label"] = args.label

    write_summary(payload)
    report(payload, tail=args.tail, as_json=args.json)
    return int(payload["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())

