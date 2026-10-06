"""活动区域 / 关卡候选的**缓存服务**：先查缓存 → 过期才联网 → 落盘。

【它解决的问题（2026-09-22 用户需求）】
网页上的「活动区域」「指定关卡」每次都要那份清单，而清单**一周才变一次**。
原实现是"每次打开页面都重新拉"（发 11025 与 11003），既没必要也让风控多看两个包。
本服务把"拉一次、存下来、过期才更新"这件事收在一处：
``webapi/`` 只负责把结果翻译成 JSON，不再自己判断"要不要刷新"。

【为什么执行函数要由外部注入（而不是直接调 runner）】
真正需要串行化的地方在 ``webapi/state.py::RunManager``：它持有一把闸门，
保证"预览 / 刷新"不会与"一键运行"同时发包（扫荡有副作用，绝不并发）。
如果本服务自己 ``runner.prepare()`` 起一次运行，就**绕过**了那把闸门 ——
而这正是 ``notes/web_layer.md`` §二 明令禁止的"入口层另起一套调度"。
所以这里只声明"我需要一个能跑只读请求的东西"（:data:`RunInline`），
由入口层把**带闸门的那一个**传进来。命令行想复用时同理传自己的执行器。

【★ 这里拿到的数据只给界面看】
真正扫荡时 ``tasks/sweep.py`` 仍现场发 11025 / 11003，用最新的 ``sectionState``
判断"是否已通关"。缓存里的关卡状态会过时（你刚通关的关卡在缓存里还是"未通关"），
拿它做判断会**误挡**真实可扫的关卡。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from models.activity_cache import ActivityCache
from services import runner

_LOGGER: Final[logging.Logger] = logging.getLogger("services.activity_cache_service")

#: 执行一次**只读发现**的函数签名（由入口层注入，见模块文档）。
RunInline = Callable[[runner.RunRequest], "runner.RunReport"]

#: 干这件事的任务名（缓存刷新就是"跑一次只读的扫荡任务"）。
TASK_NAME: Final[str] = "sweep_activity"


@dataclass(frozen=True)
class ActivityCacheResult:
    """一次"取候选"的结果（含"这次有没有联网"的如实说明）。"""

    #: 当前有效的缓存（可能是刚拉回来的，也可能是旧的）
    cache: ActivityCache
    #: ``"cache"`` = 直接用本地缓存（**没发包**）；``"network"`` = 这次真去拉了
    source: str
    #: 人话说明：命中缓存时是"数据是什么时候的"；刷新后是"更新了几个区"
    note: str
    #: 非空 = 这次刷新**失败**（此时仍返回旧缓存，界面要显示这个原因）
    error: str = ""
    #: 本次实际发出的消息号（护栏：正常只可能是 ``(11025,)`` 或 ``(11025, 11003)``）
    sent_packets: tuple[int, ...] = ()

    @property
    def ok(self) -> bool:
        """是否可用（要么命中缓存，要么刷新成功）。"""
        return not self.error and not self.cache.is_empty


@dataclass
class ActivityCacheService:
    """负责"查缓存 / 过期刷新 / 落盘"。

    :param run_inline: 带闸门的执行器（``RunManager.run_inline`` 或等价物）。
        签名与 :data:`RunInline` 一致：收一个 :class:`services.runner.RunRequest`，
        返回 :class:`services.runner.RunReport`。
    :param cache_path: 缓存文件路径；``None`` = :data:`config.ACTIVITY_CACHE_FILE`
        （测试传临时路径，避免碰到真实文件）。
    :param session_probe: 读当前会话标识的函数（``player_id`` / ``server_id``）；
        默认 :func:`services.runner.describe_session`，测试可注入固定值。
    """

    run_inline: RunInline
    cache_path: Path | None = None
    session_probe: Callable[[], dict[str, Any]] = runner.describe_session
    #: 最近一次结果（便于调用方与测试查看"刚才发生了什么"）
    last: ActivityCacheResult | None = field(default=None, init=False)

    # ------------------------------------------------------------------
    # 内部：会话标识 / 拉取
    # ------------------------------------------------------------------
    def session_ids(self) -> tuple[int, int]:
        """当前会话的 ``(player_id, server_id)``；读不到就返回 ``(0, 0)``。

        【为什么读不到不算"过期"】
        判据见 :meth:`models.activity_cache.ActivityCache.needs_refresh`：
        只有**两边都知道**且不一致时才作废。否则没登录 / 没解析出 playerId 时
        会把缓存判成"另一个角色"，表现就是"永远在重拉"。
        """
        try:
            info = self.session_probe() or {}
        except Exception as exc:  # noqa: BLE001 - 读会话失败不该拖垮取候选
            _LOGGER.warning("读取会话标识失败，按「不知道」处理：%s", exc)
            return 0, 0
        return int(info.get("player_id") or 0), int(info.get("server_id") or 0)

    def _fetch(
        self, area: str | None, *, prefetch_sections: bool = False
    ) -> tuple[dict[str, Any], tuple[int, ...], str]:
        """跑一次只读发现。

        :param area: ``None`` = 只拉区域清单（11025）；给了就是"区域 + 该区关卡"。
        :param prefetch_sections: ``True`` = 在拉区域清单时一并预拉所有区域关卡（11003）。
        :return: ``(任务 data, 发出的消息号, 错误说明)``；错误说明非空表示这次失败。
        """
        task_params = {
            "area": area,
            # ★ times=0 = 只读：11009（扫荡）连组装都不会发生。
            "times": 0,
            "section": None,
            "allow_unpassed": True,
        }
        if prefetch_sections:
            task_params["prefetch_sections"] = True

        request = runner.RunRequest(
            tasks=(
                runner.TaskRequest(
                    name=TASK_NAME,
                    params=task_params,
                ),
            ),
        )
        # ★ 这里**故意不捕获**异常：``RunBusyError`` / ``InvalidRunError``
        # 由入口层翻译成 HTTP 409 / 400（与 ``/api/player`` 完全一致）——
        # 在这里吞掉它们，只会把"上一次运行还没结束"变成一条含糊的 200 提示。
        report = self.run_inline(request)

        step = report.steps[0] if report.steps else None
        if step is None:
            return {}, (), "刷新失败：没有收到任何结果"
        data: dict[str, Any] = dict(step.data) if step.data else {}
        sent = tuple(int(item) for item in data.get("sent_packets", ()) or ())
        if not step.ok:
            return data, sent, step.message or "刷新失败"
        return data, sent, ""

    # ------------------------------------------------------------------
    # 对外：区域 / 关卡
    # ------------------------------------------------------------------
    def ensure_areas(self, *, force: bool = False) -> ActivityCacheResult:
        """取"当期开放区域"：缓存新鲜就直接给，过期（或 ``force``）才联网拉。

        :param force: ``True`` = 用户点了「更新区域」，**无条件**重拉一次。
        """
        cache = ActivityCache.load(self.cache_path)
        player_id, server_id = self.session_ids()

        if force:
            reason = "用户点了「更新区域」"
        else:
            need, reason = cache.needs_refresh(player_id=player_id, server_id=server_id)
            if not need:
                result = ActivityCacheResult(cache=cache, source="cache", note=reason)
                self.last = result
                return result

        data, sent, error = self._fetch(None, prefetch_sections=True)
        if error:
            # 刷新失败：**保留旧缓存**（宁可显示 9 月 23 日的数据，也不要变空白）。
            result = ActivityCacheResult(
                cache=cache,
                source="cache",
                note=reason,
                error=error,
                sent_packets=sent,
            )
            self.last = result
            return result

        areas = [dict(item) for item in data.get("areas", []) or []]
        if not areas:
            result = ActivityCacheResult(
                cache=cache,
                source="cache",
                note=reason,
                error="服务端没有返回任何当期开放区域",
                sent_packets=sent,
            )
            self.last = result
            return result

        # 拿到新区域 → 记下来（并清空关卡缓存：区域换了，关卡必然换）
        cache.store_areas(areas, player_id=player_id, server_id=server_id)
        # 若本次一次性返回了全部区域关卡，一并填充进缓存
        all_sections = data.get("all_sections")
        if isinstance(all_sections, dict):
            for a_id_str, sec_data in all_sections.items():
                if a_id_str.isdigit() and isinstance(sec_data, dict):
                    cache.store_sections(
                        int(a_id_str),
                        sweepable=sec_data.get("sweepable", []),
                        blocked=sec_data.get("blocked", []),
                    )
        cache.save(self.cache_path)
        result = ActivityCacheResult(
            cache=cache,
            source="network",
            note=f"已更新区域数据：{len(areas)} 个当期开放区域（{cache.fetched_text}）",
            sent_packets=sent,
        )
        self.last = result
        return result

    def ensure_sections(self, area_id: int, *, force: bool = False) -> ActivityCacheResult:
        """取"某区的关卡候选"。

        【为什么先 :meth:`ensure_areas` 再查关卡】
        区域换了之后，上一期的关卡号在新一期里可能根本不存在。
        先确保区域新鲜（刷新会顺带清空关卡缓存），再决定"这个区的关卡在不在缓存里" ——
        顺序反了就会用上一期的数据去答这一期的问题。
        """
        areas_result = self.ensure_areas(force=force)
        if areas_result.error:
            return areas_result  # 区域都没拿到，谈不上关卡

        cache = areas_result.cache
        if cache.sections_view(area_id) and not force:
            result = ActivityCacheResult(
                cache=cache,
                source="cache",
                note=f"{area_id} 的关卡数据来自 {cache.fetched_text} 的缓存",
                sent_packets=areas_result.sent_packets,
            )
            self.last = result
            return result

        data, sent, error = self._fetch(str(int(area_id)))
        if error:
            result = ActivityCacheResult(
                cache=cache,
                source=areas_result.source,
                note=f"未能更新 {area_id} 的关卡数据",
                error=error,
                sent_packets=areas_result.sent_packets + sent,
            )
            self.last = result
            return result

        sections = data.get("sections") or {}
        sweepable = [dict(item) for item in sections.get("sweepable", []) or []]
        blocked = [dict(item) for item in sections.get("blocked", []) or []]
        cache.store_sections(area_id, sweepable, blocked)
        cache.save(self.cache_path)
        result = ActivityCacheResult(
            cache=cache,
            source="network",
            note=(
                f"已更新 {area_id} 的关卡数据：可扫 {len(sweepable)} 关、"
                f"被挡 {len(blocked)} 关"
            ),
            sent_packets=areas_result.sent_packets + sent,
        )
        self.last = result
        return result
