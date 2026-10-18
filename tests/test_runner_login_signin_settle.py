"""运行侧补触发「登录签到」的回归测试（★ 2026-10-17 · 方向 A）。

覆盖对象：``services/runner.py::_settle_login_signin``（经 ``run_tasks`` 调用）。
背景：登录签到原来只挂"登录动作"两处，而会话持久化的日常根本不经过登录页
⇒ 钩子从不执行（根因：``notes/login_signin_trigger_gap.md``）；
修复方案：``notes/login_signin_run_trigger_plan.md``。

【本文件要钉死的五条规则】
1. ``RunRequest.settle_login_signin`` **默认关** —— 测试/脚本直接构造的请求零副作用；
2. 置位 + 有会话 → 恰好触发一次，且发生在**第一个任务开始之前**；
3. 无会话 / 演练模式 / 批内含 ``login`` → 不触发；
4. 钩子抛异常 → 只记 warning，批次照常完成（"双保险"不破）；
5. 钩子经 ``emit`` 输出的行会进入本次运行的**说明流**（``report.notes`` + 外部 sink）。

运行方式：

    python -m pytest tests/test_runner_login_signin_settle.py -q
"""

from __future__ import annotations

import logging

import pytest

from client.session import GameSession
from client.spending import SpendPolicy
from models.session_state import GameSessionState
from services import runner
from tasks.base import TaskResult


def make_state(*, player_id: int = 1, server_id: int = 1) -> GameSessionState:
    """带区服/角色信息的会话状态（触发与否只看它"存在"；真伪由钩子自己的测试管）。"""
    return GameSessionState(token="tok", player_id=player_id, server_id=server_id)


def make_context(*, with_session: bool) -> runner.RunContext:
    """不依赖会话文件的运行上下文（与 test_services_runner 的 context fixture 同款）。"""
    return runner.RunContext(
        session=GameSession(),
        session_state=make_state() if with_session else None,
        session_source="（测试）",
        spend_policy=SpendPolicy(),
        run_options=runner.RunOptions(),
    )


def spy_hook(
    monkeypatch: pytest.MonkeyPatch, events: list[str] | None = None
) -> list[dict]:
    """把 ``tasks.login_signin.run_if_first_login_today`` 换成记录器。

    【为什么这里劫持得到】``runner._settle_login_signin`` 里是**函数内**局部导入，
    导入发生在调用时 —— 拿到的是模块上的当前属性，monkeypatch 生效。
    """
    import tasks.login_signin as login_signin

    calls: list[dict] = []

    def fake(session_state: object, *, now=None, path=None, emit=None) -> None:
        if events is not None:
            events.append("hook")
        calls.append({"state": session_state, "emit": emit})

    monkeypatch.setattr(login_signin, "run_if_first_login_today", fake)
    return calls


def stub_task(
    monkeypatch: pytest.MonkeyPatch, name: str, events: list[str] | None = None
) -> None:
    """把某个任务的执行换成离线替身（沿用 test_services_runner 的既有手法）。

    ``execute``（真跑）与 ``_dry_run``（演练）都替换 —— 无论 ``request.dry_run``
    取什么值都不会联网。
    """
    runner.load_builtin_tasks()
    task_class = runner.resolve_task_class(name)

    def fake_execute(self: object) -> TaskResult:
        if events is not None:
            events.append(f"task:{name}")
        return TaskResult(task=name, ok=True, message="（测试替身）")

    monkeypatch.setattr(task_class, "execute", fake_execute)
    monkeypatch.setattr(task_class, "_dry_run", fake_execute, raising=False)


class TestSettleLoginSignin:
    def test_default_request_does_not_trigger(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """默认关：直接构造的请求（测试 / 脚本）零副作用。"""
        calls = spy_hook(monkeypatch)
        stub_task(monkeypatch, "handshake")
        ctx = make_context(with_session=True)
        request = runner.RunRequest(tasks=(runner.TaskRequest("handshake"),))
        try:
            report = runner.run_tasks(ctx, request)
        finally:
            ctx.close()
        assert calls == []
        assert report.ok is True

    def test_flagged_request_triggers_once_before_first_task(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """置位 + 有会话 → 恰好一次，且在第一个任务开始之前。"""
        events: list[str] = []
        calls = spy_hook(monkeypatch, events)
        stub_task(monkeypatch, "handshake", events)
        ctx = make_context(with_session=True)
        request = runner.RunRequest(
            tasks=(runner.TaskRequest("handshake"),), settle_login_signin=True
        )
        try:
            report = runner.run_tasks(
                ctx, request, on_start=lambda name: events.append(f"start:{name}")
            )
        finally:
            ctx.close()

        assert events.count("hook") == 1
        assert (
            events.index("hook")
            < events.index("start:handshake")
            < events.index("task:handshake")
        )
        assert calls[0]["state"] is ctx.session_state
        assert calls[0]["emit"] is None  # 未提供说明流出口 → 不硬塞
        assert report.ok is True

    def test_no_session_does_not_trigger(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """没有会话 → 跳过（钩子自己也认，这里省一次调用）。"""
        calls = spy_hook(monkeypatch)
        stub_task(monkeypatch, "handshake")
        ctx = make_context(with_session=False)
        request = runner.RunRequest(
            tasks=(runner.TaskRequest("handshake"),), settle_login_signin=True
        )
        try:
            report = runner.run_tasks(ctx, request)
        finally:
            ctx.close()
        assert calls == []
        assert report.ok is True

    def test_dry_run_does_not_trigger(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """演练模式不触发 —— 守住"dry-run = 零网络流量"的既有契约。"""
        calls = spy_hook(monkeypatch)
        stub_task(monkeypatch, "handshake")
        ctx = make_context(with_session=True)
        request = runner.RunRequest(
            tasks=(runner.TaskRequest("handshake"),),
            dry_run=True,
            settle_login_signin=True,
        )
        try:
            report = runner.run_tasks(ctx, request)
        finally:
            ctx.close()
        assert calls == []
        assert report.ok is True

    def test_login_batch_does_not_trigger(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """批内含 login → 交给登录侧钩子（免得先拿旧令牌白发一次）。"""
        calls = spy_hook(monkeypatch)
        stub_task(monkeypatch, "login")
        stub_task(monkeypatch, "handshake")
        ctx = make_context(with_session=True)
        request = runner.RunRequest(
            tasks=(runner.TaskRequest("login"), runner.TaskRequest("handshake")),
            settle_login_signin=True,
        )
        try:
            report = runner.run_tasks(ctx, request)
        finally:
            ctx.close()
        assert calls == []
        assert len(report.steps) == 2  # 批照样跑（login 由替身执行）

    def test_hook_exception_is_contained(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """钩子抛异常 → 只记 warning，批次照常完成（双保险）。"""
        import tasks.login_signin as login_signin

        def boom(session_state: object, **kwargs: object) -> None:
            raise RuntimeError("模拟钩子崩了")

        monkeypatch.setattr(login_signin, "run_if_first_login_today", boom)
        stub_task(monkeypatch, "handshake")
        ctx = make_context(with_session=True)
        request = runner.RunRequest(
            tasks=(runner.TaskRequest("handshake"),), settle_login_signin=True
        )
        with caplog.at_level(logging.WARNING, logger="services.runner"):
            try:
                report = runner.run_tasks(ctx, request)
            finally:
                ctx.close()
        assert report.ok is True  # 批次不受影响
        warnings = [r.getMessage() for r in caplog.records]
        assert any("登录签到" in m and "触发异常" in m for m in warnings)

    def test_emit_line_reaches_notes_and_sink(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """钩子 emit 出来的行 → 本次运行的说明流（notes + 外部 sink）。"""
        import tasks.login_signin as login_signin

        def fake(session_state: object, *, now=None, path=None, emit=None) -> None:
            assert emit is not None  # 有说明流出口时必须传下去
            emit("登录签到（每日+活动） ✔ 测试入账")

        monkeypatch.setattr(login_signin, "run_if_first_login_today", fake)
        stub_task(monkeypatch, "handshake")
        lines: list[str] = []
        ctx = make_context(with_session=True)
        request = runner.RunRequest(
            tasks=(runner.TaskRequest("handshake"),), settle_login_signin=True
        )
        try:
            report = runner.run_tasks(ctx, request, emit=lines.append)
        finally:
            ctx.close()
        assert "登录签到（每日+活动） ✔ 测试入账" in lines
        assert "登录签到（每日+活动） ✔ 测试入账" in report.notes
