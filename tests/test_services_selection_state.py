"""任务勾选/参数服务端持久化（``services/selection_state.py``）。

跑法::

    python -m pytest tests/test_services_selection_state.py
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from services import selection_state


@pytest.fixture()
def state_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把状态文件指到临时目录（测试之间互不干扰，也不碰真实工程根）。"""
    target = tmp_path / "selection_state.json"
    monkeypatch.setattr(
        selection_state, "_path", lambda path=None: Path(path or target)
    )
    return target


# ---------------------------------------------------------------------------
# 默认值：从未保存 = 空（前端据此走 localStorage 兜底）
# ---------------------------------------------------------------------------
def test_missing_file_means_empty(state_file: Path) -> None:
    """从没保存过 = 空任务表 + dry_run 未记录（None）。"""
    assert selection_state.saved_tasks() == {}
    assert selection_state.saved_dry_run() is None


def test_describe_marks_never_saved(state_file: Path) -> None:
    """saved_updated_at 为空串 = "从未存过"——前端据此保留 localStorage。"""
    described = selection_state.describe()
    assert described["saved_updated_at"] == ""
    assert described["saved_tasks"] == {}
    assert described["saved_dry_run"] is None


# ---------------------------------------------------------------------------
# 写读回
# ---------------------------------------------------------------------------
def test_save_then_read_round_trip(state_file: Path) -> None:
    tasks = {
        "sweep_material": {"checked": True, "params": {"times": 5}},
        "push_main_stage": {"checked": False, "params": {"max_stages": 3}},
    }
    saved = selection_state.save_selection(tasks, dry_run=True)
    assert saved["tasks"] == tasks
    assert saved["dry_run"] is True
    assert saved["updated_at"]  # 排障用时间戳必须有值

    assert selection_state.saved_tasks() == tasks
    assert selection_state.saved_dry_run() is True
    # saved 与"从未存过"现在必须可区分
    assert selection_state.describe()["saved_updated_at"] != ""


def test_saved_file_is_valid_json_with_version(state_file: Path) -> None:
    selection_state.save_selection({}, dry_run=False)
    payload = json.loads(state_file.read_text(encoding="utf-8"))
    assert payload["version"] == selection_state._FILE_VERSION
    assert payload["tasks"] == {}
    assert payload["dry_run"] is False


def test_explicit_path_overrides_default(tmp_path: Path) -> None:
    """``path`` 参数给谁就写谁（webapi 测试与多实例场景依赖这一点）。"""
    custom = tmp_path / "custom.json"
    selection_state.save_selection({"mail": {"checked": True, "params": {}}}, False, path=custom)
    assert custom.is_file()
    assert selection_state.saved_tasks(path=custom) == {
        "mail": {"checked": True, "params": {}}
    }


# ---------------------------------------------------------------------------
# 白名单过滤：任务名与参数键只认规格表（防前后端规格漂移攒脏数据）
# ---------------------------------------------------------------------------
def test_unknown_task_name_is_dropped(state_file: Path) -> None:
    saved = selection_state.save_selection(
        {"mail": {"checked": True, "params": {}}, "no_such_task": {"checked": True, "params": {}}},
        dry_run=False,
    )
    assert "mail" in saved["tasks"]
    assert "no_such_task" not in saved["tasks"]


def test_unknown_param_key_is_dropped(state_file: Path) -> None:
    """``sweep_material`` 的规格参数是 ``times``；别的键一律不留。"""
    saved = selection_state.save_selection(
        {
            "sweep_material": {
                "checked": True,
                "params": {"times": 5, "tims": 9, "hack": "x"},
            }
        },
        dry_run=False,
    )
    entry = saved["tasks"]["sweep_material"]
    assert entry["params"] == {"times": 5}


def test_malformed_entries_are_tolerated(state_file: Path) -> None:
    """entry 不是 dict → 整条丢弃；params 不是 dict → 当空参数。都绝不炸。"""
    saved = selection_state.save_selection(
        {
            "mail": "不是 dict",
            "free_gift": {"checked": True, "params": "也不是 dict"},
            "push_main_stage": {"checked": True, "params": {"max_stages": 3}},
        },
        dry_run=False,
    )
    assert "mail" not in saved["tasks"]  # 形状完全不对 = 无法解读 = 整条丢
    assert saved["tasks"]["free_gift"] == {"checked": True, "params": {}}
    assert saved["tasks"]["push_main_stage"]["params"] == {"max_stages": 3}


def test_values_are_stored_verbatim(state_file: Path) -> None:
    """值不做类型归一（那是前端 reconcile 按 ParamSpec.kind 的职责）——
    字符串就存字符串，-1（扫到空）也原样保留。"""
    saved = selection_state.save_selection(
        {"sweep_material": {"checked": True, "params": {"times": -1}}},
        dry_run=False,
    )
    assert saved["tasks"]["sweep_material"]["params"]["times"] == -1


# ---------------------------------------------------------------------------
# 损坏降级：任何一种坏法都等价于"从未设置"（空），绝不抛异常
# ---------------------------------------------------------------------------
def test_broken_json_falls_back_to_empty(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{ 这不是 JSON", encoding="utf-8")
    assert selection_state.saved_tasks(path=broken) == {}
    assert selection_state.saved_dry_run(path=broken) is None


def test_wrong_version_falls_back_to_empty(tmp_path: Path) -> None:
    future = tmp_path / "future.json"
    future.write_text(json.dumps({"version": 999, "tasks": {}}), encoding="utf-8")
    assert selection_state.saved_tasks(path=future) == {}


def test_wrong_tasks_type_falls_back_to_empty(tmp_path: Path) -> None:
    """手改文件把 tasks 写成列表/字符串：当"从未设置"处理。"""
    weird = tmp_path / "weird.json"
    weird.write_text(
        json.dumps({"version": 1, "tasks": ["不对"], "dry_run": True}),
        encoding="utf-8",
    )
    assert selection_state.saved_tasks(path=weird) == {}
    # dry_run 类型正确，照常读回；tasks 坏只影响 tasks
    assert selection_state.saved_dry_run(path=weird) is True


def test_unwritable_target_never_raises(tmp_path: Path) -> None:
    """没有写权限（这里用"父路径是文件"模拟）时只记日志，不让接口 500。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("我是一个文件，不是目录", encoding="utf-8")
    saved = selection_state.save_selection({}, False, path=blocker / "inner.json")
    assert saved["dry_run"] is False  # 内存里的返回值照常成立


# ---------------------------------------------------------------------------
# 并发：多线程同时写不产生坏文件（模块锁的最基本验证）
# ---------------------------------------------------------------------------
def test_concurrent_writes_keep_valid_state(state_file: Path) -> None:
    def write(index: int, times: int) -> None:
        for _ in range(times):
            selection_state.save_selection(
                {"mail": {"checked": index % 2 == 0, "params": {}}},
                dry_run=bool(index % 2),
            )

    threads = [
        threading.Thread(target=write, args=(index, 25)) for index in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # 100 次并发写之后文件必须仍是合法状态（不坏不缺，白名单照常生效）
    tasks = selection_state.saved_tasks()
    assert set(tasks) <= {"mail"}
    assert selection_state.saved_dry_run() in (True, False)
