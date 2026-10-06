"""素材关卡与元素试炼单元测试。"""

from __future__ import annotations

from typing import Any
import pytest

from models.envelope import EnvelopeView
from models.game_packet import ProtoMessage
from models.wallet import GetAllItemPacket, GetAllItemRes
from models.daily import ItemClass
from models.material_stage import (
    ALLOWED_PACKET_IDS,
    FORBIDDEN_PACKET_IDS,
    AreaStateClass,
    FightingRequestPacket,
    FightingResponseRes,
    FightingResultPacket,
    FightingResultRes,
    GetAreaByDifficultyPacket,
    GetAreaByDifficultyRes,
    SWEEP_TICKET_ITEM_ID,
    SweepSectionPacket,
    SweepSectionRes,
    get_material_areas,
    resolve_material_area,
    resolve_material_section,
)
from tasks.material_stage import SweepMaterialTask

import config


from types import SimpleNamespace


def inventory_res(tickets: int) -> GetAllItemRes:
    """构造一个"背包里有 N 张扫荡券"的 ``2008`` 响应。

    （★ 2026-10-02：扫荡券闸门的测试都靠它 —— 券数决定"扫几次 / 扫不扫"。）
    """
    return GetAllItemRes(
        itemList=[ItemClass(itemID=SWEEP_TICKET_ITEM_ID, itemAmount=tickets)]
    )


def area_list_res(*area_ids: int) -> GetAreaByDifficultyRes:
    """构造一个"服务端素材区清单"的 ``11002`` 响应（10.3 抓包同构）。"""
    return GetAreaByDifficultyRes(
        areaStateList=[AreaStateClass(areaID=a, areaStars=18) for a in area_ids]
    )


class FakeGameClient:
    def __init__(self, responses: dict[type[ProtoMessage], Any]) -> None:
        self.responses = responses
        self.sent: list[ProtoMessage] = []
        #: 每次 ``send`` 的关键字参数（★ 2026-10-04 新增）。本任务必须**按包**决定
        #: ``include_defaults``，而它只存在于"发送参数"里 —— 只断言 ``packet`` 的
        #: 内容/字段值会让"漏传参数"这类缺陷照样绿灯（素材扫荡事故就是这么漏的）。
        self.send_kwargs: list[dict[str, Any]] = []

    def __enter__(self) -> FakeGameClient:
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def send(self, packet: ProtoMessage, **kwargs: Any) -> Any:
        self.sent.append(packet)
        self.send_kwargs.append(dict(kwargs))
        resp = self.responses.get(type(packet))
        if resp is None:
            raise KeyError(f"Unexpected packet: {type(packet)}")
        if isinstance(resp, list):
            # 同型包多次回放（例如 11009 先回 -1 未通关、首通后重扫回 0）
            resp = resp.pop(0)
        return SimpleNamespace(decode_sub_packet=lambda cls: resp)


def test_material_areas_tree_structure() -> None:
    """验证 13 个区域与 64 个关卡静态子目录完整性。"""
    job_areas = get_material_areas("job")
    element_areas = get_material_areas("element")
    all_areas = get_material_areas("all")

    assert len(job_areas) == 7
    assert len(element_areas) == 6
    assert len(all_areas) == 13

    total_sections = sum(len(a.sections) for a in all_areas)
    assert total_sections == 64  # 7*4 + 6*6 = 28 + 36 = 64

    # 验证全部 cheatCheck=0, energyCost=0
    for a in all_areas:
        for s in a.sections:
            assert s.cheat_check == 0
            assert s.energy_cost == 0


def test_resolve_material_area() -> None:
    """测试通过关键字、编号解析素材区域。"""
    a1 = resolve_material_area("101", "job")
    assert a1 is not None and a1.display_id == 101

    a2 = resolve_material_area("火", "element")
    assert a2 is not None and a2.display_id == 201
    assert a2.area_id == 128010001 and a2.name == "火之元素試煉塔"

    a3 = resolve_material_area("non_existent")
    assert a3 is None


def test_resolve_material_section() -> None:
    """测试关卡解析与最高难度获取。"""
    area = resolve_material_area("101")
    assert area is not None
    highest = area.get_highest_section()
    assert highest.difficulty == 4
    # ★ 2026-10-04：目录改为配置表驱动，sectionID 是真实协议 ID
    # （鎮守挑戰4 = 129000123，与 SpecialSectionData 一致）
    assert highest.section_id == 129000123

    found = resolve_material_section("129000120", area)
    assert found is not None
    _, sec = found
    assert sec.section_id == 129000120
    # display_id（区域内序号）仍可用于选择
    found2 = resolve_material_section(2, area)
    assert found2 is not None and found2[1].section_id == 129000121


def test_packet_guard_whitelist() -> None:
    """出站包白名单必须包含 11003, 11009, 11015, 11017，禁发包含 11027, 24005。"""
    assert "GetAreaByDifficultyPacket" in ALLOWED_PACKET_IDS
    assert "SweepSectionPacket" in ALLOWED_PACKET_IDS
    assert "FightingRequestPacket" in ALLOWED_PACKET_IDS
    assert "FightingResultPacket" in ALLOWED_PACKET_IDS
    assert "BuyActivityStagePacket" in FORBIDDEN_PACKET_IDS
    assert "BuyEnergyPacket" in FORBIDDEN_PACKET_IDS


def test_sweep_material_list_catalog() -> None:
    """未指定区域时只读打印目录，零发包。"""
    fake_client = FakeGameClient({})
    task = SweepMaterialTask(category="job", area=None)
    task.open_game_client = lambda: fake_client  # type: ignore[assignment]

    result = task.execute()
    assert result.ok is True
    assert "7 个区域" in result.message
    assert len(fake_client.sent) == 0


def test_sweep_material_read_only_times_zero() -> None:
    """times=0 时仅查询 11003，不发 11009 扫荡。"""
    info_res = area_list_res(128000020)  # 目标区域 10104 属于区域 128000010
    fake_client = FakeGameClient({GetAreaByDifficultyPacket: info_res})

    task = SweepMaterialTask(category="job", area="101", times=0)
    task.open_game_client = lambda: fake_client  # type: ignore[assignment]

    result = task.execute()
    assert result.ok is True
    assert result.data["section_id"] == 129000123
    assert len(fake_client.sent) == 1
    assert isinstance(fake_client.sent[0], GetAreaByDifficultyPacket)


def test_sweep_material_sweep_success() -> None:
    """已通关关卡正常扫荡（11003 查状态 → 11009 扫荡，★ 不查券）。

    ★ 2026-10-03 修订（依据 10.3 素材抓包）：素材扫荡 useSweepTicket=0、
    不消耗券，真实客户端不查券直接扫 —— 所以这里**没有** 2007 查券这一步。
    """
    info_res = area_list_res(128000020)  # 目标区域 10104 属于区域 128000010
    sweep_res = SweepSectionRes(errorCode=0)
    fake_client = FakeGameClient({
        GetAreaByDifficultyPacket: info_res,
        SweepSectionPacket: sweep_res,
    })

    task = SweepMaterialTask(category="job", area="101", times=5)
    task.open_game_client = lambda: fake_client  # type: ignore[assignment]

    result = task.execute()
    assert result.ok is True
    assert result.data["times"] == 5
    assert len(fake_client.sent) == 2
    assert isinstance(fake_client.sent[0], GetAreaByDifficultyPacket)
    assert isinstance(fake_client.sent[1], SweepSectionPacket)
    assert fake_client.sent[1].times == 5
    # ★ 2026-10-03：素材扫荡 useSweepTicket=0（10.3 素材抓包实测），不是活动关的 1
    assert fake_client.sent[1].useSweepTicket == 0
    assert fake_client.sent[1].useSweepTicket == config.MATERIAL_SWEEP_USE_TICKET_VALUE
    # 不涉及券：结果里没有券字段
    assert "ticket_left" not in result.data
    assert "扫荡券" not in result.message


def test_sweep_material_zero_ticket_still_sweeps() -> None:
    """★ 2026-10-03：背包 0 张扫荡券**不再阻止**素材扫荡。

    素材扫荡不消耗券（10.3 抓包 costList 恒空），"0 券拒扫"的闸门已按抓包移除；
    2007 查券这一步也不会发生。
    """
    info_res = area_list_res(128000020)  # 目标区域 10104 属于区域 128000010
    # 背包里只有金币，没有扫荡券
    fake_client = FakeGameClient({
        GetAreaByDifficultyPacket: info_res,
        SweepSectionPacket: SweepSectionRes(errorCode=0),
    })

    task = SweepMaterialTask(category="job", area="101", times=5)
    task.open_game_client = lambda: fake_client  # type: ignore[assignment]

    result = task.execute()
    assert result.ok is True
    # 2007 查券从未发生，11009 照发
    assert not any(isinstance(p, GetAllItemPacket) for p in fake_client.sent)
    assert any(isinstance(p, SweepSectionPacket) for p in fake_client.sent)


def test_sweep_material_ticket_count_does_not_cap_times() -> None:
    """★ 2026-10-03：券少不再削峰 —— 素材扫荡与券无关，times 保持档位值。"""
    info_res = area_list_res(128000020)  # 目标区域 10104 属于区域 128000010
    fake_client = FakeGameClient({
        GetAreaByDifficultyPacket: info_res,
        SweepSectionPacket: SweepSectionRes(errorCode=0),
    })

    task = SweepMaterialTask(category="job", area="101", times=5)
    task.open_game_client = lambda: fake_client  # type: ignore[assignment]

    result = task.execute()
    sweep = [p for p in fake_client.sent if isinstance(p, SweepSectionPacket)]
    assert len(sweep) == 1
    # 券为 0 也不会把 5 次削成 1 次
    assert sweep[0].times == 5
    assert result.ok is True


def test_sweep_material_off_choice_times_is_normalized() -> None:
    """★ 2026-10-02：非官方档位（例如 7）会被向下归到 5 才发出。"""
    info_res = area_list_res(128000020)  # 目标区域 10104 属于区域 128000010
    fake_client = FakeGameClient({
        GetAreaByDifficultyPacket: info_res,
        GetAllItemPacket: inventory_res(10),
        SweepSectionPacket: SweepSectionRes(errorCode=0),
    })

    task = SweepMaterialTask(category="job", area="101", times=7)
    task.open_game_client = lambda: fake_client  # type: ignore[assignment]

    task.execute()
    sweep = [p for p in fake_client.sent if isinstance(p, SweepSectionPacket)]
    assert [p.times for p in sweep] == [5]


def test_sweep_material_unpassed_without_allow_reports_ok(
    # noqa: D103 —— 简单用例
) -> None:
    """扫荡回 -1（未通关）且未开 --allow-unpassed → ok=True 如实说明，不发包首通。"""
    fake_client = FakeGameClient({
        GetAreaByDifficultyPacket: area_list_res(128000020),
        SweepSectionPacket: SweepSectionRes(errorCode=-1),
    })
    task = SweepMaterialTask(category="job", area="101", times=1)
    task.open_game_client = lambda: fake_client  # type: ignore[assignment]

    result = task.execute()
    assert result.ok is True
    assert "尚未首通" in result.message
    assert not any(isinstance(p, FightingRequestPacket) for p in fake_client.sent)


def test_sweep_material_area_not_in_server_list_fails() -> None:
    """★ 2026-10-04：目标区域不在 11002 服务端清单里 → 如实失败（区域会轮换）。"""
    fake_client = FakeGameClient({
        GetAreaByDifficultyPacket: area_list_res(129000001, 129000002),
    })
    task = SweepMaterialTask(category="job", area="101", times=1)
    task.open_game_client = lambda: fake_client  # type: ignore[assignment]

    result = task.execute()
    assert result.ok is False
    assert "不在服务端当前的素材区清单" in result.message
    assert not any(isinstance(p, SweepSectionPacket) for p in fake_client.sent)


def test_sweep_material_first_pass() -> None:
    """★ 2026-10-04 新流程：扫荡回 errorCode=-1（未通关）→ 首通 → 重扫成功。

    10.3 抓包里真实客户端素材流程是 11001 → 11009 直接扫，不发 11003 ——
    "是否首通"由服务端的扫荡错误码告诉我们，不再前置预判。
    """
    area_res = area_list_res(128000020)
    token_res = FightingResponseRes(errorCode=0, fightToken=9999)
    fight_res = FightingResultRes(errorCode=0)

    fake_client = FakeGameClient({
        GetAreaByDifficultyPacket: area_res,
        FightingRequestPacket: token_res,
        FightingResultPacket: fight_res,
        SweepSectionPacket: [SweepSectionRes(errorCode=-1), SweepSectionRes(errorCode=0)],
    })

    task = SweepMaterialTask(
        category="job",
        area="101",
        times=1,
        allow_unpassed=True,
    )
    task.open_game_client = lambda: fake_client  # type: ignore[assignment]

    result = task.execute()
    assert result.ok is True
    # ★ 2026-10-04 新流程：11001 → 11009(-1 未通关) → 11015 → 11017 → 11009 重扫
    assert len(fake_client.sent) == 5
    assert isinstance(fake_client.sent[0], GetAreaByDifficultyPacket)
    assert isinstance(fake_client.sent[1], SweepSectionPacket)
    assert isinstance(fake_client.sent[2], FightingRequestPacket)
    assert isinstance(fake_client.sent[3], FightingResultPacket)
    assert isinstance(fake_client.sent[4], SweepSectionPacket)


# ---------------------------------------------------------------------------
# ★ 2026-10-04 回归钉子：``include_defaults`` 必须"按包"传对（钉在**发送参数**上）
# ---------------------------------------------------------------------------
def test_sweep_material_include_defaults_per_packet() -> None:
    """11009 必须带默认值发、11001 必须省略默认值 —— 钉在**发送参数**上。

    【为什么必须有这条钉子】
    2026-10-04 真机事故：前置步骤全过，唯独 11009 被服务端回 22 B
    ``{"response_code":"OK"}``。根因是 ``_guarded_send`` 没把
    ``include_defaults=True`` 传下去 → 实际只发 7 B（少了 ``useSweepTicket=0``），
    而真实客户端发的是 9 B（见夹具 ``MSWEEP08_REQUEST_HEX``）。

    旧测试（``tests/test_capture_1003_regression.py``）只断言
    ``SweepSectionPacket.encode()``（默认 ``include_defaults=True``）等于抓包字节 ——
    钉的是**模型默认值**，不是**上线参数**，所以拦不住这个缺陷。
    """
    fake_client = FakeGameClient({
        GetAreaByDifficultyPacket: area_list_res(128000020),
        SweepSectionPacket: SweepSectionRes(errorCode=0),
    })
    task = SweepMaterialTask(category="job", area="101", times=5)
    task.open_game_client = lambda: fake_client  # type: ignore[assignment]

    assert task.execute().ok is True
    assert len(fake_client.send_kwargs) == 2
    # ① 11001：抓包 raw/02 子包 11 B（regionID=0 被客户端省略）→ 必须 False
    assert fake_client.send_kwargs[0]["include_defaults"] is False
    # ② 11009：抓包 raw/08 子包 9 B（useSweepTicket=0 照发）→ 必须 True
    assert fake_client.send_kwargs[1]["include_defaults"] is True


def test_sweep_material_request_bytes_match_capture() -> None:
    """11009 子包字节 = 抓包 raw/08（9 B）；7 B 版本就是事故现场。

    夹具 ``MSWEEP08_REQUEST_HEX`` 来自 ``captures/素材关卡扫荡.saz`` 的 ``raw/08``
    请求子包（由 ``tools/export_saz_samples.py`` 导出，请勿手工编辑）。
    """
    from protocol_samples.material_sweep_samples import MSWEEP08_REQUEST_HEX

    captured = bytes.fromhex(MSWEEP08_REQUEST_HEX)
    packet = SweepSectionPacket(sectionID=129010056, times=5, useSweepTicket=0)

    sent_mode = packet.encode(include_defaults=True)
    assert sent_mode == captured, "上线口径（include_defaults=True）必须与抓包逐字节一致"
    assert len(sent_mode) == 9
    assert sent_mode.endswith(b"\x18\x00"), "useSweepTicket=0 必须发出来"

    # ★ 反面样本：事故当时真正上网的 7 B（少了 18 00）—— 被服务端回 22 B ack
    omitted = packet.encode(include_defaults=False)
    assert omitted == captured[:7]
    assert b"\x18\x00" not in omitted

    # ★ 端到端：真实 ``GameClient.build`` 组出的**整包信封**里，嵌的子包字节
    # 必须仍然就是这 9 B（把"参数 → 上线字节"这条链一次性钉死，不依赖人工核对）。
    from client.game_client import GameClient
    from models.envelope import parse_envelope

    body = GameClient().build(packet, include_defaults=True).body
    assert parse_envelope(body).sub_packet_bytes == captured


# ---------------------------------------------------------------------------
# ★ 2026-10-04：「扫荡到次数耗尽」（``times=-1``）
#
# 素材关**不消耗体力**（消耗每日次数），所以哨兵 ``-1`` 的含义是"扫到次数耗尽"：
# 反复发 ``times=5`` 的 11009，直到服务端回 ``TimesIsFull = -7``。
# 与活动关清空模式同一执行方式（每轮一个包、随机 1~2s 间隔），差别只在停止信号。
# ---------------------------------------------------------------------------
def test_sweep_material_until_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    """反复发 ``times=5``，服务端回 ``-7``（次数已满）后正常收尾。"""
    monkeypatch.setattr(config, "sweep_interval", lambda: 0.0)
    fake_client = FakeGameClient({
        GetAreaByDifficultyPacket: area_list_res(128000020),
        # 前两轮成功，第三轮回 -7（次数已满）→ 正常收尾
        SweepSectionPacket: [
            SweepSectionRes(errorCode=0),
            SweepSectionRes(errorCode=0),
            SweepSectionRes(errorCode=-7),
        ],
    })
    task = SweepMaterialTask(category="job", area="101", times=-1)
    task.open_game_client = lambda: fake_client  # type: ignore[assignment]

    result = task.execute()
    sweeps = [p for p in fake_client.sent if isinstance(p, SweepSectionPacket)]
    # 每轮都是**一个** times=5 的包；素材关 useSweepTicket 恒为 0
    assert [p.times for p in sweeps] == [5, 5, 5]
    assert all(p.useSweepTicket == 0 for p in sweeps)
    assert result.ok is True  # -7 是预期内收尾
    assert result.data["mode"] == "sweep_until_exhausted"
    assert result.data["rounds"] == 3
    assert result.data["times_total"] == 10
    assert result.data["last_error_code"] == -7
    assert "次数已耗尽" in result.message


def test_sweep_material_until_exhausted_safety_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """服务端永远回成功时，安全上限必须兜住（默认 200 → 这里改成 10）。"""
    monkeypatch.setattr(config, "sweep_interval", lambda: 0.0)
    monkeypatch.setattr(config, "SWEEP_UNTIL_EMPTY_MAX_TIMES", 10)
    fake_client = FakeGameClient({
        GetAreaByDifficultyPacket: area_list_res(128000020),
        SweepSectionPacket: SweepSectionRes(errorCode=0),
    })
    task = SweepMaterialTask(category="job", area="101", times=-1)
    task.open_game_client = lambda: fake_client  # type: ignore[assignment]

    result = task.execute()
    sweeps = [p for p in fake_client.sent if isinstance(p, SweepSectionPacket)]
    assert len(sweeps) == 2  # 10 // 5
    assert result.data["times_total"] == 10
    assert "安全上限" in result.data["stop_reason"]
