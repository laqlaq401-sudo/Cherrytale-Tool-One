"""``models/daily_reset.py`` 的守护测试：游戏日的换日时刻（每天 05:00）怎么算。

【为什么这些边界必须钉死】
网页上的"每日任务自动执行"靠 :func:`game_day` 判断"今天跑过没有"，而这条规则
错了，症状是**静默偏一小时**：

- 算早了（按 00:00 换日）→ 半夜 00:30 打开页面会自动多领一遍；
- 算晚了 → 早上 07:00 打开，界面说"今天已完成"，其实一件都没做。

所以 04:59 / 05:00 / 05:01、跨月、朴素时间、改配置全部覆盖。

运行方式（两种都支持，与仓库其它测试一致）：

    python -m tests.test_daily_reset
    python -m pytest tests/test_daily_reset.py -q
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import config
from models.activity_stage import DISPLAY_TZ
from models.daily_reset import (
    BANQUET_RESET_HOUR,
    BANQUET_RESET_MINUTE,
    banquet_day,
    format_reset,
    game_day,
    in_banquet_gap,
    next_reset_after,
)


def at(hour: int, minute: int = 0, *, day: int = 24) -> datetime:
    """2026-09-``day`` 的某个时刻（带展示时区，与后端算出来的一致）。"""
    return datetime(2026, 9, day, hour, minute, tzinfo=DISPLAY_TZ)


def at_oct(hour: int, minute: int = 0, *, day: int = 3) -> datetime:
    """2026-10-``day`` 的某个时刻（宴席测试用 —— 抓包发生在 10 月初）。"""
    return datetime(2026, 10, day, hour, minute, tzinfo=DISPLAY_TZ)


class TestGameDay:
    """游戏日：05:00 之前算前一天，05:00 起算当天。"""

    def test_just_before_reset_belongs_to_previous_day(self) -> None:
        assert game_day(at(4, 59)) == "2026-09-23"

    def test_exactly_at_reset_is_the_new_day(self) -> None:
        assert game_day(at(config.DAILY_RESET_HOUR, 0)) == "2026-09-24"

    def test_just_after_reset_is_the_new_day(self) -> None:
        assert game_day(at(5, 1)) == "2026-09-24"

    def test_midnight_is_not_a_reset_point(self) -> None:
        """00:00 **不是**换日点 —— 这条最容易被想当然写错。"""
        assert game_day(at(0, 30)) == "2026-09-23"

    def test_month_boundary(self) -> None:
        """10-01 04:00 仍属 09-30（跨月也要对）。"""
        assert game_day(datetime(2026, 10, 1, 4, 0, tzinfo=DISPLAY_TZ)) == "2026-09-30"

    def test_naive_datetime_is_read_in_display_timezone(self) -> None:
        """测试里传朴素时间也能用：按 DISPLAY_TZ（UTC+8）解释。"""
        assert game_day(datetime(2026, 9, 24, 4, 59)) == "2026-09-23"

    def test_reset_hour_follows_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """换日时刻来自配置：游戏改点了只改 config.DAILY_RESET_HOUR。"""
        monkeypatch.setattr(config, "DAILY_RESET_HOUR", 7)
        assert game_day(at(6, 30)) == "2026-09-23"
        assert game_day(at(7, 0)) == "2026-09-24"


class TestBanquetDay:
    """宴席日：**06:10 换日**，与游戏日 05:00 独立（2026-10-05 新增 / 10-06 修正）。

    【为什么必须和游戏日分开】
    晚宴席是 14:00–次日 06:00，**跨过 05:00**。若复用游戏日，
    昨晚 05:00 之后还开着的晚宴席会被判成"新的一天"，"已吃过"的记录被作废、
    允许重复发包。所以宴席单独以 **06:10**（= 早宴席刷新时刻）为界。

    【★ 为什么是 06:10 而不是 06:00（2026-10-06 修正）】
    晚宴席 06:00 结束、早宴席 06:10 才刷新，**06:00–06:10 是空档期**（两顿都不可吃）。
    换日点跟着"第一顿刷新"走，才能做到"换日 = 新的一天第一顿开放"。
    """

    def test_just_before_banquet_reset_belongs_to_previous_day(self) -> None:
        # 10-03 05:59：晚宴席（10-02 14:00–10-03 06:00）还没结束 → 仍算 10-02。
        assert banquet_day(at_oct(5, 59, day=3)) == "2026-10-02"

    def test_gap_start_still_belongs_to_previous_day(self) -> None:
        """★ 06:00（空档期起点）**还没换日** —— 旧的 06:00 口径在这里就错了。"""
        assert banquet_day(at_oct(6, 0, day=3)) == "2026-10-02"

    def test_gap_tail_still_belongs_to_previous_day(self) -> None:
        assert banquet_day(at_oct(6, 9, day=3)) == "2026-10-02"

    def test_exactly_at_banquet_reset_is_the_new_day(self) -> None:
        """★ 换日点是 06:10（= 早宴席刷新时刻）。"""
        assert banquet_day(at_oct(6, 10, day=3)) == "2026-10-03"

    def test_reset_minute_constant_is_used(self) -> None:
        """换日的分钟位取自 :data:`BANQUET_RESET_MINUTE`。"""
        assert BANQUET_RESET_MINUTE == 10

    def test_early_morning_before_dawn_still_previous_day(self) -> None:
        # 10-03 00:30 属于晚宴席（10-02 那天的第二顿）→ 算 10-02。
        assert banquet_day(at_oct(0, 30, day=3)) == "2026-10-02"

    def test_morning_meal_and_evening_meal_share_the_same_banquet_day(self) -> None:
        """同一天的早宴席（06:10–13:59）与晚宴席（14:00–次日06:00）必须是同一个宴席日。"""
        morning = banquet_day(at_oct(10, 0, day=3))  # 早宴席窗口内
        evening = banquet_day(at_oct(20, 0, day=3))  # 晚宴席窗口内（当天）
        late_night = banquet_day(at_oct(2, 0, day=4))  # 晚宴席窗口内（次日凌晨）
        assert morning == evening == late_night == "2026-10-03"

    def test_differs_from_game_day_across_reset(self) -> None:
        """最重要的一条：05:00–06:10 之间，游戏日已换、宴席日未换。"""
        moment = at_oct(5, 30, day=3)
        assert game_day(moment) == "2026-10-03"  # 游戏日：已换
        assert banquet_day(moment) == "2026-10-02"  # 宴席日：还没换

    def test_reset_time_follows_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """小时与**分钟**都跟着常量走 —— 游戏改点只改这两个值。"""
        monkeypatch.setattr("models.daily_reset.BANQUET_RESET_HOUR", 7)
        monkeypatch.setattr("models.daily_reset.BANQUET_RESET_MINUTE", 30)
        assert banquet_day(at_oct(7, 29, day=3)) == "2026-10-02"
        assert banquet_day(at_oct(7, 30, day=3)) == "2026-10-03"

    def test_month_boundary(self) -> None:
        """11-01 05:00 仍属 10-31（跨月也要对；05:00 早于 06:10）。"""
        assert banquet_day(datetime(2026, 11, 1, 5, 0, tzinfo=DISPLAY_TZ)) == "2026-10-31"


class TestBanquetGap:
    """空档期：每天 **06:00–06:10** 两顿都不可吃（2026-10-06 用户确认）。

    【为什么要有它】
    晚宴席 06:00 结束、早宴席 06:10 刷新，中间这 10 分钟服务端没有可参与的期。
    任务层拿它挡在**组包之前** —— 空档期一个 ``15003`` 都不发。
    """

    def test_before_gap_is_not_gap(self) -> None:
        # 05:59 晚宴席还在跑。
        assert in_banquet_gap(at_oct(5, 59, day=3)) is False

    def test_gap_start_is_gap(self) -> None:
        assert in_banquet_gap(at_oct(6, 0, day=3)) is True

    def test_gap_middle_is_gap(self) -> None:
        assert in_banquet_gap(at_oct(6, 5, day=3)) is True

    def test_gap_tail_is_gap(self) -> None:
        assert in_banquet_gap(at_oct(6, 9, day=3)) is True

    def test_gap_end_is_not_gap(self) -> None:
        """06:10 早宴席已刷新 → 不再算空档。"""
        assert in_banquet_gap(at_oct(6, 10, day=3)) is False

    def test_other_hours_are_not_gap(self) -> None:
        for hour in (0, 5, 7, 13, 14, 23):
            assert in_banquet_gap(at_oct(hour, 0, day=3)) is False

    def test_naive_time_is_interpreted_in_display_tz(self) -> None:
        """传朴素时间按 UTC+8 解释（与 banquet_day 同一口径）。"""
        assert in_banquet_gap(datetime(2026, 10, 3, 6, 5)) is True
        assert in_banquet_gap(datetime(2026, 10, 3, 5, 55)) is False

    def test_gap_follows_constants(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("models.daily_reset.BANQUET_RESET_HOUR", 7)
        monkeypatch.setattr("models.daily_reset.BANQUET_RESET_MINUTE", 20)
        assert in_banquet_gap(at_oct(7, 10, day=3)) is True
        assert in_banquet_gap(at_oct(7, 20, day=3)) is False


class TestNextReset:
    """下一次重置的时刻（界面显示"下次 09-24 05:00 重置"）。"""

    def test_before_reset_points_to_today(self) -> None:
        assert next_reset_after(at(4, 59)) == at(config.DAILY_RESET_HOUR, 0)

    def test_after_reset_points_to_tomorrow(self) -> None:
        expected = at(config.DAILY_RESET_HOUR, 0) + timedelta(days=1)
        assert next_reset_after(at(5, 1)) == expected

    def test_is_always_in_the_future(self) -> None:
        """一天里任何整点算出来的"下次重置"都必须晚于现在（否则界面上会出现负数倒计时）。"""
        for hour in range(24):
            assert next_reset_after(at(hour)) > at(hour)

    def test_format_is_readable(self) -> None:
        assert format_reset(at(4, 59)) == "09-24 05:00"
        assert format_reset(at(23, 0)) == "09-25 05:00"


if __name__ == "__main__":  # 支持 `python -m tests.test_daily_reset`
    raise SystemExit(pytest.main([__file__, "-v", "--no-header"]))
