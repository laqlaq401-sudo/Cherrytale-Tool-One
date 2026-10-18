"""荣耀之巅只读任务（``tasks/top_pvp_status.py``）的安全边界测试。

【它为什么必须存在（★ 2026-10-18）】
``top_pvp_battle`` 的 ``battles=0`` 已改成"打到次数用完"，于是它**没有任何
只读分支**。``/api/player?full=1`` 的信息面板要读"我现在第几名、多少分"，
就必须有一个**结构上打不了一场**的入口 —— 本任务的白名单里只有 39001，
39007（挑战）/ 39019（写开关）/ 39011（进入模式）全都不在 allowed 集合里，
``guarded_send`` 会直接抛错。

这套测试用与战斗任务同一套"记账式传输替身"：从**请求字节**里解出消息号，
而不是相信调用点的写法。

运行方式：

    python -m pytest tests/test_tasks_top_pvp_status.py -q
"""

from __future__ import annotations

from pathlib import Path

import pytest

import tasks.top_pvp_status as status_task  # noqa: F401  导入即注册
from tasks.base import RunOptions, create_task
from tests.test_tasks_top_pvp import (
    CHALLENGE_ID,
    HOME_ID,
    TEAM_EDIT_ID,
    TOGGLE_ID,
    envelope_bytes,
    home_payload,
    make_session,
    patch_transport,
    sent_packet_ids,
)


def make_task(**kwargs: object):
    """构造只读任务（不需要运行参数，这里给一份默认值只是为了接口一致）。"""
    return create_task(
        "top_pvp_status",
        make_session(),
        run_options=RunOptions(),
        **kwargs,
    )


def test_sends_only_home_packet(monkeypatch: pytest.MonkeyPatch) -> None:
    """整个流程只该发一个 39001。"""
    recorded = patch_transport(
        monkeypatch, lambda number, body: envelope_bytes(HOME_ID, home_payload())
    )
    result = make_task().run()

    assert result.ok is True
    assert sent_packet_ids(recorded) == [HOME_ID]


def test_never_sends_challenge_or_write_packets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """挑战 / 写开关 / 进入模式三个包一个都不许出现。"""
    recorded = patch_transport(
        monkeypatch, lambda number, body: envelope_bytes(HOME_ID, home_payload())
    )
    make_task().run()
    ids = set(sent_packet_ids(recorded))

    assert CHALLENGE_ID not in ids
    assert TOGGLE_ID not in ids
    assert TEAM_EDIT_ID not in ids


def test_result_is_marked_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """数据里要标 ``mode=read_only`` —— 信息面板据此区分"读到的是什么"。"""
    patch_transport(
        monkeypatch, lambda number, body: envelope_bytes(HOME_ID, home_payload())
    )
    result = make_task().run()

    assert result.data["mode"] == "read_only"
    assert "只读" in result.message


def test_source_has_no_battle_packets() -> None:
    """源码级回归：只读任务不该 import / 构造任何挑战或写操作的包。"""
    source = Path(str(status_task.__file__)).read_text(encoding="utf-8")

    assert "TopPvpChallengePacket" not in source
    assert "TopPvpSetSkipBattleTogglePacket" not in source
    assert "TopTeamEditInfoPacket" not in source


def test_registered_in_spec_as_hidden() -> None:
    """规格表里必须是隐藏的自动组任务（与 arena_status 等同构）。"""
    from services import task_spec

    spec = task_spec.TASK_SPECS["top_pvp_status"]
    assert spec.hidden is True
    assert spec.group == "auto"
