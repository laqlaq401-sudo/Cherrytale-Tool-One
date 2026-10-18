"""主动任务「一键执行」的契约（★ 2026-10-16 用户需求）。

跑法::

    python -m pytest tests/test_manual_batch_exec.py -q

【这个功能的一句话定义】
把勾上的主动任务**按规格表给的顺序**拼成**一次** ``/api/run`` 提交，
每个任务后面照抄单卡执行的拼法各跟一个 ``daily_box``（宴席除外）。

【为什么"一次提交"是硬要求（下面有用例钉死）】
服务端的 ``/api/stop`` 只中断**当前那一批**，队列里其他批次照跑。
拆成 N 次提交的话，用户点「停止执行」只能停掉手上那一件 ——
而他要的是"从当前任务开始，后面全都别跑"。

【名单与顺序的唯一来源是规格表】
前端不抄名单（抄了就会出现"新任务登记了却没勾选框"这种没人能一眼看出的漏洞），
所以下面既测 ``batch_tasks()`` 的顺序，也测它随 ``/api/tasks`` 下发。
"""

from __future__ import annotations

import pytest

from services import daily_state, runner, task_spec
from webapi import app as webapp
from webapi import logstream, state

# 复用队列测试里那套"不会真的联网"的替身与等待工具 ——
# 它们已经处理好了"worker 线程 + 取消令牌"的时序细节，
# 在第二个文件里再抄一遍只会让两份实现各自漂移。
from tests.test_webapi_state_queue import (  # noqa: PLC2701 - 跨测试模块复用
    _FakeContext,
    _fake_run_tasks,
    _wait_until,
    _wait_until_idle,
)


@pytest.fixture()
def manager(monkeypatch: pytest.MonkeyPatch) -> state.RunManager:
    """一个不会真的联网的 RunManager。

    ⚠️ ``prepare`` 的替身**必须**像 :class:`_FakeContext` 那样提供 ``close()``。
    返回一个裸 ``object()`` 会让 ``_execute_one`` 的 ``finally`` 在
    ``context.close()`` 处抛 ``AttributeError`` —— 而 ``finally`` 里排在它
    后面的正是 ``cancellation.uninstall_token(token)``。于是**取消令牌泄漏**：
    本文件之后的用例（乃至后面整个测试会话里所有用 ``sleep_interruptible``
    的用例）都会莫名其妙地被"用户点了停止执行"打断。
    （2026-10-16 实测踩过：全量跑时 test_material_stage / test_services_cancellation
    三处失败，单独跑却全绿 —— 就是因为本文件的字母序排在它们前面。）
    """
    monkeypatch.setattr(runner, "prepare", lambda request, emit=None: _FakeContext())
    return state.RunManager(logstream.LogStream())


@pytest.fixture()
def isolated_state_files(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """把三份"服务端账本"指到临时目录。

    ``/api/tasks`` 会顺带读它们（自动任务记账 / 宴席记账 / 勾选状态）。
    不隔离的话，用例的结果会随**开发机当前的真实状态**变化 ——
    比如今天已经捐过款，判据就会莫名其妙地红。
    """
    import config

    monkeypatch.setattr(config, "AUTO_DAILY_STATE_FILE", tmp_path / "daily.json")
    monkeypatch.setattr(config, "BANQUET_STATE_FILE", tmp_path / "banquet.json")
    monkeypatch.setattr(config, "SELECTION_STATE_FILE", tmp_path / "selection.json")
    monkeypatch.setattr(config, "SPEND_STATE_FILE", tmp_path / "spend.json")


# ---------------------------------------------------------------------------
# 一、成员与顺序：规格表驱动
# ---------------------------------------------------------------------------
def test_batch_tasks_order() -> None:
    """顺序 = 宴席第一，其余按规格表声明顺序。

    用户原话："以获得体力类为首先执行，也就是宴席排第一"。
    这是**展示/编排元数据**，所以它只该在规格表里写一次
    （``batch_order``），前端照着 ``/api/tasks`` 给的名单用。
    """
    assert task_spec.batch_tasks() == (
        "bbq_energy",
        "sweep_activity",
        "top_pvp_battle",
        "battle_arena",
        "wudou_pvp_battle",
        "battle_experiment",
        "battle_yimo",
        "sweep_material",
        "market_buy",
        "alliance_donate",
        "daily_task_auto",
    )


def test_batch_members_are_manual_and_enabled() -> None:
    """参与一键执行的任务必须是**可执行的主动任务**。

    ``enabled=False`` 的占位项（协议未确认）进了名单，用户会看到一个
    勾选框却永远跑不动；进了 ``auto`` 组则会与"每日自动执行"的语义打架。
    """
    for name in task_spec.BATCH_TASKS:
        spec = task_spec.get_spec(name)
        assert spec is not None, f"{name} 没有规格"
        assert spec.group == "manual", f"{name} 不是主动任务"
        assert spec.enabled, f"{name} 是占位项，不该参与一键执行"


def test_excluded_tasks_are_excluded() -> None:
    """用户点名排除的两个：买体力（花钻石）与主线通关（不限关数，可能很久）。"""
    for name in ("buy_energy", "push_main_stage"):
        assert name not in task_spec.BATCH_TASKS
        assert task_spec.get_spec(name).batch_eligible is False


def test_batch_order_only_on_batch_members() -> None:
    """``batch_order`` 只对成员有意义 —— 写在别人身上是打错字，不是配置。"""
    for name, spec in task_spec.TASK_SPECS.items():
        if spec.batch_order is not None:
            assert spec.batch_eligible, f"{name} 没参与一键执行却写了 batch_order"


def test_batch_eligible_reaches_the_view() -> None:
    """``batch_eligible`` 必须送到前端（它决定卡片画不画勾选框）。"""
    views = {view["name"]: view for view in task_spec.describe_tasks()}
    for name in task_spec.BATCH_TASKS:
        assert views[name]["batch_eligible"] is True
    assert views["buy_energy"]["batch_eligible"] is False
    assert views["push_main_stage"]["batch_eligible"] is False


def test_tasks_endpoint_exposes_batch_order(isolated_state_files: None) -> None:
    """``/api/tasks`` 要带一份**排好序**的名单。

    【为什么不只发每张卡自己的 ``batch_order`` 让前端自己排】
    那等于把同一条排序规则实现两遍（Python 一遍、JS 一遍），
    迟早不一致 —— 而这类不一致的表现是"顺序偶尔变了"，极难归因。
    """
    payload = webapp.WebApp(stream=logstream.LogStream()).tasks()
    assert payload["batch_tasks"] == list(task_spec.batch_tasks())


def test_alliance_donate_is_once_per_day() -> None:
    """工会捐献靠 ``once_per_day`` 的**服务端记账**实现"做一次就置灰"。

    它的"每天一次"和自动任务那批（礼包/签到/挖矿…）共用同一份账本，
    但**不参与自动执行编排**（那套只取 ``group="auto"``）。
    """
    assert "alliance_donate" in task_spec.ONCE_PER_DAY_TASKS
    assert task_spec.get_spec("alliance_donate").group == "manual"


# ---------------------------------------------------------------------------
# 二、记账只认成功（2026-10-16 修）
# ---------------------------------------------------------------------------
def test_accounting_skips_failed_tasks(
    manager: state.RunManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """失败**不记账** —— 否则捐献失败后卡片当天就灰了，想重试都点不了。

    旧行为（不看 ``result.ok``）对自动任务只是"当天不再重试"（还有卡片上的
    「执行」小按钮可手动补），但对借这套记账做"每日一次置灰"的手动任务
    就是硬伤。
    """
    recorded: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        daily_state,
        "mark_completed",
        lambda names, *a, **k: (recorded.append(tuple(names)), ())[1],
    )

    manager._on_step_finish(  # noqa: SLF001 - 这就是被测的钩子
        runner.StepResult(name="alliance_donate", ok=False, message="金币不足")
    )
    assert recorded == [], "失败的任务不该被记成「今天做过了」"

    manager._on_step_finish(  # noqa: SLF001
        runner.StepResult(name="alliance_donate", ok=True, message="已捐献")
    )
    assert recorded == [("alliance_donate",)]


def test_accounting_ignores_tasks_outside_the_list(
    manager: state.RunManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不在 ``once_per_day`` 名单里的任务不记账（它们一天可以跑很多次）。"""
    recorded: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        daily_state,
        "mark_completed",
        lambda names, *a, **k: (recorded.append(tuple(names)), ())[1],
    )
    manager._on_step_finish(  # noqa: SLF001
        runner.StepResult(name="sweep_activity", ok=True, message="扫完")
    )
    assert recorded == []


# ---------------------------------------------------------------------------
# 三、按 run_id 精确撤批（一键执行的「停止执行」靠它）
# ---------------------------------------------------------------------------
def test_stop_specific_cancels_a_queued_batch(
    manager: state.RunManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """排队中的那一批能被**单独撤掉**，且不会连坐正在跑的那一批。

    【为什么必须有这个能力】
    点「一键执行」时如果前面还有别的批次在跑，一键这批就是排队状态。
    老 ``stop()`` 会去停"正在跑的那一批"（不是用户点的那件事）——
    正好停错对象。而"从当前任务开始停掉之后所有任务"的期望，
    在这批还没开跑时等价于"整批撤掉"。
    """
    calls: list[str] = []
    monkeypatch.setattr(
        runner, "run_tasks", _fake_run_tasks(calls, blocking=frozenset({"wallet"}))
    )

    first_id, _ = manager.start(
        runner.RunRequest(
            tasks=(
                runner.TaskRequest(name="mail"),
                runner.TaskRequest(name="wallet"),
            )
        )
    )
    second_id, position = manager.start(
        runner.RunRequest(tasks=(runner.TaskRequest(name="daily_box"),))
    )
    assert position == 1  # 排在第一批后面

    _wait_until(lambda: "wallet" in calls)
    result = manager.stop(run_id=second_id)
    assert result["stopped"] is True
    assert result["cancelled_queued"] is True

    # 让第一批收尾（wallet 卡在里面等中断）
    manager.stop(run_id=first_id)
    _wait_until_idle(manager)

    steps = {step["name"]: step["status"] for step in manager.status()["steps"]}
    assert steps["daily_box"] == "skipped", "被撤销的批次要留下「没执行」的痕迹"
    assert "daily_box" not in calls, "撤销之后它一步都不该跑"
    assert steps["mail"] == "ok", "别的批次照常跑完"


def test_stop_specific_on_the_running_batch_interrupts_it(
    manager: state.RunManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """指定的那一批正在跑 → 走普通中断（剩下的步骤标 skipped）。"""
    calls: list[str] = []
    monkeypatch.setattr(
        runner, "run_tasks", _fake_run_tasks(calls, blocking=frozenset({"wallet"}))
    )
    run_id, _ = manager.start(
        runner.RunRequest(
            tasks=(
                runner.TaskRequest(name="mail"),
                runner.TaskRequest(name="wallet"),
                runner.TaskRequest(name="handshake"),
            )
        )
    )
    _wait_until(lambda: "wallet" in calls)

    result = manager.stop(run_id=run_id)
    assert result["stopped"] is True
    assert result.get("cancelled_queued") is not True

    _wait_until_idle(manager)
    steps = {step["name"]: step["status"] for step in manager.status()["steps"]}
    assert steps["mail"] == "ok"
    assert steps["handshake"] == "skipped"


def test_stop_specific_on_finished_batch_says_so(manager: state.RunManager) -> None:
    """找不到那一批（已跑完）时回一句人话，而不是假装停成功。"""
    result = manager.stop(run_id="never-existed")
    assert result["stopped"] is False
    assert "已经结束" in result["message"]
