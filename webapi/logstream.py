"""日志流：把"任务执行过程中说的话"收进一个内存缓冲，供网页增量读取。

【为什么需要**两条**采集通道】
任务层的用户可见输出有两种，缺哪一种网页都会"少一半信息"：

1. ``logging`` 日志 —— 例如 ``▶ 开始任务 mail（拉取邮件列表 → 一键领取全部邮件奖励。）``；
2. ``print()`` 输出 —— 例如 ``—— 以上为演练输出，未发送任何请求 ——``。
   ``tasks/`` 下共有 11 个文件、100+ 处 ``print``，且**演练/只读模式**的说明
   几乎全是 ``print`` 出来的。

.. note::
   **★ 2026-10-04 结果行去重**：每一步的结论行只有**一处**来源 ——
   入口层输出的 ``[✔] 任务名: 文案``（CLI 是 ``print(step.line)``，
   Web 是 :meth:`webapi.state.RunState._on_step_finish` 里的 ``_log(result.line)``）。
   ``tasks/base.py`` 原先那句 ``✔ 任务 mail 完成（0.9s）：…`` 已降为 DEBUG，
   否则同一句话会在日志流里出现两遍。

只挂 logging Handler 的话，网页上会看不到任何一个"未发送"的提示 ——
而那恰恰是"默认只读、fail-closed"纪律最需要被用户看到的部分。
又因为本次任务书明确要求"``tasks/`` 原封不动"（改 ``print`` 为 logging
还会打穿 ``tests/test_main.py`` 里对 stdout 的断言），所以第二条通道用
**线程感知的 stdout 代理**：只把 worker 线程的 ``print`` 收进队列，
其它线程（uvicorn 的访问日志等）照旧写原始 stdout。

【为什么用 deque + seq，而不是 queue.Queue】
前端是**轮询**取增量的（手机端尤其如此）。``queue.Queue`` 取走就没了，
断线重连后无法"从上次的位置继续"。这里用固定容量的 ``deque`` + 单调递增的
``seq``：前端带上上次的 ``since`` 就能精确续传，既不重复也不丢行；
超出容量的旧行会被淘汰，并且**如实**告诉前端"有丢失"。

【线程安全】
worker 线程写、HTTP 线程读，两边都过同一把 ``Lock``。
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Final, TextIO

#: 未配置日志时的默认容量。为什么是 2000 行：一次日常运行大约几十行，
#: 2000 行够放下几十次运行；同时每行不过百来字节，内存占用在百 KB 量级。
DEFAULT_CAPACITY: Final[int] = 2000

#: ``print`` 出来的行用的"级别"名。
#: 前端看到 ``USER`` 就按普通文本渲染（不带级别标签、不染色）。
USER_LEVEL: Final[str] = "USER"

#: 只采集这些前缀的 logger —— 避免 uvicorn 的访问日志把业务日志冲掉。
#: 用**前缀**而不是具体名字：``tasks.login``、``client.game_client`` 都要收。
CAPTURED_LOGGER_PREFIXES: Final[tuple[str, ...]] = (
    "main",
    "client",
    "crypto",
    "models",
    "services",
    "tasks",
    "webapi",
)


@dataclass(frozen=True)
class LogLine:
    """一行日志。

    :param seq: 单调递增序号（前端用它做增量游标）。
    :param ts: ``HH:MM:SS`` 形式的时间戳（不带日期：一次运行不会跨天）。
    :param level: 级别名（``INFO`` / ``DEBUG`` / ``WARNING`` / ``USER`` …）。
    :param logger: 来源名（``tasks``、``client.game_client``、``USER``）。
    :param message: 正文（已去掉行尾换行）。
    """

    seq: int
    ts: str
    level: str
    logger: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        """转成 JSON 可序列化的 dict（``/api/logs`` 直接返回它）。"""
        return {
            "seq": self.seq,
            "ts": self.ts,
            "level": self.level,
            "logger": self.logger,
            "message": self.message,
        }


class LogStream:
    """固定容量的环形日志缓冲（线程安全）。

    :param capacity: 最多保留多少行；超出后淘汰最旧的。
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self._capacity = max(int(capacity), 1)
        self._lines: deque[LogLine] = deque(maxlen=self._capacity)
        self._lock = threading.Lock()
        self._next_seq = 1
        #: 被淘汰（前端会漏读）的行数合计 —— 用于如实告知"有丢失"。
        self._dropped = 0

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def append(self, level: str, logger_name: str, message: str) -> LogLine:
        """追加一行（多行文本请先按行拆开，见 :func:`_emit_text_lines`）。

        :return: 落库的那一行（便于测试与调试）。
        """
        with self._lock:
            line = LogLine(
                seq=self._next_seq,
                ts=time.strftime("%H:%M:%S"),
                level=level,
                logger=logger_name,
                message=message,
            )
            if len(self._lines) == self._capacity:
                # deque(maxlen) 会自动挤掉最旧的一行，这里单独记一笔"丢了"
                self._dropped += 1
            self._lines.append(line)
            self._next_seq += 1
            return line

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def since(self, seq: int = 0) -> tuple[list[LogLine], bool]:
        """取回 ``seq`` **之后**的所有行（前端增量拉取用）。

        :param seq: 前端上次拿到的最后一个序号；``0`` 表示"从头开始"。
        :return: ``(行列表, 是否发生过丢失)``。
            "丢失"= 前端要的区间里有行已被容量淘汰（前端据此提示用户"日志被截断"，
            而不是以为程序漏打印了）。
        """
        with self._lock:
            lines = [line for line in self._lines if line.seq > seq]
            oldest = self._lines[0].seq if self._lines else self._next_seq
            # 前端要的行已经被挤掉：它给的 seq 比现存最旧的行还早，
            # 或者期间有淘汰（_dropped > 0 且 seq < oldest-1）。
            truncated = seq < oldest - 1 and seq > 0
            if seq == 0 and self._dropped > 0:
                truncated = True
            return lines, truncated

    @property
    def next_seq(self) -> int:
        """下一个将被分配的序号（前端拿它当"当前进度"显示）。"""
        with self._lock:
            return self._next_seq

    @property
    def dropped(self) -> int:
        """累计被淘汰的行数。"""
        with self._lock:
            return self._dropped

    def snapshot(self, *, limit: int = 200) -> list[LogLine]:
        """取最后 ``limit`` 行（首屏渲染用；不做增量语义）。"""
        with self._lock:
            return list(self._lines)[-max(int(limit), 1) :]

    def clear(self) -> None:
        """清空缓冲。

        .. note::
            ``seq`` **不会**归零：归零会让前端的游标与新的行号撞车
            （前端还以为自己已经读到最新，于是新日志一行都不显示）。
        """
        with self._lock:
            self._lines.clear()


# ---------------------------------------------------------------------------
# 通道一：logging → 日志流
# ---------------------------------------------------------------------------
class LogStreamHandler(logging.Handler):
    """把日志记录推进 :class:`LogStream`（只收业务模块，见 :meth:`filter`）。"""

    def __init__(self, stream: LogStream, *, level: int = logging.INFO) -> None:
        super().__init__(level)
        self._stream = stream

    def emit(self, record: logging.LogRecord) -> None:
        """写一条记录。多行消息按行拆开（前端一行一条更好读）。"""
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - 格式化失败不该拖垮整个程序
            return
        for line in message.splitlines() or [""]:
            self._stream.append(record.levelname, record.name, line)

    def filter(self, record: logging.LogRecord) -> bool:
        """只放行业务 logger（前缀白名单）。

        【为什么要过滤】
        根日志器上还挂着 uvicorn / urllib3 等库的日志，它们会以 DEBUG 级别
        疯狂刷屏（一次请求几十行），把业务日志冲出可视范围。
        这里用**白名单**而不是黑名单：将来新增某个库时默认不收，
        不会出现"某天网页日志里突然多出几百行不明来源的 DEBUG"。
        """
        return record.name.startswith(CAPTURED_LOGGER_PREFIXES)


def attach_to_root(stream: LogStream, *, level: int = logging.INFO) -> LogStreamHandler:
    """把日志流挂到根日志器上，返回 handler（便于测试或关闭时摘掉）。

    :param stream: 目标日志流。
    :param level: 采集级别。
    :return: 已挂上的 :class:`LogStreamHandler`。

    【为什么要顺手调根日志器级别】
    Python 根日志器的默认级别是 ``WARNING``，级别是在**产生端**过滤的 ——
    只挂 handler 的话，INFO 级别的"✔ 任务完成"根本到不了 handler。
    所以这里把根级别**至少**调到 ``level``（已经是 DEBUG 就保持不动，
    绝不"顺手降低"，与 ``services/runner.py`` 里 ``_temporary_log_level`` 同一原则）。
    """
    root = logging.getLogger()
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)
    handler = LogStreamHandler(stream, level=level)
    root.addHandler(handler)
    return handler


def detach(handler: logging.Handler) -> None:
    """把 handler 从根日志器上摘掉（测试收尾与服务关闭时用）。"""
    logging.getLogger().removeHandler(handler)


# ---------------------------------------------------------------------------
# 通道二：print（仅 worker 线程）→ 日志流
# ---------------------------------------------------------------------------
class ThreadAwareStdout:
    """一个"按线程决定去处"的 stdout 代理。

    :param stream: 目标日志流。
    :param original: 原始 stdout（未被捕获的线程照旧写它）。
    :param logger_name: 捕获到的行在日志流里显示成哪个来源。

    【为什么不用 ``contextlib.redirect_stdout``】
    它是**进程级**的：把 ``sys.stdout`` 换掉的那段时间里，
    所有线程的输出都会被吞进同一个目标。长驻的 Web 服务里有 uvicorn 的
    访问日志、有并发的请求处理，串台之后表现为"网页日志里混进了别人的输出"，
    而这种串台问题在本地单人测试时几乎不会出现，只在真实使用中暴露。

    【为什么只捕获 worker 线程】
    任务的 ``print`` 只会发生在执行任务的线程里。把捕获范围收窄到该线程，
    其它线程（包括将来并行跑多个任务）就天然互不干扰。

    【为什么要有"半行缓冲"】
    ``print("a", end="")`` 会先写 ``"a"`` 再写换行，甚至可能分成两次 ``write``。
    不做缓冲的话，日志里会出现被切成两半的行。缓冲以**线程**为单位，
    捕获结束时丢弃 —— 避免半行串到下一次运行。
    """

    def __init__(
        self,
        stream: LogStream,
        original: TextIO,
        *,
        logger_name: str = USER_LEVEL,
    ) -> None:
        self._stream = stream
        self._original = original
        self._logger_name = logger_name
        self._captured: set[int] = set()
        self._lock = threading.Lock()
        self._pending: dict[int, str] = {}

    @property
    def original(self) -> TextIO:
        """被代理的原始流（卸载时要用它恢复 ``sys.stdout``）。"""
        return self._original

    @property
    def stream(self) -> LogStream:
        """代理把捕获到的行写进哪个日志流。

        【为什么暴露这个属性】
        入口层（``web_server.py`` / ``main_gui.py``）要同时把"内存日志流"给
        应用（状态与说明用它）和给代理（``print`` 用它）。两者**必须是同一个对象**，
        否则会出现"网页上少了一半日志"这种极难察觉的问题（真实踩过：
        只传了代理、没传流，于是两个缓冲各写各的）。
        ``webapi/app.py`` 的 ``create_app`` 在缺省 ``stream`` 时直接用本属性补上，
        从结构上消灭这种传参失误。
        """
        return self._stream

    # ------------------------------------------------------------------
    # 捕获开关
    # ------------------------------------------------------------------
    @contextmanager
    def capture_current_thread(self) -> Iterator[None]:
        """把**当前线程**的输出并入日志流；退出时恢复。

        :return: 上下文管理器。

        Web 侧用法：worker 线程里 ``with bridge.capture_current_thread():``
        包住整次 ``run_tasks()``，任务里的所有 ``print`` 就都进了日志流。
        """
        ident = threading.get_ident()
        with self._lock:
            self._captured.add(ident)
        try:
            yield
        finally:
            with self._lock:
                self._captured.discard(ident)
                self._pending.pop(ident, None)

    # ------------------------------------------------------------------
    # TextIO 接口
    # ------------------------------------------------------------------
    def write(self, text: str) -> int:
        """写文本。未被捕获的线程原样转发，被捕获的线程按行送进日志流。"""
        ident = threading.get_ident()
        with self._lock:
            captured = ident in self._captured
        if not captured:
            if self._original is not None and hasattr(self._original, "write"):
                return self._original.write(text)
            return len(text)

        with self._lock:
            remainder = self._pending.get(ident, "") + text
            complete: list[str] = []
            while "\n" in remainder:
                line, remainder = remainder.split("\n", 1)
                complete.append(line.rstrip("\r"))
            self._pending[ident] = remainder

        for line in complete:
            self._stream.append(USER_LEVEL, self._logger_name, line)
        return len(text)

    def flush(self) -> None:
        """转发 flush（未被捕获时也要保证正常输出可见）。"""
        if self._original is not None and hasattr(self._original, "flush"):
            self._original.flush()

    def isatty(self) -> bool:
        """永远返回 ``False``：被代理后的输出不是终端，别让调用方去用 ANSI 颜色。"""
        return False

    def __getattr__(self, name: str) -> Any:
        """其余属性/方法一律转发给原始流。

        【为什么要这个兜底】
        ``sys.stdout`` 会被当成完整的文件对象使用（``encoding``、``fileno()``、
        ``reconfigure()`` …）。少实现一个属性就可能让某个第三方库在运行时炸掉，
        而且报错位置离真正的原因很远，极难定位。转发是最省事也最安全的做法。
        若无控制台（例如 Windows 无窗口 GUI 运行，sys.stdout 为 None），则提供静默空操作。
        """
        if self._original is None:
            return lambda *args, **kwargs: None
        return getattr(self._original, name)


def install_stdout_bridge(
    stream: LogStream, *, original: TextIO | None = None
) -> ThreadAwareStdout:
    """安装 stdout 代理（进程级，装一次即可）。

    :param stream: 目标日志流。
    :param original: 被代理的原始流；``None`` 表示取"当前的 sys.stdout"。
        显式传入主要是给测试用 —— 避免与 pytest 自己的输出捕获互相干扰。
    :return: 安装好的代理（卸载时要用同一个对象）。
    """
    raw_target = original if original is not None else sys.stdout
    bridge = ThreadAwareStdout(stream, raw_target)
    sys.stdout = bridge  # type: ignore[assignment]
    return bridge


def uninstall_stdout_bridge(bridge: ThreadAwareStdout) -> None:
    """卸载代理。

    **只在它仍是当前 ``sys.stdout`` 时恢复**：如果期间有别的库换过 stdout
    （例如 pytest、jupyter），贸然覆盖会把它们的捕获机制弄坏 ——
    宁可留下一层无害的代理，也不破坏别人的环境。
    """
    if sys.stdout is bridge:
        sys.stdout = bridge.original


