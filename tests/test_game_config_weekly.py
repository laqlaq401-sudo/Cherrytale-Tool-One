"""周常任务 / 周常活跃宝箱配置表的单元测试（``models/game_config.py`` 的周常部分）。

运行方式：

    python -m pytest tests/test_game_config_weekly.py -q
    python -m tests.test_game_config_weekly

【为什么单独测"周常"】
日常与周常的表结构几乎一样、**ID 段却完全不同**：

    ============  ==========================  ==========================
    项目           日常                        周常
    ============  ==========================  ==========================
    任务 ID        ``dmID`` 24xxxxxxx         ``wmID`` 25xxxxxxx
    宝箱 ID        ``dmrID`` 939000001~5      ``wmrID`` 251000000~3
    活跃点列        ``addPoint``               ``addPoint``（同一列名）
    ============  ==========================  ==========================

把两套 ID 搞混是这类接口最常见的错误，而服务端只会回一个参数错误。
所以这里既测"读得出来"，也测"读出来的确实是周常那一段 ID"。
"""

from __future__ import annotations

import pytest

from models.game_config import (
    TABLE_WEEKLY_MISSION,
    TABLE_WEEKLY_MISSION_REWARD,
    boxes_reachable,
    load_weekly_missions,
    load_weekly_reward_boxes,
    table_path,
    total_active_point,
)


def has(path_name: str) -> bool:
    """本地素材目录里是否有这张表（缺素材时跳过用例）。"""
    try:
        return table_path(path_name).is_file()
    except (FileNotFoundError, OSError):
        return False


needs_weekly_tables = pytest.mark.skipif(
    not (has(TABLE_WEEKLY_MISSION) and has(TABLE_WEEKLY_MISSION_REWARD)),
    reason="本地素材目录里没有周常配置表",
)


@needs_weekly_tables
class TestWeeklyMissionTable:
    def test_reads_missions_with_weekly_id_range(self) -> None:
        missions = load_weekly_missions()
        assert missions, "周常任务表不该是空的"
        assert all(mission.mission_id >= 25_0000_000 for mission in missions)

    def test_fields_are_translated(self) -> None:
        """抽查第一条：需要次数与活跃点必须被正确读出来。"""
        first = load_weekly_missions()[0]
        assert first.required_count >= 1
        assert first.add_point >= 0
        assert first.line_no >= 2  # 第 1 行是表头

    def test_index_by_id_is_possible(self) -> None:
        missions = load_weekly_missions()
        by_id = {mission.mission_id: mission for mission in missions}
        assert len(by_id) == len(missions)


@needs_weekly_tables
class TestWeeklyBoxTable:
    def test_reads_boxes_with_weekly_id_range(self) -> None:
        boxes = load_weekly_reward_boxes()
        assert boxes, "周常宝箱表不该是空的"
        assert all(251_000_000 <= box.box_id <= 251_000_999 for box in boxes)

    def test_required_points_are_sorted_ascending(self) -> None:
        points = [box.required_point for box in load_weekly_reward_boxes()]
        assert points == sorted(points)


class TestActivePoint:
    """活跃点计算（日常与周常共用同一函数）。"""

    def test_counts_only_completed_missions(self) -> None:
        from models.game_config import WeeklyMission

        missions = [
            WeeklyMission(  # 需要 5 次、给 1 点
                mission_id=1,
                title=None,
                description=None,
                action_id=None,
                action_target=None,
                required_count=5,
                add_point=1,
                reward_tip_item=None,
                vip_require=0,
                sort=0,
                raw={},  # type: ignore[arg-type]
            ),
            WeeklyMission(
                mission_id=2,
                title=None,
                description=None,
                action_id=None,
                action_target=None,
                required_count=1,
                add_point=2,
                reward_tip_item=None,
                vip_require=0,
                sort=1,
                raw={},  # type: ignore[arg-type]
            ),
        ]
        # 第 1 条只做了 4/5 次 → 不给分；第 2 条做完 → 给 2 分
        assert total_active_point(missions, {1: 4, 2: 1}) == 2
        # 都做完 → 3 分
        assert total_active_point(missions, {1: 5, 2: 1}) == 3
        # 服务端没有任何记录 → 0 分（不做本地推测）
        assert total_active_point(missions, {}) == 0


class TestBoxesReachable:
    def test_only_boxes_within_point(self) -> None:
        from models.game_config import WeeklyMissionRewardBox

        boxes = [
            WeeklyMissionRewardBox(box_id=1, required_point=1, box_img_type=0, raw={}),  # type: ignore[arg-type]
            WeeklyMissionRewardBox(box_id=2, required_point=3, box_img_type=1, raw={}),  # type: ignore[arg-type]
            WeeklyMissionRewardBox(box_id=3, required_point=5, box_img_type=2, raw={}),  # type: ignore[arg-type]
        ]
        assert boxes_reachable(boxes, 0) == []
        assert boxes_reachable(boxes, 3) == [1, 2]
        assert boxes_reachable(boxes, 5) == [1, 2, 3]


if __name__ == "__main__":  # 支持 `python -m tests.test_game_config_weekly`
    raise SystemExit(pytest.main([__file__, "-v", "--no-header"]))
