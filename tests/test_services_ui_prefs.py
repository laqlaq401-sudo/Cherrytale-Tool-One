"""主动任务卡片展开状态的服务端持久化（``services/ui_prefs.py``）。

跑法::

    python -m pytest tests/test_services_ui_prefs.py -q
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services import task_spec, ui_prefs


@pytest.fixture()
def state_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把状态文件指到临时目录（测试之间互不干扰，也不碰真实工程根）。"""
    target = tmp_path / "ui_prefs.json"
    monkeypatch.setattr(ui_prefs, "_path", lambda path=None: Path(path or target))
    return target


def _manual_task_names() -> list[str]:
    """规格表里"可以被展开"的任务名（主动任务且不隐藏）。"""
    return [
        name
        for name, spec in task_spec.TASK_SPECS.items()
        if spec.group == "manual" and not spec.hidden
    ]


def _auto_task_name() -> str:
    """一个**自动**任务名（它没有可展开的卡片，必须被白名单丢掉）。"""
    for name, spec in task_spec.TASK_SPECS.items():
        if spec.group != "manual" and not spec.hidden:
            return name
    raise AssertionError("规格表里居然没有自动任务")  # pragma: no cover


def _hidden_task_name() -> str:
    """一个 ``hidden=True`` 的任务名（不出现在清单里，必须被丢掉）。"""
    for name, spec in task_spec.TASK_SPECS.items():
        if spec.hidden:
            return name
    raise AssertionError("规格表里居然没有隐藏任务")  # pragma: no cover


# ---------------------------------------------------------------------------
# 默认值
# ---------------------------------------------------------------------------
def test_missing_file_means_all_collapsed(state_file: Path) -> None:
    """从没保存过 = 一张卡也没展开（不是错误，也不该抛异常）。"""
    assert ui_prefs.expanded_tasks() == []


def test_load_returns_default_shape_for_missing_file(state_file: Path) -> None:
    payload = ui_prefs.load()
    assert payload == {
        "version": ui_prefs._FILE_VERSION,
        "expanded": [],
        "updated_at": "",
    }


# ---------------------------------------------------------------------------
# 写读回
# ---------------------------------------------------------------------------
def test_set_then_read_round_trip(state_file: Path) -> None:
    names = _manual_task_names()[:2]
    assert len(names) == 2, "规格表里至少要有两个主动任务，这条测试才有意义"

    saved = ui_prefs.set_expanded(names)
    assert saved["expanded"] == names
    assert saved["updated_at"]  # 排障用时间戳必须有值
    assert ui_prefs.expanded_tasks() == names

    # 全部收起也必须生效（"关了就关了"）
    ui_prefs.set_expanded([])
    assert ui_prefs.expanded_tasks() == []


def test_saved_file_is_valid_json_with_version(state_file: Path) -> None:
    ui_prefs.set_expanded(_manual_task_names()[:1])
    payload = json.loads(state_file.read_text(encoding="utf-8"))
    assert payload["version"] == ui_prefs._FILE_VERSION
    assert payload["expanded"] == _manual_task_names()[:1]


# ---------------------------------------------------------------------------
# 白名单：读出与写入两条路径都要过滤
# ---------------------------------------------------------------------------
def test_unknown_and_wrong_group_names_are_dropped(state_file: Path) -> None:
    """未知任务名 / 自动任务 / 隐藏任务一律丢弃，只留主动任务。"""
    keep = _manual_task_names()[0]
    ui_prefs.set_expanded([keep, "no_such_task", _auto_task_name(), _hidden_task_name()])
    assert ui_prefs.expanded_tasks() == [keep]


def test_declaration_order_and_dedup(state_file: Path) -> None:
    """结果是按规格表声明顺序、去重后的 —— 与传入顺序无关（写入即可复现）。"""
    names = _manual_task_names()
    if len(names) < 2:  # pragma: no cover - 规格表退化时跳过
        pytest.skip("主动任务不足两个，无法验证顺序")
    reverse = list(reversed(names)) + [names[0]]
    ui_prefs.set_expanded(reverse)
    assert ui_prefs.expanded_tasks() == names


def test_non_string_entries_are_ignored(state_file: Path) -> None:
    keep = _manual_task_names()[0]
    ui_prefs.set_expanded([keep, 123, None, {"x": 1}])  # type: ignore[list-item]
    assert ui_prefs.expanded_tasks() == [keep]


# ---------------------------------------------------------------------------
# 坏文件：静默降级，绝不能把接口打挂
# ---------------------------------------------------------------------------
def test_broken_json_falls_back_to_empty(state_file: Path) -> None:
    state_file.write_text("{ 这不是 JSON", encoding="utf-8")
    assert ui_prefs.expanded_tasks() == []


def test_future_version_is_discarded(state_file: Path) -> None:
    """版本不符 = 整份丢弃（重新展开一次即可恢复，不是数据损失）。"""
    state_file.write_text(
        json.dumps({"version": 999, "expanded": _manual_task_names()}), encoding="utf-8"
    )
    assert ui_prefs.expanded_tasks() == []


def test_missing_expanded_key_is_tolerated(state_file: Path) -> None:
    """★ 关键兼容性：以后加字段不必升版本 —— 少一个键按"空"处理，而不是当损坏。"""
    state_file.write_text(json.dumps({"version": ui_prefs._FILE_VERSION}), encoding="utf-8")
    assert ui_prefs.expanded_tasks() == []


def test_expanded_wrong_type_is_tolerated(state_file: Path) -> None:
    state_file.write_text(
        json.dumps({"version": ui_prefs._FILE_VERSION, "expanded": "不是列表"}),
        encoding="utf-8",
    )
    assert ui_prefs.expanded_tasks() == []


def test_describe_matches_expanded_tasks(state_file: Path) -> None:
    """``describe()``（给 /api/tasks 用）与 ``expanded_tasks()`` 必须一致。"""
    names = _manual_task_names()[:1]
    ui_prefs.set_expanded(names)
    described = ui_prefs.describe()
    assert described["expanded"] == names
    assert described["updated_at"] == ui_prefs.load()["updated_at"]
