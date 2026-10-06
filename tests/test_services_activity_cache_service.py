"""``services/activity_cache_service.py`` 的守护测试。

【这一层唯一真正重要的性质】
**"缓存新鲜 → 一个包都不发"**。它是用户明确要求的（"不要每次都自动拉取"），
也是最容易在重构里丢掉的性质：只要有人把判断顺序写反（先拉后查），
功能看起来完全正常，只是每次打开页面都多两个只读包。
所以这里的假执行器会**记录每次调用**，核心断言就是"必须是 0 次"。

运行方式：

    python -m pytest tests/test_services_activity_cache_service.py -q
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from models.activity_cache import ActivityCache
from services import runner
from services.activity_cache_service import ActivityCacheService

#: 测试用会话（真实值来自真机：#113 伊莉莎白一世 / role 10000003）
PLAYER_ID = 10000003
SERVER_ID = 113
AREA_ID = 128110053


def session() -> dict[str, Any]:
    """假的会话探测（不读 .session.json，测试与这台机器无关）。"""
    return {"player_id": PLAYER_ID, "server_id": SERVER_ID}


class FakeRunner:
    """假的"带闸门执行器"：记录调用、按参数造出合理的响应。"""

    def __init__(self) -> None:
        #: 每次调用收到的任务参数（断言"该不该发、发了什么"）
        self.calls: list[dict[str, Any]] = []

    def __call__(self, request: runner.RunRequest) -> runner.RunReport:
        params = dict(request.tasks[0].params)
        self.calls.append(params)
        return runner.RunReport(
            steps=[
                runner.StepResult(
                    name="sweep_activity",
                    ok=True,
                    message="只读完成",
                    data=self.payload(params),
                )
            ]
        )

    @staticmethod
    def payload(params: dict[str, Any]) -> dict[str, Any]:
        """按"任务真实会返回什么"造数据（含 sent_packets，方便断言只读）。"""
        if params.get("area"):
            area_id = int(params["area"])
            return {
                "areas": [
                    {"index": 1, "area_id": area_id, "name": "可疑的工作", "progress": "5/5"}
                ],
                "area": {"area_id": area_id, "name": "可疑的工作"},
                "sections": {
                    "sweepable": [
                        {"section_id": 129110539, "energy_cost": 40, "passed": True}
                    ],
                    "blocked": [{"section_id": 129110537, "reason": "不消耗体力"}],
                },
                "sent_packets": (11025, 11003),
            }
        return {
            "areas": [
                {"index": 1, "area_id": AREA_ID, "name": "可疑的工作", "progress": "5/5"}
            ],
            "sent_packets": (11025,),
        }


def seed_cache(path: Path, *, player_id: int = PLAYER_ID) -> ActivityCache:
    """写一份**新鲜**的缓存（刚 store，刷新点在下一个周三 20:00）。"""
    cache = ActivityCache()
    cache.store_areas(
        [{"index": 1, "area_id": AREA_ID, "name": "可疑的工作"}],
        player_id=player_id,
        server_id=SERVER_ID,
    )
    cache.save(path)
    return cache


def make_service(path: Path, fake: FakeRunner) -> ActivityCacheService:
    return ActivityCacheService(fake, cache_path=path, session_probe=session)


# ---------------------------------------------------------------------------
# ① 区域
# ---------------------------------------------------------------------------
class TestEnsureAreas:
    def test_first_run_fetches_and_saves(self, tmp_path: Path) -> None:
        """第一次（没有缓存）必须拉一次，并落盘。"""
        path = tmp_path / ".activity_cache.json"
        fake = FakeRunner()
        result = make_service(path, fake).ensure_areas()

        assert result.source == "network"
        assert len(fake.calls) == 1
        assert fake.calls[0]["times"] == 0  # ★ 只读
        assert fake.calls[0]["area"] is None  # 只要区域清单
        # ★ 2026-10-06：刷新区域时必须**顺带**把各区域的关卡拉回来（一次预热）。
        # 这个键曾因"规格表没登记"被当未知参数丢掉 → 预热静默失效（见
        # tests/test_services_task_spec.py::TestInternalParams）。
        assert fake.calls[0]["prefetch_sections"] is True
        assert result.sent_packets == (11025,)
        assert path.exists()
        assert result.cache.areas_view()[0]["area_id"] == AREA_ID

    def test_prefetch_fills_every_area_in_one_shot(self, tmp_path: Path) -> None:
        """★ 2026-10-06：预热回来的 ``all_sections`` 必须真的落进缓存。

        这条链路曾经断在"参数被当不认识的键丢掉"上：``ensure_areas`` 里读
        ``all_sections`` 的代码一直都在，只是**永远读不到东西** ——
        表现是"用户每选一个新区域就多发两个只读包"，而功能看起来完全正常。

        这里用"预拉生效后，再取该区关卡一个包都不发"作为可核对的判据。
        """
        path = tmp_path / ".activity_cache.json"
        area_id = AREA_ID

        def with_all_sections(request: runner.RunRequest) -> runner.RunReport:
            assert request.tasks[0].params["prefetch_sections"] is True
            return runner.RunReport(
                steps=[
                    runner.StepResult(
                        name="sweep_activity",
                        ok=True,
                        message="只读完成：当期开放 1 个区域（关卡已一并加载）。",
                        data={
                            "areas": [
                                {"index": 1, "area_id": area_id, "name": "可疑的工作"}
                            ],
                            "all_sections": {
                                str(area_id): {
                                    "sweepable": [
                                        {
                                            "section_id": 129110539,
                                            "energy_cost": 40,
                                            "passed": True,
                                        }
                                    ],
                                    "blocked": [
                                        {"section_id": 129110537, "reason": "不消耗体力"}
                                    ],
                                }
                            },
                            "sent_packets": (11025, 11003),
                        },
                    )
                ]
            )

        result = ActivityCacheService(
            with_all_sections, cache_path=path, session_probe=session
        ).ensure_areas(force=True)

        assert result.source == "network"
        assert result.cache.sections_view(area_id)[0]["section_id"] == 129110539

        # 预拉生效 → 之后取该区关卡**一个包都不发**
        fake = FakeRunner()
        sections = make_service(path, fake).ensure_sections(area_id)
        assert sections.source == "cache"
        assert fake.calls == []

    def test_fresh_cache_sends_nothing(self, tmp_path: Path) -> None:
        """★ 核心性质：缓存新鲜时**零请求**。"""
        path = tmp_path / ".activity_cache.json"
        seed_cache(path)
        fake = FakeRunner()
        result = make_service(path, fake).ensure_areas()

        assert fake.calls == []  # 一个包都没发
        assert result.source == "cache"
        assert result.ok is True
        assert "下次自动更新" in result.note  # 界面直接显示这句话

    def test_force_refreshes_even_if_fresh(self, tmp_path: Path) -> None:
        """「更新区域」按钮（``force=True``）必须无视缓存。"""
        path = tmp_path / ".activity_cache.json"
        seed_cache(path)
        fake = FakeRunner()
        result = make_service(path, fake).ensure_areas(force=True)

        assert len(fake.calls) == 1
        assert result.source == "network"

    def test_failure_keeps_the_old_cache(self, tmp_path: Path) -> None:
        """刷新失败（例如没登录）：**保留旧缓存**并如实报错，而不是变空白。"""
        path = tmp_path / ".activity_cache.json"
        seed_cache(path)

        def failing(request: runner.RunRequest) -> runner.RunReport:
            return runner.RunReport(
                steps=[
                    runner.StepResult(
                        name="sweep_activity",
                        ok=False,
                        message="任务 'sweep_activity' 需要游戏会话状态（1004 令牌）",
                    )
                ]
            )

        result = ActivityCacheService(
            failing, cache_path=path, session_probe=session
        ).ensure_areas(force=True)

        assert result.ok is False
        assert "需要游戏会话状态" in result.error
        assert result.source == "cache"
        assert result.cache.areas_view()  # 旧数据还在，界面仍可用

    def test_other_player_triggers_refresh(self, tmp_path: Path) -> None:
        """缓存属于别的角色 → 必须重拉（进度是账号数据）。"""
        path = tmp_path / ".activity_cache.json"
        seed_cache(path, player_id=999)
        fake = FakeRunner()
        result = make_service(path, fake).ensure_areas()

        assert len(fake.calls) == 1
        # 重拉之后缓存已归属当前会话（这才是"换号必须重拉"的可核对结果）
        assert result.cache.player_id == PLAYER_ID


# ---------------------------------------------------------------------------
# ② 关卡
# ---------------------------------------------------------------------------
class TestEnsureSections:
    def test_cached_sections_are_reused(self, tmp_path: Path) -> None:
        path = tmp_path / ".activity_cache.json"
        cache = seed_cache(path)
        cache.store_sections(
            AREA_ID, [{"section_id": 129110539, "energy_cost": 40, "passed": True}], []
        )
        cache.save(path)

        fake = FakeRunner()
        result = make_service(path, fake).ensure_sections(AREA_ID)

        assert fake.calls == []  # 命中缓存 → 零请求
        assert result.source == "cache"
        assert result.cache.sections_view(AREA_ID)[0]["section_id"] == 129110539

    def test_missing_area_sections_are_fetched_once(self, tmp_path: Path) -> None:
        path = tmp_path / ".activity_cache.json"
        seed_cache(path)
        fake = FakeRunner()
        result = make_service(path, fake).ensure_sections(AREA_ID)

        # 只该发一次（带 area），且只读
        assert len(fake.calls) == 1
        assert fake.calls[0]["area"] == str(AREA_ID)
        assert fake.calls[0]["times"] == 0
        assert result.source == "network"
        assert result.cache.sections_view(AREA_ID)[0]["section_id"] == 129110539
        assert result.cache.blocked_view(AREA_ID)[0]["reason"] == "不消耗体力"
        assert 11009 not in result.sent_packets  # 预览绝不扫荡

    def test_refresh_clears_then_reloads_sections(self, tmp_path: Path) -> None:
        """换期后（force）关卡缓存必须先被清掉、再按新区域重新拉。"""
        path = tmp_path / ".activity_cache.json"
        cache = seed_cache(path)
        cache.store_sections(AREA_ID, [{"section_id": 111}], [])
        cache.save(path)

        fake = FakeRunner()
        result = make_service(path, fake).ensure_sections(AREA_ID, force=True)

        # force → 先拉区域（顺带清空关卡），再拉关卡：两次调用
        assert [call["area"] for call in fake.calls] == [None, str(AREA_ID)]
        assert result.cache.sections_view(AREA_ID)[0]["section_id"] == 129110539


if __name__ == "__main__":  # 支持 `python -m tests.test_services_activity_cache_service`
    raise SystemExit(pytest.main([__file__, "-v", "--no-header"]))
