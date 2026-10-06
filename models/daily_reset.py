"""游戏日（daily reset）的口径：**每天凌晨 05:00（UTC+8）换一天**。

【它解决什么问题（2026-09-24 用户需求）】
网页上的"每日任务"改成**勾选即自动执行**之后，必须回答一个问题：
"今天到底跑过没有？" 判错的两种后果都不小：

- 算早了（按 00:00 换日）→ 半夜 00:30 打开页面会**提前重跑一遍**；
- 算晚了（按 12:00 换日之类）→ 早上 07:00 打开，界面说"今天已完成"，
  而实际上一件都没做（用户会以为工具坏了）。

用户实测游戏服务器在**每天凌晨 05:00** 刷新日常，所以"游戏日"从 05:00 起算：
``04:59 → 前一天``、``05:00 → 当天``。

【为什么前端不自己算】
与"区域数据上次更新 / 下次更新"同一个理由：时间文本一律由**后端**算好
（``/api/tasks`` 的 ``game_day`` / ``next_daily_reset`` 字段）。
前端各自算一套时区，迟早出现"界面说今天已跑过、后端却还在跑"这种自相矛盾。
见 ``notes/web_layer.md`` 里关于时间展示口径的说明。

【纯函数、可直接单测】
:func:`game_day` 与 :func:`next_reset_after` 都接受 ``now`` 参数，测试直接传时间
即可，不需要冻结时钟 —— 与 ``models/activity_cache.py`` 的 ``next_refresh_after``
同一套做法。换日时刻取自 :data:`config.DAILY_RESET_HOUR`，游戏改点了只改配置。

【宴席日：与游戏日**独立**的第二套换日口径（2026-10-05 新增 / 2026-10-06 修正）】
宴席的早晚两顿是 06:10–13:59 与 14:00–次日06:00，**晚宴席跨过 05:00 换日点**。
若拿游戏日当"今天"，昨晚 05:00 之后还开着的晚宴席会被判成"新的一天"，
导致"已吃过"的记录被作废、允许重复发包。因此宴席单独用 :func:`banquet_day`。

**★ 2026-10-06 修正：换日点是 06:10，不是 06:00。**
晚宴席 06:00 结束、当天第一顿（早宴席）**06:10** 才刷新 —— 中间 **06:00–06:10 是空档期**，
两顿都不可吃（用户 2026-10-06 确认）。换日点因此跟着**第一顿的刷新时刻**（06:10）走：
这样"换日"与"新的一天第一顿开放"是同一个瞬间，不会出现"已换日却还没得吃"的夹缝；
06:00–06:10 仍算**前一宴席日**的尾巴。空档期判定见 :func:`in_banquet_gap`。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Final

import config
from models.activity_stage import DISPLAY_TZ

#: 游戏日的比较键格式（``2026-09-24``）。只用于"是不是同一个游戏日"的比对。
_DAY_FORMAT: Final[str] = "%Y-%m-%d"

#: 下一次重置的展示格式（``09-24 05:00``）—— 给人看，带月日就不会有歧义。
_RESET_FORMAT: Final[str] = "%m-%d %H:%M"

#: 宴席日的换日**时刻**（UTC+8）：**06:10**，= 当天第一顿（早宴席）刷新的时刻。
#:
#: 【为什么是 06:10，而不是 06:00、也不复用 :data:`config.DAILY_RESET_HOUR`（05:00）】
#: 1. 宴席的早/晚两顿是"06:10–13:59"与"14:00–次日06:00"—— 晚宴席**跨过 05:00**。
#:    若复用游戏日（05:00 换日），晚宴席在 05:00–06:00 这一小时里会被判成
#:    "已经进入新的一天"，于是"昨晚吃过的晚宴席"记录被作废、允许再发一次包。
#: 2. 晚宴席 **06:00** 结束、早宴席 **06:10** 刷新，中间 **06:00–06:10 是空档期**
#:    （用户 2026-10-06 确认"这 10 分钟两顿都不可吃"）。换日点跟着**第一顿的刷新时刻**
#:    走，于是"换日"与"新的一天第一顿开放"是同一个瞬间；06:00–06:10 仍算前一宴席日的尾巴。
BANQUET_RESET_HOUR: Final[int] = int(
    os.getenv("CHERRYTALE_BANQUET_RESET_HOUR", "6") or "6"
)

#: 宴席日换日的**分钟**位（配合 :data:`BANQUET_RESET_HOUR`，默认 **6:10**）。
BANQUET_RESET_MINUTE: Final[int] = int(
    os.getenv("CHERRYTALE_BANQUET_RESET_MINUTE", "10") or "10"
)


def _moment(now: datetime | None) -> datetime:
    """把 ``now`` 归一成**带时区**的时刻（``None`` = 现在）。

    传进来的朴素时间按展示时区（``DISPLAY_TZ`` = UTC+8）解释 —— 与
    ``models/activity_cache.py`` 的 ``next_refresh_after`` 保持一致，
    这样测试可以直接写 ``datetime(2026, 9, 24, 4, 59)``，不必关心时区细节。
    """
    moment = now if now is not None else datetime.now(DISPLAY_TZ)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=DISPLAY_TZ)
    return moment


def game_day(now: datetime | None = None) -> str:
    """``now`` 所属的**游戏日**，形如 ``"2026-09-24"``。

    :param now: 计算基准；``None`` 表示"现在"。测试直接传时间即可。

    【判定规则（本文件是这条规则的唯一定义处）】
    - 09-24 04:59 → ``"2026-09-23"``（还没换日，仍算前一天）；
    - 09-24 05:00 → ``"2026-09-24"``（刚好换日）；
    - 09-24 05:01 → ``"2026-09-24"``。
    """
    moment = _moment(now)
    if moment.hour < config.DAILY_RESET_HOUR:
        # 凌晨重置之前的一段时间属于**前一天**的尾巴。
        moment -= timedelta(days=1)
    return moment.strftime(_DAY_FORMAT)


def next_reset_after(now: datetime | None = None) -> datetime:
    """``now`` 之后**最近的**一次日常重置（每天 :data:`config.DAILY_RESET_HOUR` 点）。

    :param now: 计算基准；``None`` 表示"现在"。

    【规则】
    - 09-24 04:59 → **今天** 05:00（还没到）；
    - 09-24 05:01 → **明天** 05:00（今天那个点已经过去）；
    - 时间带上时区（``DISPLAY_TZ``），调用方 ``strftime`` / 比较都不会有歧义。
    """
    moment = _moment(now)
    candidate = moment.replace(
        hour=config.DAILY_RESET_HOUR,
        minute=0,
        second=0,
        microsecond=0,
    )
    if candidate <= moment:
        candidate += timedelta(days=1)
    return candidate


def format_reset(now: datetime | None = None) -> str:
    """把 :func:`next_reset_after` 格式化成 ``09-24 05:00``（界面直接显示这一句）。"""
    return next_reset_after(now).strftime(_RESET_FORMAT)


def banquet_day(now: datetime | None = None) -> str:
    """``now`` 所属的**宴席日**，形如 ``"2026-10-03"``。

    :param now: 计算基准；``None`` 表示"现在"。测试直接传时间即可。

    【它和 :func:`game_day` 的区别（本函数存在的唯一理由）】
    宴席的早晚两顿是 **06:10–13:59** 与 **14:00–次日 06:00**，
    而游戏日是 **05:00** 换日 —— 晚宴席**跨过了游戏日的换日点**。
    若拿游戏日当宴席的"今天"，昨晚 05:00 之后还开着的晚宴席会被判成
    "新的一天"，于是"已吃过"的记录被作废、允许重复发包。
    所以宴席单独用 **06:10** 为界（= 早宴席的刷新时刻），与游戏日**独立**。

    【判定规则（★ 2026-10-06 由 06:00 改为 06:10）】
    - 10-03 05:59 → ``"2026-10-02"``（晚宴席还没结束，仍算前一天）；
    - 10-03 06:00 → ``"2026-10-02"``（**仍在空档期**，还没到第一顿刷新点）；
    - 10-03 06:09 → ``"2026-10-02"``（空档期尾巴）；
    - 10-03 06:10 → ``"2026-10-03"``（换日 = 早宴席开放）；
    - 10-03 07:00 → ``"2026-10-03"``（早宴席窗口）。

    .. note::
       时刻来自 :data:`BANQUET_RESET_HOUR` / :data:`BANQUET_RESET_MINUTE`（默认 **6:10**），
       可用环境变量 ``CHERRYTALE_BANQUET_RESET_HOUR`` /
       ``CHERRYTALE_BANQUET_RESET_MINUTE`` 覆盖 —— 游戏若改点，只改这两个值。
    """
    moment = _moment(now)
    if (moment.hour, moment.minute) < (BANQUET_RESET_HOUR, BANQUET_RESET_MINUTE):
        # 换日点之前属于**前一天的尾巴**：晚宴席（14:00–次日06:00）的收尾，
        # 外加 06:00–06:10 的空档期（两顿都不可吃，见 :func:`in_banquet_gap`）。
        moment -= timedelta(days=1)
    return moment.strftime(_DAY_FORMAT)


def in_banquet_gap(now: datetime | None = None) -> bool:
    """``now`` 是否落在宴席的**空档期**（每天 06:00–06:10，UTC+8）。

    :param now: 计算基准；``None`` 表示"现在"。测试直接传时间即可。
    :return: 空档期内 ``True``，否则 ``False``。

    【空档期是什么】
    晚宴席窗口到 **06:00** 结束，早宴席窗口 **06:10** 才刷新 —— 中间的 **06:00–06:10**
    两顿都不可吃（用户 2026-10-06 确认）。这 10 分钟里**不该发任何 ``15003``**：
    服务端此刻没有可参与的期，发了只会白挨一次拒绝。

    【为什么用本机时钟，而不是 32008 里的服务端时间戳】
    服务端下发的是**"下一次"**窗口（抓包实证：10-03 00:39 时它把早宴席列成 10-03 06:10），
    拿它反推"现在是不是空档"会随下发时机漂移 —— 比如 06:05 时晚宴席可能已被换成
    "今天 14:00–明天 06:00"的那一条，用它的 ``endDateTime`` 就完全推不出空档。
    而空档期本身是**每天固定的 10 分钟**，与 :func:`banquet_day` 的换日点同源，
    用同一个（本机）时钟口径最不容易自相矛盾。

    【判定规则】
    - 10-03 05:59 → ``False``（晚宴席还在跑）；10-03 06:00 → ``True``；
    - 10-03 06:09 → ``True``；10-03 06:10 → ``False``（早宴席已刷新）。
    """
    moment = _moment(now)
    start = moment.replace(
        hour=BANQUET_RESET_HOUR, minute=0, second=0, microsecond=0
    )
    end = moment.replace(
        hour=BANQUET_RESET_HOUR, minute=BANQUET_RESET_MINUTE, second=0, microsecond=0
    )
    return start <= moment < end
