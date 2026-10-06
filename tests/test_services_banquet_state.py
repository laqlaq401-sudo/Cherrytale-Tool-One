"""宴席「早/晚两顿吃过了没有」的服务端记账（``services/banquet_state.py``）。

【为什么这些边界必须钉死】
- 换日口径错 → 晚宴席在 05:00–06:00 之间被判成新的一天，"已吃过"记录作废，
  于是**重复发包**（用户明确要求独立于游戏日）；
- 身份键错 → 换号 / 换区后误判"已经吃过"，新角色一整天都吃不了。

跑法::

    python -m pytest tests/test_services_banquet_state.py -q
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from models.activity_stage import DISPLAY_TZ
from services import banquet_state


def _session(server_id: int = 12, player_id: int = 345678) -> dict[str, int]:
    """``describe_session()`` 形态的最小会话信息。"""
    return {"server_id": server_id, "player_id": player_id}


def _oct(hour: int, minute: int = 0, *, day: int = 3) -> datetime:
    """2026-10-``day`` 的某个时刻（带展示时区，与后端一致）。"""
    return datetime(2026, 10, day, hour, minute, tzinfo=DISPLAY_TZ)


@pytest.fixture()
def state_file(tmp_path: Path) -> Path:
    """把记账文件指到临时目录（测试之间互不干扰）。"""
    return tmp_path / "banquet_state.json"


# ---------------------------------------------------------------------------
# 基本读写
# ---------------------------------------------------------------------------
def test_no_record_means_nothing_eaten(state_file: Path) -> None:
    assert banquet_state.meals_done(_session(), path=state_file) == ()


def test_mark_then_read(state_file: Path) -> None:
    banquet_state.mark_meal_done("morning", _session(), path=state_file)
    assert banquet_state.meals_done(_session(), path=state_file) == ("morning",)


def test_marking_both_meals(state_file: Path) -> None:
    banquet_state.mark_meal_done("morning", _session(), path=state_file)
    banquet_state.mark_meal_done("evening", _session(), path=state_file)
    assert set(banquet_state.meals_done(_session(), path=state_file)) == {"morning", "evening"}


def test_marking_is_idempotent(state_file: Path) -> None:
    """同一顿记两次不会重复（追加是幂等的）。"""
    banquet_state.mark_meal_done("morning", _session(), path=state_file)
    banquet_state.mark_meal_done("morning", _session(), path=state_file)
    assert banquet_state.meals_done(_session(), path=state_file) == ("morning",)


def test_unknown_meal_is_ignored(state_file: Path) -> None:
    """非法的餐次标识一律忽略，不写进记录。"""
    banquet_state.mark_meal_done("brunch", _session(), path=state_file)
    assert banquet_state.meals_done(_session(), path=state_file) == ()


def test_identity_key_covers_server_and_player(state_file: Path) -> None:
    """换号 / 换区都必须是另一个对象（否则新角色一整天吃不了）。"""
    banquet_state.mark_meal_done("morning", _session(1, 2), path=state_file)
    assert banquet_state.meals_done(_session(1, 3), path=state_file) == ()
    assert banquet_state.meals_done(_session(2, 2), path=state_file) == ()


def test_no_session_is_not_recorded(state_file: Path) -> None:
    assert banquet_state.mark_meal_done("morning", {}, path=state_file) == ()
    assert banquet_state.meals_done({}, path=state_file) == ()


# ---------------------------------------------------------------------------
# 换日：宴席日（06:00）而不是游戏日（05:00）
# ---------------------------------------------------------------------------
def test_record_survives_the_0500_game_reset_but_not_the_0600_banquet_reset(
    state_file: Path,
) -> None:
    """★ 本模块存在的核心理由：晚宴席跨过 05:00，不能被游戏日重置。

    场景：10-03 00:30 吃完晚宴席（属于 10-02 的宴席日）。
    - 10-03 05:30（游戏日已换、宴席日未换）→ 记录**仍在**；
    - 10-03 06:30（宴席日也换了）→ 记录**作废**。
    """
    banquet_state.mark_meal_done("evening", _session(), now=_oct(0, 30, day=3), path=state_file)

    # 还没到 06:00：仍是 10-02 的宴席日 → 记录有效。
    assert banquet_state.meals_done(_session(), now=_oct(5, 30, day=3), path=state_file) == (
        "evening",
    )
    # 过了 06:00：新宴席日 → 记录作废。
    assert banquet_state.meals_done(_session(), now=_oct(6, 30, day=3), path=state_file) == ()


def test_new_banquet_day_overwrites_previous(state_file: Path) -> None:
    banquet_state.mark_meal_done("morning", _session(), now=_oct(10, 0, day=3), path=state_file)
    banquet_state.mark_meal_done("morning", _session(), now=_oct(10, 0, day=4), path=state_file)
    # 新一天的记录只含新的一天那一顿，旧日期不会残留。
    assert banquet_state.meals_done(_session(), now=_oct(10, 0, day=4), path=state_file) == (
        "morning",
    )
    assert banquet_state.meals_done(_session(), now=_oct(10, 0, day=3), path=state_file) == ()


# ---------------------------------------------------------------------------
# 稳健性
# ---------------------------------------------------------------------------
def test_corrupt_file_degrades_to_empty(state_file: Path) -> None:
    """文件损坏时静默降级（不能让 /api/tasks 整个 500）。"""
    state_file.write_text("{ not json", encoding="utf-8")
    assert banquet_state.meals_done(_session(), path=state_file) == ()


def test_describe_view(state_file: Path) -> None:
    banquet_state.mark_meal_done("evening", _session(), now=_oct(20, 0, day=3), path=state_file)
    view = banquet_state.describe(_session(), now=_oct(20, 0, day=3), path=state_file)
    assert view == {"banquet_day": "2026-10-03", "meals": ["evening"]}


def test_clear(state_file: Path) -> None:
    banquet_state.mark_meal_done("morning", _session(), path=state_file)
    banquet_state.clear(path=state_file)
    assert banquet_state.meals_done(_session(), path=state_file) == ()


if __name__ == "__main__":  # 支持 `python -m tests.test_services_banquet_state`
    raise SystemExit(pytest.main([__file__, "-v", "--no-header"]))
