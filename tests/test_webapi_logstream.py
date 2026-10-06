"""``webapi/logstream.py`` 的守护测试（离线，不联网）。

【它守的是什么】
1. **增量游标**：``seq`` 单调、``since()`` 只回新行、容量淘汰后如实标记"有截断"；
2. **两条采集通道**：logging Handler 只收业务模块；
   ``print`` 桥**只收 worker 线程**（其它线程的输出必须原样转发，不能串台）；
3. **端到端**：真的跑一次 ``handshake --dry-run``（离线），
   并确认「logging 日志」与「print 出来的"未发送"」**都**进了日志流 ——
   这正是网页日志窗不会缺内容的原因。

运行方式：

    python -m tests.test_webapi_logstream
    python -m pytest tests/test_webapi_logstream.py -q
"""

from __future__ import annotations

import io
import logging
import sys
import threading
from collections.abc import Iterator

import pytest

from webapi import logstream


@pytest.fixture()
def stream() -> logstream.LogStream:
    """一个小容量的日志流（便于测"淘汰"行为）。"""
    return logstream.LogStream(capacity=5)


@pytest.fixture()
def attached(stream: logstream.LogStream) -> Iterator[logstream.LogStream]:
    """把日志流挂到根日志器上，测试结束**必须**摘掉并恢复级别。"""
    root = logging.getLogger()
    level_before = root.level
    handler = logstream.attach_to_root(stream, level=logging.INFO)
    try:
        yield stream
    finally:
        logstream.detach(handler)
        root.setLevel(level_before)


# ---------------------------------------------------------------------------
# ① 缓冲与增量游标
# ---------------------------------------------------------------------------
class TestLogStream:
    def test_seq_is_monotonic(self, stream: logstream.LogStream) -> None:
        first = stream.append("INFO", "tasks", "第一行")
        second = stream.append("INFO", "tasks", "第二行")
        assert (first.seq, second.seq) == (1, 2)
        assert stream.next_seq == 3

    def test_since_returns_only_new_lines(self, stream: logstream.LogStream) -> None:
        stream.append("INFO", "tasks", "a")
        stream.append("INFO", "tasks", "b")
        lines, truncated = stream.since(1)
        assert [line.message for line in lines] == ["b"]
        assert truncated is False

    def test_since_zero_returns_everything(self, stream: logstream.LogStream) -> None:
        stream.append("INFO", "tasks", "a")
        lines, truncated = stream.since(0)
        assert [line.message for line in lines] == ["a"]
        assert truncated is False

    def test_capacity_evicts_oldest(self, stream: logstream.LogStream) -> None:
        for index in range(7):
            stream.append("INFO", "tasks", f"第{index}行")
        assert [line.message for line in stream.snapshot()] == [
            "第2行",
            "第3行",
            "第4行",
            "第5行",
            "第6行",
        ]
        assert stream.dropped == 2

    def test_truncation_is_reported(self, stream: logstream.LogStream) -> None:
        """被挤掉的行必须如实标记：否则用户会以为"程序没打印"。

        注意：只有前端**已经读过一些行**（seq > 0）时"截断"才有意义 ——
        首屏（seq=0）本来就只拿现存的行，不算丢失。
        """
        for index in range(8):
            stream.append("INFO", "tasks", f"第{index}行")
        _, truncated = stream.since(1)  # 前端上次读到 seq=1，但它已被淘汰
        assert truncated is True

    def test_clear_does_not_reset_seq(self, stream: logstream.LogStream) -> None:
        """清空后序号必须继续递增。

        一旦归零，前端的游标（旧的大序号）就会"超前"，
        新日志一行都不会显示 —— 表现为"点了清空之后日志再也不动了"。
        """
        stream.append("INFO", "tasks", "a")
        before = stream.next_seq
        stream.clear()
        assert stream.snapshot() == []
        assert stream.next_seq == before
        new_line = stream.append("INFO", "tasks", "b")
        assert new_line.seq == before

    def test_to_dict_is_serialisable(self, stream: logstream.LogStream) -> None:
        line = stream.append("WARNING", "client.tls", "证书链可疑")
        assert line.to_dict() == {
            "seq": line.seq,
            "ts": line.ts,
            "level": "WARNING",
            "logger": "client.tls",
            "message": "证书链可疑",
        }


# ---------------------------------------------------------------------------
# ② logging 通道
# ---------------------------------------------------------------------------
class TestLoggingChannel:
    def test_only_business_loggers_are_captured(
        self, attached: logstream.LogStream
    ) -> None:
        logging.getLogger("tasks.login").info("业务日志")
        logging.getLogger("uvicorn.access").info("访问日志不该出现")
        logging.getLogger("urllib3.connectionpool").info("库日志也不该出现")

        loggers = {line.logger for line in attached.snapshot()}
        assert "tasks.login" in loggers
        assert "uvicorn.access" not in loggers
        assert "urllib3.connectionpool" not in loggers

    def test_info_level_survives_root_default(self, stream: logstream.LogStream) -> None:
        """根日志器默认是 WARNING；挂上来之后 INFO 必须能到达（否则网页只有警告）。"""
        root = logging.getLogger()
        level_before = root.level
        root.setLevel(logging.WARNING)
        handler = logstream.attach_to_root(stream, level=logging.INFO)
        try:
            logging.getLogger("tasks").info("这条必须被收到")
        finally:
            logstream.detach(handler)
            root.setLevel(level_before)
        assert [line.message for line in stream.snapshot()] == ["这条必须被收到"]

    def test_multiline_message_is_split(self, attached: logstream.LogStream) -> None:
        logging.getLogger("tasks").info("第一行\n第二行")
        assert [line.message for line in attached.snapshot()] == ["第一行", "第二行"]


# ---------------------------------------------------------------------------
# ③ print 通道（线程感知）
# ---------------------------------------------------------------------------
class TestStdoutBridge:
    def test_uncaptured_thread_goes_to_original(self, stream: logstream.LogStream) -> None:
        original = io.StringIO()
        bridge = logstream.ThreadAwareStdout(stream, original)
        bridge.write("普通输出\n")
        assert original.getvalue() == "普通输出\n"
        assert stream.snapshot() == []

    def test_captured_thread_lines_go_to_stream(
        self, stream: logstream.LogStream
    ) -> None:
        original = io.StringIO()
        bridge = logstream.ThreadAwareStdout(stream, original)

        def worker() -> None:
            with bridge.capture_current_thread():
                bridge.write("第一行\n第二行\n")

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=10)

        lines = stream.snapshot()
        assert [line.message for line in lines] == ["第一行", "第二行"]
        assert original.getvalue() == ""  # 被捕获的线程不再写原始流
        assert all(line.level == logstream.USER_LEVEL for line in lines)
        assert all(line.logger == logstream.USER_LEVEL for line in lines)

    def test_capture_is_thread_scoped(self, stream: logstream.LogStream) -> None:
        """worker 线程被捕获时，主线程的输出必须**不受影响**（不串台）。"""
        original = io.StringIO()
        bridge = logstream.ThreadAwareStdout(stream, original)
        started = threading.Event()
        release = threading.Event()

        def worker() -> None:
            with bridge.capture_current_thread():
                started.set()
                release.wait(timeout=10)
                bridge.write("worker 输出\n")

        thread = threading.Thread(target=worker)
        thread.start()
        assert started.wait(timeout=10)

        bridge.write("主线程输出\n")  # 主线程没有被捕获
        release.set()
        thread.join(timeout=10)

        assert "主线程输出" in original.getvalue()
        assert [line.message for line in stream.snapshot()] == ["worker 输出"]

    def test_partial_line_is_buffered_until_newline(
        self, stream: logstream.LogStream
    ) -> None:
        """``print(x, end="")`` 会分两次 write：不能把一行拆成两行。"""
        bridge = logstream.ThreadAwareStdout(stream, io.StringIO())

        def worker() -> None:
            with bridge.capture_current_thread():
                bridge.write("半行")
                assert stream.snapshot() == []  # 还没换行 → 不落库
                bridge.write("补齐\n")

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=10)
        assert [line.message for line in stream.snapshot()] == ["半行补齐"]

    def test_leftover_partial_line_is_dropped(self, stream: logstream.LogStream) -> None:
        """捕获结束时仍未结束的半行必须丢弃，避免串进下一次运行。"""
        bridge = logstream.ThreadAwareStdout(stream, io.StringIO())

        def worker() -> None:
            with bridge.capture_current_thread():
                bridge.write("没有换行的半行")

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=10)
        assert stream.snapshot() == []

    def test_attribute_forwarding(self, stream: logstream.LogStream) -> None:
        original = io.StringIO()
        bridge = logstream.ThreadAwareStdout(stream, original)
        assert bridge.encoding == original.encoding
        assert bridge.isatty() is False

    def test_install_and_uninstall(self, stream: logstream.LogStream) -> None:
        """显式传入 ``original`` 时，卸载后恢复成**它**（而不是别的流）。"""
        saved = sys.stdout
        original = io.StringIO()
        bridge = logstream.install_stdout_bridge(stream, original=original)
        try:
            assert sys.stdout is bridge
            assert bridge.original is original
        finally:
            logstream.uninstall_stdout_bridge(bridge)
            assert sys.stdout is original
            sys.stdout = saved  # 交还给 pytest，别影响后续用例

    def test_install_without_original_wraps_current_stdout(
        self, stream: logstream.LogStream
    ) -> None:
        """不传 ``original`` 时包装的是"当前的 sys.stdout"，卸载后原样恢复。

        这是 ``web_server.py`` / ``main_gui.py`` 真实使用的路径。
        """
        saved = sys.stdout
        bridge = logstream.install_stdout_bridge(stream)
        try:
            assert bridge.original is saved
        finally:
            logstream.uninstall_stdout_bridge(bridge)
            assert sys.stdout is saved

    def test_uninstall_leaves_foreign_stream_alone(
        self, stream: logstream.LogStream
    ) -> None:
        """期间若别人换过 ``sys.stdout``（pytest/jupyter），不要去覆盖它。"""
        saved = sys.stdout
        bridge = logstream.install_stdout_bridge(stream, original=io.StringIO())
        foreign = io.StringIO()
        try:
            sys.stdout = foreign
            logstream.uninstall_stdout_bridge(bridge)
            assert sys.stdout is foreign
        finally:
            sys.stdout = saved


# ---------------------------------------------------------------------------
# ④ 端到端：真跑一次离线任务，两条通道都要有内容
# ---------------------------------------------------------------------------
class TestEndToEnd:
    def test_runner_output_all_lands_in_stream(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """在 worker 线程里跑一次 ``handshake --dry-run``（**不联网**）。

        这条是"网页日志窗不会缺内容"的直接证据：
        既要能收到 ``tasks`` 的 logging，也要能收到任务 ``print`` 的"未发送"。

        【★ 2026-10-04 为什么不再用共享的 ``attached`` fixture】
        那条流的容量是 **5**（为"淘汰行为"测试准备的）。端到端这里要断言的是
        "**两条通道都有内容**"，容量 5 会让先出现的 logging 行被后来的 print 挤掉，
        断言就退化成"看谁排在最后"。去重整改后 ``✔ 任务完成`` 不再进 INFO
        （入口层已输出 ``[✔] 名字: 文案``），`logger == "tasks"` 只剩
        ``▶ 开始任务`` 一条 —— 它恰好被挤掉，于是断言当场变红。
        改用正常容量的流，才是这条用例真正想测的东西。
        """
        from services import runner

        monkeypatch.setattr(runner, "resolve_session_state", lambda: (None, "（无）"))

        attached = logstream.LogStream(capacity=500)
        root = logging.getLogger()
        level_before = root.level
        handler = logstream.attach_to_root(attached, level=logging.INFO)
        original = io.StringIO()
        bridge = logstream.install_stdout_bridge(attached, original=original)
        try:
            request = runner.RunRequest(
                tasks=(runner.TaskRequest("handshake"),), dry_run=True
            )

            def worker() -> None:
                # 真实 Web 侧的写法：worker 线程 + 捕获当前线程的 print
                with bridge.capture_current_thread():
                    context = runner.prepare(request)
                    try:
                        runner.run_tasks(context, request)
                    finally:
                        context.close()

            thread = threading.Thread(target=worker, name="cherrytale-run", daemon=True)
            thread.start()
            thread.join(timeout=120)
            assert not thread.is_alive(), "任务线程超时未结束"
        finally:
            logstream.uninstall_stdout_bridge(bridge)
            logstream.detach(handler)
            root.setLevel(level_before)

        lines = attached.snapshot(limit=500)
        messages = [line.message for line in lines]

        # ① print 通道（演练说明）
        assert any("未发送" in message for message in messages), messages
        # ② logging 通道（任务日志）—— 去重后由 `▶ 开始任务` 承担这一角色
        assert any(line.logger == "tasks" for line in lines), messages
        assert any("开始任务" in message for message in messages), messages
        assert any("handshake" in message for message in messages)
        # ③ 原始 stdout 不该被污染（worker 的 print 已被引走）
        assert "未发送" not in original.getvalue()


if __name__ == "__main__":  # 支持 `python -m tests.test_webapi_logstream`
    raise SystemExit(pytest.main([__file__, "-v", "--no-header"]))

