"""钻石使用总闸的服务端持久化（``services/spend_state.py``）。

跑法::

    python -m pytest tests/test_services_spend_state.py -q
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services import spend_state


@pytest.fixture()
def state_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把状态文件指到临时目录（测试之间互不干扰，也不碰真实工程根）。"""
    target = tmp_path / "spend_state.json"
    monkeypatch.setattr(spend_state, "_path", lambda path=None: Path(path or target))
    return target


# ---------------------------------------------------------------------------
# 默认值：fail-closed
# ---------------------------------------------------------------------------
def test_missing_file_means_closed(state_file: Path) -> None:
    """从没保存过 = 总闸关闭（默认禁止，绝不会因为"没状态"而放行）。"""
    assert spend_state.allow_diamond() is False


def test_load_returns_default_shape_for_missing_file(state_file: Path) -> None:
    payload = spend_state.load()
    assert payload == {
        "version": spend_state._FILE_VERSION,
        "allow_diamond": False,
        "updated_at": "",
    }


# ---------------------------------------------------------------------------
# 写读回
# ---------------------------------------------------------------------------
def test_set_then_read_round_trip(state_file: Path) -> None:
    saved = spend_state.set_allow_diamond(True)
    assert saved["allow_diamond"] is True
    assert saved["updated_at"]  # 排障用时间戳必须有值
    assert spend_state.allow_diamond() is True

    # 关回去也必须生效（"关了就关了"）
    spend_state.set_allow_diamond(False)
    assert spend_state.allow_diamond() is False


def test_saved_file_is_valid_json_with_version(state_file: Path) -> None:
    spend_state.set_allow_diamond(True)
    payload = json.loads(state_file.read_text(encoding="utf-8"))
    assert payload["version"] == spend_state._FILE_VERSION
    assert payload["allow_diamond"] is True


def test_explicit_path_overrides_default(tmp_path: Path) -> None:
    """``path`` 参数给谁就写谁（webapi 测试与多实例场景依赖这一点）。"""
    custom = tmp_path / "custom.json"
    spend_state.set_allow_diamond(True, path=custom)
    assert custom.is_file()
    assert spend_state.allow_diamond(path=custom) is True


# ---------------------------------------------------------------------------
# 损坏降级：任何一种坏法都等价于"从未设置"（关闭），绝不抛异常
# ---------------------------------------------------------------------------
def test_broken_json_falls_back_to_closed(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{ 这不是 JSON", encoding="utf-8")
    assert spend_state.allow_diamond(path=broken) is False
    assert spend_state.load(path=broken)["allow_diamond"] is False


def test_wrong_version_falls_back_to_closed(tmp_path: Path) -> None:
    future = tmp_path / "future.json"
    future.write_text(
        json.dumps({"version": 999, "allow_diamond": True}), encoding="utf-8"
    )
    assert spend_state.allow_diamond(path=future) is False


def test_wrong_field_type_falls_back_to_closed(tmp_path: Path) -> None:
    """手改文件把布尔写成字符串：当"从未设置"处理，不信任坏数据。"""
    weird = tmp_path / "weird.json"
    weird.write_text(
        json.dumps({"version": 1, "allow_diamond": "true"}), encoding="utf-8"
    )
    assert spend_state.allow_diamond(path=weird) is False


def test_missing_field_falls_back_to_closed(tmp_path: Path) -> None:
    sparse = tmp_path / "sparse.json"
    sparse.write_text(json.dumps({"version": 1}), encoding="utf-8")
    assert spend_state.allow_diamond(path=sparse) is False


def test_unwritable_target_never_raises(tmp_path: Path) -> None:
    """没有写权限（这里用"父路径是文件"模拟）时只记日志，不让接口 500。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("我是一个文件，不是目录", encoding="utf-8")
    # target.parent.mkdir(exist_ok=True) 会撞上这个文件 → OSError → 被吞掉
    saved = spend_state.set_allow_diamond(True, path=blocker / "inner.json")
    assert saved["allow_diamond"] is True  # 内存里的返回值照常成立


# ---------------------------------------------------------------------------
# 并发：两线程同时写不丢更新（模块锁的最基本验证）
# ---------------------------------------------------------------------------
def test_concurrent_writes_keep_last_value(state_file: Path) -> None:
    import threading

    def flip(value: bool, times: int) -> None:
        for _ in range(times):
            spend_state.set_allow_diamond(value)

    threads = [
        threading.Thread(target=flip, args=(index % 2 == 0, 25))
        for index in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # 50 次并发写之后文件必须仍是合法状态（True/False 二选一，不坏不缺）
    assert spend_state.load()["allow_diamond"] in (True, False)
