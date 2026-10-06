"""自动任务「每游戏日只跑一次」的服务端记账（``services/daily_state.py``）。

跑法::

    python -m pytest tests/test_services_daily_state.py -q
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from services import daily_state


def _session(server_id: int = 12, player_id: int = 345678) -> dict[str, int]:
    """``describe_session()`` 形态的最小会话信息。"""
    return {"server_id": server_id, "player_id": player_id}


@pytest.fixture()
def state_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把记账文件指到临时目录（测试之间互不干扰）。"""
    target = tmp_path / "daily_auto_state.json"
    monkeypatch.setattr(daily_state, "_path", lambda path=None: Path(path or target))
    return target


def test_identity_key_covers_server_and_player() -> None:
    """换区 / 换号都必须是**另一个**对象（否则新号一整天都不做日常）。"""
    assert daily_state.identity_key(_session(1, 2)) != daily_state.identity_key(_session(1, 3))
    assert daily_state.identity_key(_session(1, 2)) != daily_state.identity_key(_session(2, 2))
    assert daily_state.identity_key({}) == "<no-session>"


def test_mark_then_read(state_file: Path) -> None:
    daily_state.mark_completed(["free_gift"], _session())
    assert daily_state.completed_names(_session()) == ("free_gift",)

    # 追加是幂等的：重复记账不会把记录撑大
    daily_state.mark_completed(["free_gift", "alliance_sign_in"], _session())
    assert daily_state.completed_names(_session()) == ("free_gift", "alliance_sign_in")


def test_other_character_is_not_affected(state_file: Path) -> None:
    daily_state.mark_completed(["free_gift"], _session(1, 111))
    assert daily_state.completed_names(_session(1, 222)) == ()


def test_new_game_day_resets_the_record(state_file: Path) -> None:
    """05:00 换日：03:00 与 06:00 属于两个游戏日，旧记录自动作废。"""
    early = datetime(2026, 10, 4, 3, 0)
    daily_state.mark_completed(["free_gift"], _session(), now=early)
    assert daily_state.completed_names(_session(), now=early) == ("free_gift",)

    later = datetime(2026, 10, 4, 6, 0)
    assert daily_state.completed_names(_session(), now=later) == ()

    daily_state.mark_completed(["alliance_sign_in"], _session(), now=later)
    assert daily_state.completed_names(_session(), now=later) == ("alliance_sign_in",)


def test_no_session_means_no_bookkeeping(state_file: Path) -> None:
    """没登录时不记账 —— 连"是谁"都不知道，记了只会误导排障。"""
    assert daily_state.mark_completed(["free_gift"], {}) == ()
    assert daily_state.completed_names({}) == ()


def test_broken_file_falls_back_to_empty(tmp_path: Path) -> None:
    """文件坏了也只当"没跑过"（多跑一遍好过页面 500）。"""
    broken = tmp_path / "broken.json"
    broken.write_text("{ 这不是 JSON", encoding="utf-8")
    assert daily_state.completed_names(_session(), path=broken) == ()


def test_clear_resets_everything(state_file: Path) -> None:
    daily_state.mark_completed(["free_gift"], _session())
    daily_state.clear()
    assert daily_state.completed_names(_session()) == ()


def test_task_spec_names_the_once_per_day_tasks() -> None:
    """用户点名的任务（挖矿有两张卡）都必须在名单里。

    2026-10-04 首批四个；2026-10-06 补了 ``daily_free_draw`` / ``top_pvp_box``
    （用户要求"礼包 / 签到 / 挖矿 / 抽卡 / 荣耀之巅宝箱，只在第一次登录工具时执行"——
    这两个任务类文档自己就写着"每日一次"，只是规格表漏登记）。
    """
    from services import task_spec

    for name in (
        "free_gift",
        "alliance_sign_in",
        "alliance_mining_personal",
        "alliance_mining_team",
        "daily_free_draw",
        "top_pvp_box",
    ):
        assert name in task_spec.ONCE_PER_DAY_TASKS, f"{name} 应当是每游戏日一次"
    # 反例：活跃宝箱跟着主动任务补领，一天可以跑多次
    assert "daily_box" not in task_spec.ONCE_PER_DAY_TASKS
    # 只读状态类任务不该被卷进来（它们每次登录都跑一次也无所谓，但没登记就没登记）
    assert "bbq_status" not in task_spec.ONCE_PER_DAY_TASKS
