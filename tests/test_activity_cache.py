"""``models/activity_cache.py`` 的守护测试：刷新时刻怎么算、何时算过期、坏文件怎么办。

【为什么这些用例值得单独写】
"每周三 20:00 刷新"这条规则错了，症状是**静默的**：界面照常显示区域，
只是永远停在上一期 —— 用户只会觉得"这工具的数据是旧的"，而不会想到是这里。
所以边界（周三 19:59 / 20:00 / 20:01、跨周、兜底天数、换号）全部钉死。

运行方式（两种都支持，与仓库其它测试一致）：

    python -m tests.test_activity_cache
    python -m pytest tests/test_activity_cache.py -q
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import config
from models.activity_cache import (
    SCHEMA_VERSION,
    ActivityCache,
    format_timestamp,
    next_refresh_after,
)
from models.activity_stage import DISPLAY_TZ


def a_wednesday() -> datetime:
    """返回一个**确定的周三 00:00**（不写死日期，避免"记错了某天是周几"）。

    从 2026-01-01 往后找第一个周三 —— 基准日期由代码算出来，
    与 :data:`config.ACTIVITY_REFRESH_WEEKDAY` 永远一致。
    """
    moment = datetime(2026, 1, 1, tzinfo=DISPLAY_TZ)
    while moment.weekday() != config.ACTIVITY_REFRESH_WEEKDAY:
        moment += timedelta(days=1)
    return moment


def at(hour: int, minute: int = 0, *, weeks: int = 0) -> datetime:
    """周三的某个时刻（``weeks`` 可偏移到下一周）。"""
    return a_wednesday().replace(hour=hour, minute=minute) + timedelta(weeks=weeks)


# ---------------------------------------------------------------------------
# ① 「下一个周三 20:00」的计算
# ---------------------------------------------------------------------------
class TestNextRefresh:
    def test_wednesday_before_hour_is_today(self) -> None:
        """周三 19:59 → **今天** 20:00（马上就该刷）。"""
        assert next_refresh_after(at(19, 59)) == at(20, 0)

    def test_exactly_at_hour_is_next_week(self) -> None:
        """周三 20:00 整 → 下周三（这一刻已属于"本期更新过"）。"""
        assert next_refresh_after(at(20, 0)) == at(20, 0, weeks=1)

    def test_wednesday_after_hour_is_next_week(self) -> None:
        """周三 20:01 → 下周三。"""
        assert next_refresh_after(at(20, 1)) == at(20, 0, weeks=1)

    def test_monday_goes_to_this_wednesday(self) -> None:
        moment = at(20, 0) - timedelta(days=2)  # 周一
        assert moment.weekday() == 0
        assert next_refresh_after(moment) == at(20, 0)

    def test_sunday_goes_to_next_wednesday(self) -> None:
        moment = at(20, 0) + timedelta(days=4)  # 周日
        assert moment.weekday() == 6
        assert next_refresh_after(moment) == at(20, 0, weeks=1)

    def test_naive_datetime_is_read_as_display_timezone(self) -> None:
        """朴素时间按 UTC+8 解释（否则"周三 20:00"会在不同机器上差 8 小时）。"""
        naive = at(12, 0).replace(tzinfo=None)
        assert next_refresh_after(naive) == at(20, 0)


# ---------------------------------------------------------------------------
# ② 何时算过期
# ---------------------------------------------------------------------------
class TestNeedsRefresh:
    @staticmethod
    def fresh_cache(
        *, now: float, player_id: int = 10000003, server_id: int = 113
    ) -> ActivityCache:
        cache = ActivityCache()
        cache.store_areas(
            [{"area_id": 128110053, "name": "可疑的工作"}],
            player_id=player_id,
            server_id=server_id,
            now=now,
        )
        return cache

    def test_empty_cache_needs_refresh(self) -> None:
        need, reason = ActivityCache().needs_refresh()
        assert need is True
        assert "还没有区域数据" in reason

    def test_fresh_cache_is_reused(self) -> None:
        now = at(12, 0).timestamp()
        cache = self.fresh_cache(now=now)
        need, reason = cache.needs_refresh(
            player_id=10000003, server_id=113, now=now + 60
        )
        assert need is False
        # 原因不是空串：界面直接显示它（"数据是什么时候的、下次什么时候自动更新"）
        assert "下次自动更新" in reason

    def test_after_refresh_moment_needs_refresh(self) -> None:
        now = at(19, 0).timestamp()
        cache = self.fresh_cache(now=now)
        later = at(20, 30).timestamp()
        need, reason = cache.needs_refresh(player_id=10000003, server_id=113, now=later)
        assert need is True
        assert "已过自动更新时刻" in reason

    def test_other_player_invalidates(self) -> None:
        """换号必须重拉：区域进度（5/5）、星数是**账号数据**。"""
        now = at(12, 0).timestamp()
        cache = self.fresh_cache(now=now)
        need, reason = cache.needs_refresh(player_id=999, server_id=113, now=now + 60)
        assert need is True
        assert "另一个角色" in reason

    def test_other_server_invalidates(self) -> None:
        now = at(12, 0).timestamp()
        cache = self.fresh_cache(now=now)
        need, reason = cache.needs_refresh(
            player_id=10000003, server_id=167, now=now + 60
        )
        assert need is True
        assert "另一个区服" in reason

    def test_unknown_player_does_not_invalidate(self) -> None:
        """读不到会话（``player_id=0``）时**不**判过期 —— 否则会"永远在重拉"。"""
        now = at(12, 0).timestamp()
        cache = self.fresh_cache(now=now)
        need, _ = cache.needs_refresh(player_id=0, server_id=0, now=now + 60)
        assert need is False

    def test_max_age_is_the_last_resort(self) -> None:
        """兜底有效期：即使刷新点还没到（例如猜错了更新日），也不许无限旧下去。"""
        now = at(12, 0).timestamp()
        cache = ActivityCache(
            areas=[{"area_id": 1}],
            player_id=0,
            server_id=0,
            fetched_at=now - (config.ACTIVITY_CACHE_MAX_AGE_DAYS + 1) * 86400,
            next_refresh_at=now + 86400,  # 刷新点还在未来 → 只能靠兜底拦
        )
        need, reason = cache.needs_refresh(now=now)
        assert need is True
        assert "超过" in reason

    def test_old_schema_version_needs_refresh(self) -> None:
        cache = ActivityCache(areas=[{"area_id": 1}], schema_version=0)
        need, reason = cache.needs_refresh()
        assert need is True
        assert "版本" in reason


# ---------------------------------------------------------------------------
# ③ 存储 / 读回
# ---------------------------------------------------------------------------
class TestPersistence:
    def test_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / ".activity_cache.json"
        cache = ActivityCache()
        cache.store_areas(
            [{"area_id": 128110053, "name": "可疑的工作", "progress": "5/5"}],
            player_id=10000003,
            server_id=113,
            now=at(21, 0).timestamp(),
        )
        cache.store_sections(
            128110053,
            [{"section_id": 129110539, "energy_cost": 40, "passed": True}],
            [{"section_id": 129110537, "reason": "不消耗体力"}],
        )
        assert cache.save(path) == path

        loaded = ActivityCache.load(path)
        assert loaded.player_id == 10000003
        assert loaded.server_id == 113
        assert loaded.schema_version == SCHEMA_VERSION
        assert loaded.areas_view()[0]["area_id"] == 128110053
        assert loaded.sections_view(128110053)[0]["section_id"] == 129110539
        assert loaded.blocked_view(128110053)[0]["reason"] == "不消耗体力"
        assert loaded.next_refresh_at == cache.next_refresh_at

    def test_store_areas_clears_section_cache(self) -> None:
        """换期后旧的关卡号在新一期里可能不存在 → 必须连带清掉。"""
        cache = ActivityCache()
        cache.store_sections(128110053, [{"section_id": 1}], [])
        cache.store_areas([{"area_id": 222222222}], player_id=1, server_id=1, now=1.0)
        assert cache.sections_view(128110053) == []
        assert cache.blocked_view(128110053) == []

    def test_missing_file_is_empty_cache(self, tmp_path: Path) -> None:
        assert ActivityCache.load(tmp_path / "nope.json").is_empty

    def test_broken_file_is_empty_cache(self, tmp_path: Path) -> None:
        """半个 JSON（例如被手工编辑坏）只会让这次"当作没有缓存"，绝不抛错。"""
        path = tmp_path / "broken.json"
        path.write_text("{ 这不是 JSON", encoding="utf-8")
        assert ActivityCache.load(path).is_empty

    def test_non_object_json_is_ignored(self, tmp_path: Path) -> None:
        path = tmp_path / "list.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        assert ActivityCache.load(path).is_empty

    def test_unknown_schema_version_is_ignored(self, tmp_path: Path) -> None:
        path = tmp_path / "old.json"
        path.write_text(
            json.dumps({"schema_version": 0, "areas": [{"area_id": 1}]}),
            encoding="utf-8",
        )
        assert ActivityCache.load(path).is_empty

    def test_dirty_values_do_not_crash(self, tmp_path: Path) -> None:
        """字段形状不对（人为编辑 / 旧版本写法）时退回默认值，而不是让页面报错。"""
        path = tmp_path / "dirty.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "player_id": "不是数字",
                    "areas": "不是列表",
                    "sections": 5,
                    "blocked": {"128110053": [1, {"section_id": 2}]},
                }
            ),
            encoding="utf-8",
        )
        loaded = ActivityCache.load(path)
        assert loaded.player_id == 0
        assert loaded.areas == []
        assert loaded.sections == {}
        # 列表里非对象的元素被丢掉，对象的留下
        assert loaded.blocked_view(128110053) == [{"section_id": 2}]

    def test_save_leaves_no_temp_file(self, tmp_path: Path) -> None:
        """原子写：写完不能留下 ``.tmp``（否则下次会看到一个疑似"半个缓存"的文件）。"""
        path = tmp_path / ".activity_cache.json"
        cache = ActivityCache()
        cache.store_areas(
            [{"area_id": 1}], player_id=0, server_id=0, now=at(21, 0).timestamp()
        )
        cache.save(path)
        leftovers = [item.name for item in tmp_path.iterdir() if item.name.endswith(".tmp")]
        assert leftovers == []

    def test_format_timestamp_handles_unset(self) -> None:
        assert format_timestamp(0) == "<未设置>"
        assert format_timestamp(-1) == "<未设置>"
        assert format_timestamp(at(21, 30).timestamp()).endswith("21:30")


if __name__ == "__main__":  # 支持 `python -m tests.test_activity_cache`
    raise SystemExit(pytest.main([__file__, "-v", "--no-header"]))
