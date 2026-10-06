"""运行状态机的两条新契约（2026-10-04）：**排队**与**只停当前**。

跑法::

    python -m pytest tests/test_webapi_state_queue.py -q
"""

from __future__ import annotations

import time

import pytest

from services import cancellation, runner
from webapi import logstream, state


class _FakeContext:
    """``runner.prepare`` 的替身：只需要能被 ``close()``。"""

    def close(self) -> None:  # pragma: no cover - 空实现
        return None


@pytest.fixture()
def manager(monkeypatch: pytest.MonkeyPatch) -> state.RunManager:
    """一个不会真的联网的 RunManager。"""
    monkeypatch.setattr(runner, "prepare", lambda request, emit=None: _FakeContext())
    stream = logstream.LogStream()
    return state.RunManager(stream)


def _fake_run_tasks(
    calls: list[str], *, per_task_sleep: float = 0.0, blocking: frozenset[str] = frozenset()
):
    """造一个假的 ``run_tasks``：记录顺序，并能在指定任务上"卡住等中断"。

    :param blocking: 在这里**停住**（``sleep_interruptible``）直到被中断，
        用来稳定地制造"用户中途点了停止"这一场景 —— 比用 sleep 猜时间稳。
    """

    def fake(context, request, emit=None, on_start=None, on_finish=None, report=None):
        steps = []
        for task in request.tasks:
            if on_start is not None:
                on_start(task.name)
            calls.append(task.name)
            if task.name in blocking:
                cancellation.sleep_interruptible(30)  # 会被 /api/stop 打断
            elif per_task_sleep:
                cancellation.sleep_interruptible(per_task_sleep)
            step = runner.StepResult(name=task.name, ok=True, message="ok")
            steps.append(step)
            if on_finish is not None:
                on_finish(step)
        return runner.RunReport(steps=steps)

    return fake


def _wait_until(condition, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError("条件没有在预期时间内成立")


def _wait_until_idle(manager: state.RunManager, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not manager.is_running:
            return
        time.sleep(0.05)
    raise AssertionError("运行没有在预期时间内结束")


def test_runs_are_executed_in_submission_order(
    manager: state.RunManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """用户点三个就跑三个，顺序与提交顺序一致（不再有 409 拒绝）。"""
    calls: list[str] = []
    monkeypatch.setattr(runner, "run_tasks", _fake_run_tasks(calls, per_task_sleep=0.05))

    _, behind_first = manager.start(
        runner.RunRequest(tasks=(runner.TaskRequest(name="mail"),))
    )
    assert behind_first == 0  # 队列空 → 立刻开始

    _, behind_second = manager.start(
        runner.RunRequest(tasks=(runner.TaskRequest(name="wallet"),))
    )
    assert behind_second >= 1  # 前面还有一批 → 排队

    _wait_until_idle(manager)
    assert calls == ["mail", "wallet"]


def test_queued_batches_are_visible_in_status(
    manager: state.RunManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """排着队的步骤也要出现在进度里 —— 否则"我点的东西"看不见。"""
    calls: list[str] = []
    monkeypatch.setattr(runner, "run_tasks", _fake_run_tasks(calls, per_task_sleep=0.05))

    manager.start(runner.RunRequest(tasks=(runner.TaskRequest(name="mail"),)))
    manager.start(runner.RunRequest(tasks=(runner.TaskRequest(name="wallet"),)))

    snapshot = manager.status()
    assert snapshot["running"] is True
    assert [step["name"] for step in snapshot["steps"]] == ["mail", "wallet"]
    _wait_until_idle(manager)


def test_finished_steps_carry_a_server_side_clock(
    manager: state.RunManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 2026-10-06：结果行的时间戳必须来自**服务端**。

    【为什么钉住它（用户报的"瞬间同时执行"）】
    ``/api/status`` 在页面加载时会把最近若干批已完成的步骤一并回填。若前端给
    回填的每一行都盖"渲染这一刻"的章，一批严格串行、耗时合计几十秒的任务就会
    全部显示成同一个秒数 —— 看起来像瞬间并发跑完。所以每个结束的步骤都必须
    自带服务端钟点 ``finished_at``（``HH:MM:SS``），前端照抄即可。
    """
    import re

    calls: list[str] = []
    monkeypatch.setattr(runner, "run_tasks", _fake_run_tasks(calls, per_task_sleep=0.05))

    manager.start(
        runner.RunRequest(
            tasks=(runner.TaskRequest(name="mail"), runner.TaskRequest(name="wallet"))
        )
    )
    _wait_until_idle(manager)

    steps = {step["name"]: step for step in manager.status()["steps"]}
    assert set(steps) == {"mail", "wallet"}
    for name, step in steps.items():
        assert re.fullmatch(r"\d{2}:\d{2}:\d{2}", step["finished_at"]), (
            f"{name} 缺少服务端结束时刻：{step['finished_at']!r}"
        )
    # 还没跑的步骤不该凭空有个时刻（否则前端会以为它已经结束）
    assert state.StepState(name="x", title="x").finished_at == ""


def test_stop_only_stops_the_current_batch(
    manager: state.RunManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 用户 2026-10-04 选定的语义：停手上的这一批，队列里剩下的照常跑。"""
    calls: list[str] = []
    # wallet 会"卡住"直到被中断 —— 用它稳定制造"用户中途点了停止"
    monkeypatch.setattr(
        runner, "run_tasks", _fake_run_tasks(calls, blocking=frozenset({"wallet"}))
    )

    manager.start(
        runner.RunRequest(
            tasks=(
                runner.TaskRequest(name="mail"),
                runner.TaskRequest(name="wallet"),
                runner.TaskRequest(name="handshake"),
            )
        )
    )
    manager.start(runner.RunRequest(tasks=(runner.TaskRequest(name="daily_box"),)))

    # 等第一批真的跑到 wallet（而不是靠 sleep 猜时间），再中断
    _wait_until(lambda: "wallet" in calls)
    result = manager.stop()
    assert result["stopped"] is True

    _wait_until_idle(manager)

    steps = {step["name"]: step["status"] for step in manager.status()["steps"]}
    # ① 当前批次：中断前跑完的 mail 是 ok；被打断的 wallet 与没轮到的 handshake
    #    都不会是 ok（skipped = 没执行 / fail = 执行到一半被中断）
    assert steps["mail"] == "ok"
    assert steps["wallet"] in ("skipped", "fail")
    assert steps["handshake"] == "skipped"
    # ② 队列里那一批照常跑完了（没有被"停止"连坐）
    assert steps["daily_box"] == "ok"
    assert calls == ["mail", "wallet", "daily_box"]


def test_stop_without_run_is_a_noop(manager: state.RunManager) -> None:
    """没有运行可停时，只是"没什么可停的"，不是错误。"""
    assert manager.stop()["stopped"] is False


# ---------------------------------------------------------------------------
# ★ 2026-10-06：卡死看门狗的"worker 心跳"契约
#
# 背景：用户报"点了执行，进度条一动不动"—— 而 ``POST /api/run`` 早就返回了，
# 真正卡住的是 worker 线程。旧看门狗只看 ``inflight``（HTTP 请求在飞数），
# 那时它是 0，于是**永远不转储**，日志里一行证据都没有。
# 下面三条测试把这个契约钉死。
# ---------------------------------------------------------------------------


def test_run_active_is_registered_while_a_batch_runs(
    manager: state.RunManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """worker 手上有活时，``run_active`` 必须 > 0（看门狗据此判断"忙不忙"）。"""
    from webapi import app as app_module

    calls: list[str] = []
    monkeypatch.setattr(runner, "run_tasks", _fake_run_tasks(calls, blocking=frozenset({"wallet"})))

    before = app_module._ACTIVITY["run_active"]
    manager.start(runner.RunRequest(tasks=(runner.TaskRequest(name="wallet"),)))
    _wait_until(lambda: "wallet" in calls)
    # 任务卡在里面 → 看门狗应当认为"有活在手上"
    assert app_module._ACTIVITY["run_active"] > before
    assert app_module._ACTIVITY.get("last_note")  # 心跳留下了"在哪一步"

    manager.stop()
    _wait_until_idle(manager)
    # 收尾后必须归零，否则看门狗会永远以为有活在跑
    assert app_module._ACTIVITY["run_active"] == before


def test_task_boundary_refreshes_heartbeat(
    manager: state.RunManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """每个任务的开始/结束都要刷新 ``last_progress``（否则正常跑也会被误判卡死）。"""
    from webapi import app as app_module

    calls: list[str] = []
    monkeypatch.setattr(runner, "run_tasks", _fake_run_tasks(calls, per_task_sleep=0.02))

    # 先把时间戳压到很久以前，模拟"已经很久没有进展"
    app_module._ACTIVITY["last_progress"] = time.time() - 9999

    manager.start(
        runner.RunRequest(
            tasks=(
                runner.TaskRequest(name="mail"),
                runner.TaskRequest(name="wallet"),
            )
        )
    )
    _wait_until_idle(manager)
    # 任务边界打的心跳应当把时间戳拉回现在
    assert time.time() - app_module._ACTIVITY["last_progress"] < 5


def test_hang_watchdog_dumps_even_with_zero_inflight(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 核心回归：``inflight == 0`` 但运行卡住时，看门狗也应当转储。

    旧实现 ``if inflight <= 0: continue`` 会直接跳过 —— 正是用户遇到的那种
    "没有请求在飞、只有 worker 卡住"的形态。

    【怎么在不真等 15 秒的情况下测到】
    ``_start_hang_watchdog`` 把巡检函数丢进守护线程，异常传不回来，
    所以这里直接**截住 Thread 构造**拿到 ``watch`` 本体，握手式地跑一轮：
    ``sleep`` 第一次返回（模拟"15 秒过去了"），判据执行完后第二次 ``sleep``
    抛出一个哨兵异常，把循环打断 —— 此时 "是否转储过" 已经有了答案。
    """
    from webapi import app as app_module

    dumped: list[str] = []

    class _Stop(Exception):
        pass

    calls = {"n": 0}

    def _sleep(_seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise _Stop

    monkeypatch.setattr(app_module, "dump_all_thread_stacks", lambda note: dumped.append(note))
    monkeypatch.setattr(app_module.time, "sleep", _sleep)

    # 造一个"有运行在跑、但久无进展"的现场
    with app_module._ACTIVITY_LOCK:
        app_module._ACTIVITY["inflight"] = 0
        app_module._ACTIVITY["run_active"] = 1
        app_module._ACTIVITY["last_progress"] = time.time() - 9999

    def _capture_target(*, target=None, name=None, daemon=None):  # noqa: A002
        # 只截下回调，不起真线程
        class _T:
            def start(self) -> None:
                return None

        _capture_target.fn = target  # type: ignore[attr-defined]
        return _T()

    monkeypatch.setattr(app_module.threading, "Thread", _capture_target)

    app_module._start_hang_watchdog()
    watch = _capture_target.fn  # type: ignore[attr-defined]

    # 直接在测试线程里跑一轮巡检（异常由 _sleep 抛出，好接住）
    with pytest.raises(_Stop):
        watch()

    assert dumped, "inflight=0 但有运行卡住时，看门狗必须转储"
    assert "1 个运行" in dumped[0]
