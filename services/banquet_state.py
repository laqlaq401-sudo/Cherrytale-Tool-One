"""宴席「早/晚两顿吃过了没有」的服务端记账（2026-10-05 新增）。

【它解决什么问题】
宴席一天分两顿（早 06:10–13:59、晚 14:00–次日06:00），每顿各自只能吃一次。
但**服务端不告诉我们"这顿吃没吃"** —— ``32008 GetBBQDataRes`` 的
``ActivityIntervalDataClass`` 只有 ``{dataNummber, groupID, startDateTime,
endDateTime, weeks}`` 五个字段，没有任何完成位（抓包实证，见
``notes/bbq_second_meal_plan.md``）。

所以"吃过就置灰、不重复发包"这件事必须由本工具自己记。记在**服务端**而不是
浏览器，理由与 ``services/daily_state.py`` 完全相同：localStorage 按 origin 隔离，
换端口 / 换窗口（桌面壳 vs Edge 回落）就丢了。

【与 services/daily_state.py 的三点不同（为什么另开一个模块）】
1. **换日口径不同**：这里用 :func:`models.daily_reset.banquet_day`（**06:00** 换日），
   而不是游戏日（05:00）—— 晚宴席 14:00–次日06:00 跨过 05:00，
   用游戏日会把"昨晚刚吃完的晚宴席"判成新的一天从而允许重复发包。
2. **记录内容不同**：那边是"任务名清单"，这里是"早/晚两个完成位"。
3. **用途不同**：那边的记录会让前端**跳过自动执行**；这里的记录只给**手动任务**
   界面置灰用（并顺带阻止重复发包），不参与"自动执行跳过"。

【文件格式】
::

    {
      "version": 1,
      "targets": {
        "s=12|p=345678": {
          "banquet_day": "2026-10-03",
          "meals": ["morning", "evening"]
        }
      }
    }

``banquet_day`` 与当前宴席日不一致 ⇒ 整体作废（跨日自动重置，不需要定时清理）。
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Mapping

import config
from models import daily_reset
from services.daily_state import identity_key

_LOGGER: Final[logging.Logger] = logging.getLogger("services.banquet_state")

#: 文件格式版本。将来结构变了就升版，旧文件整体丢弃（它没有长期价值）。
_FILE_VERSION: Final[int] = 1

#: 合法的餐次标识（与 ``tasks/bbq.py`` 的 ``MEAL_MORNING`` / ``MEAL_EVENING`` 对应）。
MEALS: Final[tuple[str, ...]] = ("morning", "evening")

_lock: Final[threading.Lock] = threading.Lock()


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------
def _path(path: Path | None = None) -> Path:
    """目标文件；``None`` 表示用 :data:`config.BANQUET_STATE_FILE`。"""
    return Path(path) if path is not None else config.BANQUET_STATE_FILE


def load(path: Path | None = None) -> dict[str, Any]:
    """读出整份记账（文件不存在 / 损坏 → 返回空结构）。

    【为什么损坏时返回空而不是抛异常】
    这份文件的价值只是"少发一次重复的包"，丢了最坏结果是多一次尝试；
    而抛异常会让 ``/api/tasks`` 整个 500 —— 页面直接打不开。两者不对等，
    所以静默降级，只留一条 WARNING（与 ``daily_state.load`` 同一取舍）。
    """
    target = _path(path)
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"version": _FILE_VERSION, "targets": {}}
    except OSError as exc:
        _LOGGER.warning("读取宴席记账失败（%s）：%s", target, exc)
        return {"version": _FILE_VERSION, "targets": {}}

    try:
        payload = json.loads(raw)
    except ValueError as exc:
        _LOGGER.warning("宴席记账不是合法 JSON（%s）：%s", target, exc)
        return {"version": _FILE_VERSION, "targets": {}}

    if not isinstance(payload, dict) or payload.get("version") != _FILE_VERSION:
        return {"version": _FILE_VERSION, "targets": {}}
    targets = payload.get("targets")
    if not isinstance(targets, dict):
        return {"version": _FILE_VERSION, "targets": {}}
    return {"version": _FILE_VERSION, "targets": targets}


def save(payload: dict[str, Any], path: Path | None = None) -> None:
    """整份写回（由本模块内部在持锁状态下调用）。

    写失败同样只记日志：没有写权限时退化成"每次都允许再试一次"，
    总好过把整个 HTTP 请求打挂。
    """
    target = _path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        _LOGGER.warning("写宴席记账失败（%s）：%s", target, exc)


def clear(path: Path | None = None) -> None:
    """清空全部记账（测试与"想重跑一遍"时用）。"""
    with _lock:
        save({"version": _FILE_VERSION, "targets": {}}, path)


# ---------------------------------------------------------------------------
# 对外的两个动作
# ---------------------------------------------------------------------------
def meals_done(
    session: Mapping[str, Any] | None = None,
    *,
    now: datetime | None = None,
    path: Path | None = None,
) -> tuple[str, ...]:
    """本宴席日（本角色本区服）**已经吃过**的餐次。

    :param session: ``runner.describe_session()`` 的结果；``None`` = 现查。
    :param now: 计算"今天是哪个宴席日"的基准（测试用；``None`` = 现在）。
    :param path: 记账文件（测试用；``None`` = 配置文件里的默认路径）。
    :return: 餐次元组（``"morning"`` / ``"evening"`` 的子集）；没有记录时为空元组。
    """
    key = identity_key(session)
    day = daily_reset.banquet_day(now)
    with _lock:
        payload = load(path)
    entry = payload.get("targets", {}).get(key)
    if not isinstance(entry, dict):
        return ()
    if str(entry.get("banquet_day", "")) != day:
        return ()
    meals = entry.get("meals")
    if not isinstance(meals, (list, tuple)):
        return ()
    return tuple(str(m) for m in meals if str(m) in MEALS)


def mark_meal_done(
    meal: str,
    session: Mapping[str, Any] | None = None,
    *,
    now: datetime | None = None,
    path: Path | None = None,
) -> tuple[str, ...]:
    """把某一顿记成"本宴席日已吃过"。

    :param meal: ``"morning"`` 或 ``"evening"``；其它值一律忽略（返回现有记录）。
    :return: 记账生效后，该角色本宴席日的完整已吃清单（便于日志直接打印）。

    【为什么只增不减】同一顿一天只会是"吃过"或"没吃过"两种状态，
    追加是幂等的；``list`` 去重后写回，重复调用不会把记录撑大。

    【为什么顺手做了"换日即覆盖"】本局宴席日与记录里的不一致 ⇒ 这是**新的一天**，
    旧清单整体作废，只从这一顿重新开始 —— 旧日期的记录因此自然消失，
    不需要额外的清理逻辑（隐性 bug 的常见温床）。

    【无会话时不记账】不知道"是谁"就写进去，只会在真正登录后影响不到任何东西，
    却可能让排障的人误以为"已经吃过"（与 ``daily_state.mark_completed`` 同一理由）。
    """
    if meal not in MEALS:
        return meals_done(session, now=now, path=path)
    key = identity_key(session)
    if key == "<no-session>":
        _LOGGER.debug("尚无游戏会话，跳过宴席记账：%s", key)
        return ()
    day = daily_reset.banquet_day(now)
    with _lock:
        payload = load(path)
        targets = payload.setdefault("targets", {})
        entry = targets.get(key)
        previous: tuple[str, ...] = ()
        if isinstance(entry, dict) and str(entry.get("banquet_day", "")) == day:
            raw = entry.get("meals")
            previous = tuple(str(m) for m in raw) if isinstance(raw, (list, tuple)) else ()
        merged = tuple(dict.fromkeys([m for m in (*previous, meal) if m in MEALS]))
        targets[key] = {"banquet_day": day, "meals": list(merged)}
        save(payload, path)
    return merged


def describe(
    session: Mapping[str, Any] | None = None,
    *,
    now: datetime | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    """给 ``/api/tasks`` 用的视图：``{banquet_day, meals}``。

    :return: ``{"banquet_day": "2026-10-03", "meals": ["morning"]}`` ——
        前端据 ``meals`` 把对应按钮置灰。``banquet_day`` 一并给出，
        是为了在排查"为什么按钮没灰"时能一眼看出用的是哪一天的口径。
    """
    return {
        "banquet_day": daily_reset.banquet_day(now),
        "meals": list(meals_done(session, now=now, path=path)),
    }
