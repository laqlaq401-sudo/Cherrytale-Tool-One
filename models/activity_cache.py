"""活动区域 / 关卡候选的**本地缓存**（``11025`` / ``11003`` 的只读快照）。

【它解决什么问题（2026-09-22 用户需求）】
降临关卡的区域与关卡**一周才轮换一次**，但网页上的两个下拉（活动区域 / 指定关卡）
每次都要那份清单。原实现是"每次打开页面都重新发 11025 与 11003"——
既没必要，也让风控多看见两个包。所以：

    第一次拉 → 落盘 → 之后直接用（**一个包都不发**）
    只有"过期"或用户点「更新区域」时才重拉

【什么算过期（三条，任一成立就刷新）】
1. 缓存为空（从没拉过）；
2. 换了账号（``player_id`` / ``server_id`` 与缓存不符）——
   区域进度（``5/5``）、星数是**账号数据**，换号必须重拉，否则显示上一个号的数据；
3. 到了刷新点：**下一个「周三 20:00」已过**（见 :func:`next_refresh_after`），
   或超过兜底 :data:`config.ACTIVITY_CACHE_MAX_AGE_DAYS` 天。

【★ 安全边界：本缓存**只服务界面**】
真正扫荡时 ``tasks/sweep.py`` 仍然现场发 11025 / 11003，用**最新**的
``sectionState`` 判断"是否已通关"。缓存里的关卡状态可能已经过时
（你刚通关的关卡在缓存里还是"未通关"）——拿它做判断会**误挡**真实可扫的关卡。
所以本模块不提供任何"给任务用的状态值"，只提供"给界面用的扁平视图"。

【读写为什么会失败也不要炸】
缓存是加速手段，不是真相来源。文件损坏 / 权限问题 / 半个 JSON 都只会
让这次"当作没有缓存"（重新拉一次），绝不能把任务或页面拖下水。
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

import config
from models.activity_stage import DISPLAY_TZ

_LOGGER: Final[logging.Logger] = logging.getLogger("models.activity_cache")

#: 缓存结构版本。字段含义变化时 +1：旧文件会被**直接忽略**（当作没有缓存），
#: 而不是让 ``from_dict`` 去猜一个已经不存在的字段。
SCHEMA_VERSION: Final[int] = 1

#: 时间展示格式（与 ``models.activity_stage.timestamp_to_text`` 保持一致）
_TIME_FORMAT: Final[str] = "%Y-%m-%d %H:%M"


def format_timestamp(value: float) -> str:
    """把 epoch 秒格式化成 ``2026-09-23 21:00``（``<= 0`` 视为"未设置"）。

    时区固定用 ``DISPLAY_TZ``（UTC+8，游戏是台服）—— 与区域起止时间的展示口径一致，
    否则界面上会出现"上次更新 13:00、区域开始 21:00"这种自相矛盾的两套时间。
    """
    if value <= 0:
        return "<未设置>"
    return datetime.fromtimestamp(value, tz=DISPLAY_TZ).strftime(_TIME_FORMAT)


def _as_int(value: Any, default: int = 0) -> int:
    """宽容地取整数：拿不到就用默认值。

    【为什么需要它】
    ``from_dict`` 直接写 ``int(data.get(...))`` 时，一个手工编辑出来的
    ``"player_id": "不是数字"`` 就会抛 ``ValueError`` —— 而缓存只是加速手段，
    **不该因为一个脏字段就让整个页面报错**。这里统一"读不出就退回默认"。
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    """宽容地取浮点数（理由同 :func:`_as_int`）。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def next_refresh_after(now: datetime | None = None) -> datetime:
    """``now`` 之后**最近的**「周三 20:00」（按 :data:`DISPLAY_TZ` 计算）。

    :param now: 计算基准；``None`` 表示"现在"。测试直接传时间即可，无需冻结时钟。

    【规则（项目里唯一的"刷新时刻"定义）】
    - 周三 19:59 → **今天** 20:00；
    - 周三 20:01 → **下周三** 20:00（今天那个点已经过了）；
    - 其它任何一天 → 本周/下周的周三 20:00。

    .. note::
       星期与时刻取自 :data:`config.ACTIVITY_REFRESH_WEEKDAY` /
       :data:`config.ACTIVITY_REFRESH_HOUR`。游戏改期时**只改配置**，
       不动这里的逻辑。
    """
    moment = now if now is not None else datetime.now(DISPLAY_TZ)
    if moment.tzinfo is None:  # 传进来的朴素时间按展示时区解释
        moment = moment.replace(tzinfo=DISPLAY_TZ)

    days_ahead = (config.ACTIVITY_REFRESH_WEEKDAY - moment.weekday()) % 7
    candidate = (moment + timedelta(days=days_ahead)).replace(
        hour=config.ACTIVITY_REFRESH_HOUR,
        minute=0,
        second=0,
        microsecond=0,
    )
    if candidate <= moment:
        candidate += timedelta(days=7)
    return candidate


@dataclass
class ActivityCache:
    """一份活动区域 / 关卡候选快照。"""

    #: 拉取时的玩家 ID（缓存键的一部分；``0`` = 不知道）
    player_id: int = 0
    #: 拉取时的区服编号（缓存键的一部分；``0`` = 不知道）
    server_id: int = 0
    #: 拉取时刻（epoch 秒）
    fetched_at: float = 0.0
    #: 下一次自动刷新时刻（epoch 秒），由 :func:`next_refresh_after` 算出
    next_refresh_at: float = 0.0
    #: 当期开放区域（``area_view()`` 的扁平结构：area_id / name / progress / start / end …）
    areas: list[dict[str, Any]] = field(default_factory=list)
    #: ``{area_id: [关卡候选…]}``（JSON 的键只能是字符串，所以统一 ``str(area_id)``）
    sections: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    #: ``{area_id: [{section_id, reason}…]}``（被挡关卡，界面要能解释"为什么少了 4 关"）
    blocked: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    #: 结构版本（见 :data:`SCHEMA_VERSION`）
    schema_version: int = SCHEMA_VERSION

    # ------------------------------------------------------------------
    # 展示辅助（界面直接消费，不做二次计算）
    # ------------------------------------------------------------------
    @property
    def is_empty(self) -> bool:
        """是否没有任何可用数据（从没成功拉过）。"""
        return not self.areas

    @property
    def fetched_text(self) -> str:
        """上次拉取时间的展示文本。"""
        return format_timestamp(self.fetched_at)

    @property
    def next_refresh_text(self) -> str:
        """下次自动刷新时间的展示文本。"""
        return format_timestamp(self.next_refresh_at)

    def areas_view(self) -> list[dict[str, Any]]:
        """区域候选（界面下拉用）。"""
        return [dict(item) for item in self.areas]

    def sections_view(self, area_id: int) -> list[dict[str, Any]]:
        """某区已缓存的关卡候选（没缓存过时返回空列表，调用方据此决定要不要去拉）。"""
        return [dict(item) for item in self.sections.get(str(area_id), [])]

    def blocked_view(self, area_id: int) -> list[dict[str, Any]]:
        """某区已缓存的"被挡关卡 + 原因"。"""
        return [dict(item) for item in self.blocked.get(str(area_id), [])]

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def store_areas(
        self,
        areas: list[Mapping[str, Any]],
        *,
        player_id: int,
        server_id: int,
        now: float | None = None,
    ) -> None:
        """记下"当期区域清单"，并**清空关卡缓存**。

        【为什么换区域要连带清空关卡】
        关卡表是「按区拉的」（11003 必须带 areaID）。区域轮换后，旧区的关卡号
        在新一期里要么不存在、要么语义变了 —— 留着只会让界面显示一个
        根本扫不了的关卡。清掉之后，界面会按需重新拉当前区的关卡。
        """
        moment = time.time() if now is None else now
        self.areas = [dict(item) for item in areas]
        self.sections.clear()
        self.blocked.clear()
        self.player_id = int(player_id)
        self.server_id = int(server_id)
        self.fetched_at = moment
        self.next_refresh_at = next_refresh_after(
            datetime.fromtimestamp(moment, tz=DISPLAY_TZ)
        ).timestamp()
        self.schema_version = SCHEMA_VERSION

    def store_sections(
        self,
        area_id: int,
        sweepable: list[Mapping[str, Any]],
        blocked: list[Mapping[str, Any]] | None = None,
    ) -> None:
        """记下某区的关卡候选（与"被挡关卡 + 原因"）。"""
        key = str(int(area_id))
        self.sections[key] = [dict(item) for item in sweepable]
        self.blocked[key] = [dict(item) for item in (blocked or [])]

    # ------------------------------------------------------------------
    # 过期判定
    # ------------------------------------------------------------------
    def needs_refresh(
        self,
        *,
        player_id: int | None = None,
        server_id: int | None = None,
        now: float | None = None,
    ) -> tuple[bool, str]:
        """判断是否该重拉一次，并给出**人话原因**（界面直接显示它）。

        :param player_id: 当前会话的玩家 ID；``None`` / ``0`` 表示"不知道"，
            此时**不**因账号不同而作废（宁可多显示一会儿，也不要因为读不到
            会话就永远拉不到数据）。
        :param server_id: 当前区服编号，规则同上。
        :param now: 判定基准（epoch 秒）；测试直接传值，不必冻结时钟。
        :return: ``(是否需要刷新, 原因)``。不需要刷新时原因不是空串 ——
            而是一句"数据是什么时候的、下次什么时候自动更新"，界面直接拿去显示。
        """
        moment = time.time() if now is None else now

        if self.schema_version != SCHEMA_VERSION:
            return True, (
                f"缓存结构版本 {self.schema_version} 已过期（当前 {SCHEMA_VERSION}）"
            )
        if self.is_empty:
            return True, "还没有区域数据（首次使用）"
        if self.player_id and player_id and self.player_id != int(player_id):
            return True, f"缓存属于另一个角色（playerId={self.player_id}）"
        if self.server_id and server_id and self.server_id != int(server_id):
            return True, f"缓存属于另一个区服（#{self.server_id}）"
        if self.next_refresh_at and moment >= self.next_refresh_at:
            return True, (
                f"已过自动更新时刻 {format_timestamp(self.next_refresh_at)}"
                "（降临关卡每周三更新）"
            )
        max_age = max(config.ACTIVITY_CACHE_MAX_AGE_DAYS, 1) * 86400
        if self.fetched_at and moment - self.fetched_at > max_age:
            return True, f"上次更新已超过 {config.ACTIVITY_CACHE_MAX_AGE_DAYS} 天"
        return False, (
            f"区域数据为 {self.fetched_text}"
            f"（下次自动更新 {self.next_refresh_text}）"
        )

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """转成可直接 ``json.dumps`` 的结构（键名就是文件里的字段名）。"""
        return {
            "schema_version": SCHEMA_VERSION,
            "player_id": self.player_id,
            "server_id": self.server_id,
            "fetched_at": self.fetched_at,
            "next_refresh_at": self.next_refresh_at,
            "areas": self.areas,
            "sections": self.sections,
            "blocked": self.blocked,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ActivityCache:
        """从 JSON 数据还原。

        【为什么每个字段都要"洗"一遍】
        文件可能被人手工编辑过，或被旧版本写成别的形状。原则是
        **读不出来的字段退回默认值**：宁可少一个字段，也不要让整个页面报错。
        """

        def _rows(value: Any) -> list[dict[str, Any]]:
            if not isinstance(value, list):
                return []
            return [dict(item) for item in value if isinstance(item, Mapping)]

        def _grouped(value: Any) -> dict[str, list[dict[str, Any]]]:
            if not isinstance(value, Mapping):
                return {}
            return {str(key): _rows(item) for key, item in value.items()}

        return cls(
            player_id=_as_int(data.get("player_id")),
            server_id=_as_int(data.get("server_id")),
            fetched_at=_as_float(data.get("fetched_at")),
            next_refresh_at=_as_float(data.get("next_refresh_at")),
            areas=_rows(data.get("areas")),
            sections=_grouped(data.get("sections")),
            blocked=_grouped(data.get("blocked")),
            schema_version=_as_int(data.get("schema_version")),
        )

    # ------------------------------------------------------------------
    # 落盘 / 读回
    # ------------------------------------------------------------------
    def save(self, path: Path | None = None) -> Path | None:
        """原子地写入缓存文件；失败只 warning，绝不抛错。

        【为什么用「临时文件 + os.replace」而不是直接 write_text】
        ``web_server.py`` 与 ``main_gui.py`` 可能同时开着（两个进程）。
        直接写时，另一个进程恰好读到"写了一半"的文件就会解析失败 ——
        这类故障偶发且极难复现。``os.replace`` 在同一分区上是原子的：
        读方要么看到旧文件、要么看到完整的新文件。
        """
        target = Path(path) if path is not None else config.ACTIVITY_CACHE_FILE
        temp = target.with_name(target.name + ".tmp")
        try:
            text = json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n"
            temp.write_text(text, encoding="utf-8")
            os.replace(temp, target)
        except OSError as exc:
            _LOGGER.warning(
                "活动区域缓存写入失败（不影响使用，只是下次还得重拉）：%s", exc
            )
            return None
        return target

    @classmethod
    def load(cls, path: Path | None = None) -> ActivityCache:
        """读回缓存；文件不存在 / 损坏 / 版本不符时返回**空缓存**。

        "空缓存"的语义是"从没拉过"：调用方据此触发一次拉取，
        而不是把错误抛给用户 —— 缓存只是加速手段，不是真相来源。
        """
        target = Path(path) if path is not None else config.ACTIVITY_CACHE_FILE
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        except (OSError, json.JSONDecodeError) as exc:
            _LOGGER.warning("活动区域缓存读取失败，将按「没有缓存」处理：%s", exc)
            return cls()
        if not isinstance(data, Mapping):
            _LOGGER.warning("活动区域缓存内容不是 JSON 对象，已忽略")
            return cls()
        if int(data.get("schema_version") or 0) != SCHEMA_VERSION:
            _LOGGER.info("活动区域缓存结构版本不符，已忽略（会自动重拉）")
            return cls()
        return cls.from_dict(data)
