"""降临/活动关卡扫荡任务（``tasks/sweep.py``）的行为与安全边界测试。

运行方式：

    python -m pytest tests/test_sweep_task.py -q
    python -m tests.test_sweep_task

【本文件最重要的四条断言】

1. **默认（``times=0``）绝不发 ``11009``** —— 扫荡会扣真实体力与扫荡券；
2. **被屏蔽的关卡永不进 11009**：不消耗体力的（需求里明确要求）、配置表禁止扫荡的、
   开了反外挂校验的，一律不发 —— 而且判断只走
   ``models.game_config.sweep_block_reason``（唯一权威实现）；
3. **出站消息号 ⊆ {2007, 11025, 11003, 11009}**（外加首通用的 11015/11017）：
   从请求字节里解出 packetID 再核对，而不是"相信代码写对了"；
4. **次数被多道闸门削**：``--times`` → ``config.SWEEP_MAX_TIMES`` →
   **体力**（★ 2026-10-05：优先用响应回传余额，没有时退回只读 ``2007`` 背包快照，
   使**首轮**也能削峰）→ ★ 2026-10-02 新增的**扫荡券张数**；
5. **零券时一个 11009 都不发**（★ 2026-10-02）：扫之前先用只读的 ``2007``
   数一遍背包里的券，0 张就直接收工 —— 而不是"发出去等被拒"。
6. **清空体力模式每轮都按当前体力削**（★ 2026-10-05）：``times`` 是
   ``min(5, 体力 // energyCost)`` **现算**出来的，复刻客户端行为；
   体力不足一次时**本地收尾**，不发注定被拒的包。

响应字节优先用**真实抓包**（``protocol_samples/sweep_samples.py`` 里的 11010），
只有"要造不同场景"时才手工编码 —— 这样"链路能跑通"这一点始终有实测背书。
"""

from __future__ import annotations

import pytest

import config
from client.game_client import GameClient
from models.activity_stage import (
    ALLOWED_PACKET_IDS,
    ENERGY_ITEM_ID,
    SWEEP_TICKET_ITEM_ID,
    SweepSectionPacket,
)
from models.envelope import parse_envelope
from models.game_config import load_special_sections, sweep_block_reason
from models.protobuf_wire import encode_int, encode_message, encode_string
from tasks.base import create_task

# 复用荣耀之巅测试里的离线替身，避免"手工构造响应"的代码出现第二份
from protocol_samples.sweep_samples import (
    SWEEP_RESPONSE_FOUR_HEX,
    SWEEP_RESPONSE_ONCE_HEX,
)
from tests.test_game_client import FakeResponse
from tests.test_tasks_top_pvp import envelope_bytes, make_session, sent_packet_ids

import tasks.sweep as sweep_task  # noqa: F401  导入即注册

#: 三个消息号（写死成常量，任何改动都会让测试红）
AREAS_ID = 11025
SECTIONS_ID = 11003
SWEEP_ID = 11009
#: ★ 2026-10-02：只读背包查询（用来数扫荡券）
INVENTORY_ID = 2007

#: 抓包里的区域与关卡（"可疑的工作" / 第 9 关，体力 40）
AREA_ID = 128110053
SECTION_ID = 129110539

#: 本任务允许出现的出站消息号（安全边界）
ALLOWED_IDS = set(ALLOWED_PACKET_IDS.values())


# ---------------------------------------------------------------------------
# 离线替身与字节构造
# ---------------------------------------------------------------------------
def patch_transport(monkeypatch: pytest.MonkeyPatch, handler) -> list[bytes]:
    """把 ``GameClient.post_raw`` 换成假的，记录每个出站请求体。

    :param handler: ``(消息号, 请求体) -> 响应字节``。**故意不给默认实现**：
        真发一条没被预期的包时应当直接炸掉（``AssertionError``），
        而不是拿一个"看起来正常"的响应糊过去。
    """
    recorded: list[bytes] = []

    def fake_post_raw(self: GameClient, body: bytes) -> FakeResponse:
        recorded.append(body)
        number = parse_envelope(body).packet_id
        return FakeResponse(handler(number, body))

    monkeypatch.setattr(GameClient, "post_raw", fake_post_raw)
    return recorded


def areas_payload(*area_ids: int) -> bytes:
    """``11026`` 的子包：``areaStateList``（字段 1）每个区域一个消息。"""
    payload = b""
    for area_id in area_ids:
        payload += encode_message(
            1, encode_int(1, area_id) + encode_string(12, "5/5")
        )
    return payload


def sections_payload(*sections: int | tuple[int, int]) -> bytes:
    """``11004`` 的子包：``sectionStateList``（字段 1）每个关卡一个消息。

    :param sections: 每个参数可以是 ``sectionID``（默认状态 2 = 已通关），
        或 ``(sectionID, sectionState)`` 元组（用来造"未通关"的场景）。
    """
    payload = b""
    for item in sections:
        section_id, state = item if isinstance(item, tuple) else (item, 2)
        payload += encode_message(
            1, encode_int(1, section_id) + encode_int(2, state)
        )
    return payload


def sweep_requests(recorded: list[bytes]) -> list[SweepSectionPacket]:
    """从记录下来的请求里挑出 ``11009``，并解成模型（断言次数/关卡用）。"""
    packets: list[SweepSectionPacket] = []
    for body in recorded:
        view = parse_envelope(body)
        if view.packet_id == SWEEP_ID:
            packets.append(SweepSectionPacket.decode(view.sub_packet_bytes))
    return packets


def inventory_payload(*items: tuple[int, int]) -> bytes:
    """``2008 GetAllItemRes`` 的子包：``itemList``（字段 1）每件道具一个消息。

    ``ItemClass`` 的字段编号：``itemSid``(1) / ``itemID``(2) / ``itemAmount``(3)。
    """
    payload = b""
    for item_id, amount in items:
        payload += encode_message(
            1,
            encode_int(1, 0) + encode_int(2, item_id) + encode_int(3, amount),
        )
    return payload


def make_handler(
    *,
    areas: tuple[int, ...] = (AREA_ID,),
    sections: tuple[int | tuple[int, int], ...] = (SECTION_ID,),
    sweep_response_hex: str = SWEEP_RESPONSE_ONCE_HEX,
    tickets: int = 30,
    energy: int = 9999,
):
    """构造一个"像真服务端"的响应分发器（不认识的包会 AssertionError）。

    :param tickets: 背包里的扫荡券张数（``2007`` → ``2008`` 的响应）。
        默认给一个宽松值（30），这样"次数闸门"那几条老测试的语义不受影响；
        要测"券不够"就显式传小值。
    :param energy: 背包里的**体力**（★ 2026-10-05：任务现在从同一条 ``2008``
        响应里顺手读体力、用于首轮削峰）。默认给一个很大的值（9999），
        等价于"体力充足、不削" —— 这样老测试的"档位/硬上限"语义不变；
        要测"体力削峰"就显式传小值。
    """

    def handler(number: int, body: bytes) -> bytes:
        if number == AREAS_ID:
            return envelope_bytes(11026, areas_payload(*areas))
        if number == SECTIONS_ID:
            return envelope_bytes(11004, sections_payload(*sections))
        if number == INVENTORY_ID:
            return envelope_bytes(
                2008,
                inventory_payload(
                    (SWEEP_TICKET_ITEM_ID, tickets),
                    (ENERGY_ITEM_ID, energy),
                ),
            )
        if number == SWEEP_ID:
            return envelope_bytes(11010, bytes.fromhex(sweep_response_hex))
        if number == 11015:
            return envelope_bytes(11016, encode_int(1, 0) + encode_int(2, 999))
        if number == 11017:
            return envelope_bytes(11018, encode_int(1, 0))
        raise AssertionError(f"任务发了一个不该发的包：{number}")

    return handler


def make_task(**kwargs: object):
    """构造扫荡任务（默认 ``times=0`` = 只读）。"""
    return create_task("sweep_activity", make_session(), **kwargs)


def no_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """把任务里所有"模拟真人节奏"的等待压成 0，测试不必真等。

    【为什么必须替换 ``sleep_interruptible`` 而不是 ``time.sleep``】
    任务里的延时统一走 ``cancellation.sleep_interruptible()``，它内部用的是
    ``threading.Event.wait()`` + ``time.monotonic()``，**根本不经过 ``time.sleep``**。
    所以对 ``time.sleep`` 打补丁是**完全无效**的 —— 会让用例真的空等：
    每次首通要等 ``time_cost_sec``（15~22 秒），指定多关时累积到分钟级，
    表现为"测试挂起"。（2026-10-05 定位：就是这个原因。）

    这里统一在真正的时间接缝上打补丁，对 5 处调用点（``sweep.py`` 的 701 / 820 /
    1270 / 1329 / 1370）一并生效，无需知道各处等的是哪个时长。
    """
    monkeypatch.setattr(sweep_task.cancellation, "sleep_interruptible", lambda _: None)
    monkeypatch.setattr(config, "sweep_interval", lambda: 0.0)


# ---------------------------------------------------------------------------
# ① 只读模式（默认）：一个 11009 都不许发
# ---------------------------------------------------------------------------
class TestReadOnlyMode:
    def test_default_only_lists_areas(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """不带参数跑：只发一个 ``11025``，把当期区域列出来。"""
        recorded = patch_transport(monkeypatch, make_handler())
        result = make_task().run()

        assert result.ok is True
        assert sent_packet_ids(recorded) == [AREAS_ID]
        assert result.data["mode"] == "read_only"

    def test_default_lists_areas_structurally(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """★ 区域清单必须**结构化**返回（网页的「活动区域」下拉靠它）。

        【这个测试守的是什么】
        ``data["open_areas"]`` 只是数量 —— 网页拿它画不出下拉，用户只能手抄 ID。
        所以这里断言 ``data["areas"]`` 里逐项带着 id / 编号 / 进度，
        并且是**扁平值**（不是 ``describe()`` 那种给日志用的字符串）。
        """
        patch_transport(monkeypatch, make_handler())
        result = make_task().run()

        areas = result.data["areas"]
        assert isinstance(areas, list) and len(areas) == 1
        first = areas[0]
        assert first["area_id"] == AREA_ID
        assert first["index"] == 1  # 1-based，供 --area <编号> 使用
        assert first["progress"] == "5/5"  # 来自 fixtures 里的 11026
        assert isinstance(first["name"], str)  # 配置表里的区域名（查不到就是空串）
        assert result.data["open_areas"] == len(areas)  # 计数键必须保持兼容

    def test_area_read_only_exposes_sections(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """★ 关卡清单同样要结构化返回（网页的「指定关卡」下拉靠它）。

        同时守住护栏：这一步只发 ``11025`` + ``11003``，**一个 11009 都不发** ——
        「预览」绝不等于「扫荡」。
        """
        recorded = patch_transport(monkeypatch, make_handler())
        result = make_task(area=str(AREA_ID)).run()

        assert sent_packet_ids(recorded) == [AREAS_ID, SECTIONS_ID]
        assert not sweep_requests(recorded)
        assert result.data["area"]["area_id"] == AREA_ID
        sweepable = result.data["sections"]["sweepable"]
        assert [item["section_id"] for item in sweepable] == [SECTION_ID]
        assert sweepable[0]["passed"] is True  # fixtures 里该关 sectionState=2
        assert isinstance(sweepable[0]["energy_cost"], int)
        assert result.data["sections"]["blocked"] == []

    def test_area_without_times_still_does_not_sweep(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """给了 ``--area`` 但没给 ``--times``：只多读一次关卡列表，仍然不扫。"""
        recorded = patch_transport(monkeypatch, make_handler())
        result = make_task(area=str(AREA_ID)).run()

        assert sent_packet_ids(recorded) == [AREAS_ID, SECTIONS_ID]
        assert not sweep_requests(recorded)
        assert result.data["sweepable"] == 1

    def test_dry_run_sends_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """演练模式连只读请求都不发（"不联网"的承诺必须是真的）。"""
        recorded = patch_transport(monkeypatch, make_handler())
        result = make_task(area=str(AREA_ID), times=5, dry_run=True).run()

        assert recorded == []
        assert result.data["mode"] == "dry_run"
        assert result.data["planned_times_per_section"] == 5

    def test_packets_stay_inside_whitelist(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """真扫一遍后，从请求字节解出的消息号必须全在白名单里。"""
        recorded = patch_transport(monkeypatch, make_handler())
        make_task(area=str(AREA_ID), times=1).run()

        assert set(sent_packet_ids(recorded)) <= ALLOWED_IDS
        # 2007（数券，只读）必须先于 11009 出现 —— 正是"先读后发"的证据
        assert sent_packet_ids(recorded) == [AREAS_ID, SECTIONS_ID, INVENTORY_ID, SWEEP_ID]


# ---------------------------------------------------------------------------
# ② 屏蔽规则："不消耗体力的一次性关卡"必须在客户端被挡住
# ---------------------------------------------------------------------------
def zero_energy_but_allowed_section_id() -> int:
    """找一条"``energyCost=0`` 但配置表没禁止扫荡"的关卡（实测有 99 条）。

    这两条同时成立才说明**必须由客户端挡** —— 如果服务端/配置表已经挡了，
    这个测试就失去意义，所以这里顺手做一次自检（见下个 ``assert``）。
    """
    for section in load_special_sections():
        if section.energy_cost == 0 and section.sweep_off == -1:
            assert sweep_block_reason(section) is not None
            return section.section_id
    raise AssertionError("配置表里没有 energyCost=0 且 sweepOff=-1 的关卡，测试前提变了")


class TestBlocking:
    def test_zero_energy_section_is_never_swept(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        zero_id = zero_energy_but_allowed_section_id()
        recorded = patch_transport(
            monkeypatch, make_handler(sections=(zero_id,))
        )
        result = make_task(area=str(AREA_ID), times=1).run()

        assert not sweep_requests(recorded), "零体力关卡绝不能被扫"
        assert result.ok is False
        assert result.data["blocked"] == 1
        assert "不消耗体力" in result.message

    def test_blocked_section_is_skipped_but_others_swept(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """同一个区里"被挡的"跳过、"可扫的"照扫。"""
        zero_id = zero_energy_but_allowed_section_id()
        recorded = patch_transport(
            monkeypatch, make_handler(sections=(zero_id, SECTION_ID))
        )
        result = make_task(area=str(AREA_ID), times=1).run()

        assert [packet.sectionID for packet in sweep_requests(recorded)] == [SECTION_ID]
        assert result.ok is True
        assert result.data["sections_done"] == 1


# ---------------------------------------------------------------------------
# ③ 次数：三道闸门（--times → 硬上限 → 响应回传的剩余体力）
# ---------------------------------------------------------------------------
class TestTimesGates:
    def test_request_carries_times_and_ticket_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """请求里的 ``times`` / ``useSweepTicket`` 必须与实测结构一致。

        ``times=5`` 是客户端 ``eSweepType.five``（档位内的值原样发出）；
        档位外的值会被 :func:`models.activity_stage.normalize_sweep_times` 向下归一。
        """
        recorded = patch_transport(monkeypatch, make_handler())
        make_task(area=str(AREA_ID), times=5).run()

        packet = sweep_requests(recorded)[0]
        assert packet.sectionID == SECTION_ID
        assert packet.times == 5
        assert packet.useSweepTicket == 1

    def test_off_choice_times_is_normalized_down(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--times 7``（客户端没有这一档）会被向下归到 5。"""
        recorded = patch_transport(monkeypatch, make_handler())
        make_task(area=str(AREA_ID), times=7).run()

        assert sweep_requests(recorded)[0].times == 5

    def test_hard_cap_trims_user_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``--times 99`` 会被 ``config.SWEEP_MAX_TIMES`` 削掉（防手误）。"""
        recorded = patch_transport(monkeypatch, make_handler())
        make_task(area=str(AREA_ID), times=99).run()

        assert sweep_requests(recorded)[0].times == config.SWEEP_MAX_TIMES

    def test_energy_from_response_trims_next_section(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """第一关响应回传体力 0（就是抓包"4 次"那一发）→ 第二关直接不扫。"""
        recorded = patch_transport(
            monkeypatch,
            make_handler(
                sections=(SECTION_ID, 129110538),
                sweep_response_hex=SWEEP_RESPONSE_FOUR_HEX,  # 体力剩 0
            ),
        )
        result = make_task(area=str(AREA_ID), times=3).run()

        assert len(sweep_requests(recorded)) == 1
        assert result.data["sections_done"] == 1
        assert result.data["energy_left"] == 0
        assert "可扫次数为 0" in result.data["stop_reason"]


# ---------------------------------------------------------------------------
# ③' 扫荡券闸门（★ 2026-10-02 新增）：没券就一个 11009 都不发
# ---------------------------------------------------------------------------
class TestTicketGate:
    def test_zero_ticket_sends_no_sweep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """0 张券 → 直接收工：出站只有 {11025, 11003, 2007}，**一个 11009 都没有**。"""
        recorded = patch_transport(monkeypatch, make_handler(tickets=0))
        result = make_task(area=str(AREA_ID), times=5).run()

        assert sweep_requests(recorded) == []
        assert sent_packet_ids(recorded) == [AREAS_ID, SECTIONS_ID, INVENTORY_ID]
        assert result.ok is True
        assert result.data["mode"] == "no_ticket"
        assert "扫荡券不足" in result.message

    def test_ticket_shortage_degrades_to_official_tier(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """只有 3 张券、用户要 5 次 → 只能按**官方档位**降到 1 次（不是 3 次）。"""
        recorded = patch_transport(monkeypatch, make_handler(tickets=3))
        make_task(area=str(AREA_ID), times=5).run()

        packets = sweep_requests(recorded)
        assert len(packets) == 1
        assert packets[0].times == 1
        # 1 / 5 / 10 之外的数字绝不允许出现在请求里
        assert packets[0].times in (1, 5, 10)

    def test_enough_tickets_keeps_planned_times(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """券够（5 张 ≥ 要扫 5 次）→ 按计划发 5 次，不被券闸门改动。"""
        recorded = patch_transport(monkeypatch, make_handler(tickets=5))
        make_task(area=str(AREA_ID), times=5).run()

        assert sweep_requests(recorded)[0].times == 5

    def test_read_only_mode_never_queries_inventory(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``times=0``（只读）连 2007 都不发：只想看清单的人不该被多查一次背包。"""
        recorded = patch_transport(monkeypatch, make_handler(tickets=0))
        result = make_task(area=str(AREA_ID), times=0).run()

        assert sent_packet_ids(recorded) == [AREAS_ID, SECTIONS_ID]
        assert result.data["mode"] == "read_only"

    def test_dry_run_never_queries_inventory(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """演练模式连 2007 也不发（"不联网、不发包"必须字面成立）。"""
        recorded = patch_transport(monkeypatch, make_handler(tickets=0))
        make_task(area=str(AREA_ID), times=5, dry_run=True).run()

        assert recorded == []


# ---------------------------------------------------------------------------
# ④ 选择器：三种写法都要能用，**模糊匹配到多个时必须报错而不是猜**
# ---------------------------------------------------------------------------
class TestSelectors:
    def test_area_by_name_keyword(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """名称关键字命中配置表区域名（128110053 = "可疑的工作"）。"""
        recorded = patch_transport(monkeypatch, make_handler())
        result = make_task(area="可疑").run()

        assert sent_packet_ids(recorded) == [AREAS_ID, SECTIONS_ID]
        # 2026-10-03 精简原则：结果消息报区域名，不再出现裸 areaID
        assert "可疑的工作" in result.message
        assert str(AREA_ID) not in result.message

    def test_area_by_list_index(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """清单编号（任务打印的第 N 行，从 1 开始）。"""
        recorded = patch_transport(monkeypatch, make_handler())
        result = make_task(area="1").run()

        assert sent_packet_ids(recorded) == [AREAS_ID, SECTIONS_ID]
        # 2026-10-03 精简原则：结果消息报区域名，不再出现裸 areaID
        assert "可疑的工作" in result.message
        assert str(AREA_ID) not in result.message

    def test_ambiguous_keyword_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """两个区域都含"英雄降臨"时不许猜（报错并列出候选）。"""
        recorded = patch_transport(
            monkeypatch, make_handler(areas=(128000051, 128000052))
        )
        result = make_task(area="英雄降臨", times=1).run()

        assert result.ok is False
        assert "无法确定" in result.message
        assert sent_packet_ids(recorded) == [AREAS_ID]

    def test_unknown_area_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded = patch_transport(monkeypatch, make_handler())
        result = make_task(area="不存在的区域xyz", times=1).run()

        assert result.ok is False
        assert sent_packet_ids(recorded) == [AREAS_ID]
        assert not sweep_requests(recorded)

    def test_unknown_section_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``--section`` 选了不存在的关卡 → 报错，且一个扫荡包都不发。"""
        recorded = patch_transport(monkeypatch, make_handler())
        result = make_task(area=str(AREA_ID), times=1, section="999999").run()

        assert result.ok is False
        assert not sweep_requests(recorded)

    def test_section_by_candidate_index(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``--section 2`` 选第 2 个候选关卡。"""
        recorded = patch_transport(
            monkeypatch, make_handler(sections=(SECTION_ID, 129110538))
        )
        make_task(area=str(AREA_ID), times=1, section="2").run()

        assert [packet.sectionID for packet in sweep_requests(recorded)] == [129110538]


# ---------------------------------------------------------------------------
# ⑤ 失败路径：服务端报错立即收工；本地配置表缺行则挡下
# ---------------------------------------------------------------------------
class TestFailurePaths:
    def test_server_error_stops_and_does_not_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``errorCode != 0`` → 立即收工、**不重试**（有副作用的请求绝不重发）。"""

        def handler(number: int, body: bytes) -> bytes:
            if number == AREAS_ID:
                return envelope_bytes(11026, areas_payload(AREA_ID))
            if number == SECTIONS_ID:
                return envelope_bytes(11004, sections_payload(SECTION_ID))
            # ★ 2026-10-02：扫荡前会先查背包数券；这里给足（10 张），
            # 好让本用例继续只盯"11010 报错后不再重试"这一件事。
            # ★ 2026-10-05：同一条 2008 响应里也要给足体力，否则会被
            # "首轮按体力削峰"削成 0 次、根本发不出 11009。
            if number == INVENTORY_ID:
                return envelope_bytes(
                    2008,
                    inventory_payload(
                        (SWEEP_TICKET_ITEM_ID, 10), (ENERGY_ITEM_ID, 9999)
                    ),
                )
            return envelope_bytes(11010, encode_int(1, -1))  # errorCode = -1

        recorded = patch_transport(monkeypatch, handler)
        result = make_task(area=str(AREA_ID), times=2).run()

        assert len(sweep_requests(recorded)) == 1
        assert result.ok is False
        assert "已停止（不重试）" in result.data["stop_reason"]

    def test_missing_config_row_is_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """服务端给出本地配置表里没有的关卡 → 挡下（提示重新解包配置表）。"""
        recorded = patch_transport(
            monkeypatch, make_handler(sections=(999999999,))
        )
        result = make_task(area=str(AREA_ID), times=1).run()

        assert not sweep_requests(recorded)
        assert result.ok is False
        assert result.data["blocked"] == 1


# ---------------------------------------------------------------------------
# ⑥ 通关护栏（2026-09-21 真机实验后加）：默认只扫已通关关卡
#
# 实测（#113 伊莉莎白一世，只读实验）：
#   已通关区 128110053 的 9 关 → sectionState 全是 2；
#   未通关区 128210047 的关卡 → sectionState = 0。
# 服务端**不会**替你挡"未通关"（它只按 sectionID 结算），所以护栏必须由客户端做。
# ---------------------------------------------------------------------------
class TestPassedGuard:
    def test_unpassed_section_is_blocked_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``sectionState=0``（未通关）→ 默认不扫，并说明原因。"""
        recorded = patch_transport(
            monkeypatch, make_handler(sections=((SECTION_ID, 0),))
        )
        result = make_task(area=str(AREA_ID), times=1).run()

        assert not sweep_requests(recorded), "未通关关卡默认绝不能扫"
        assert result.ok is False
        assert result.data["blocked"] == 1
        assert "没有可扫荡的关卡" in result.message

    def test_unpassed_section_is_listed_but_skipped_when_mixed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """同一区里"未通关"的跳过、"已通关"的照扫。"""
        recorded = patch_transport(
            monkeypatch, make_handler(sections=((129110538, 0), SECTION_ID))
        )
        result = make_task(area=str(AREA_ID), times=1).run()

        assert [packet.sectionID for packet in sweep_requests(recorded)] == [SECTION_ID]
        assert result.ok is True
        assert result.data["blocked"] == 1

    def test_allow_first_pass_simple_unpassed_stage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """勾选「允许首次通关简单关卡」（only_passed=False）时，未通关的简单关卡先模拟首通（11015/11017）。"""
        no_wait(monkeypatch)
        recorded = patch_transport(
            monkeypatch, make_handler(sections=((SECTION_ID, 0),))
        )
        result = make_task(area=str(AREA_ID), times=1, only_passed=False).run()

        assert result.ok is True
        assert result.data["first_passed"] == [SECTION_ID]
        assert sent_packet_ids(recorded) == [
            AREAS_ID,
            SECTIONS_ID,
            INVENTORY_ID,
            11015,
            11017,
            SECTIONS_ID,
        ]

    def test_allow_first_pass_then_sweeps_remaining(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """times=5 且勾选允许首通时，第 1 次首通（11015/11017），剩余 4 次走正常扫荡（11009）。"""
        no_wait(monkeypatch)
        recorded = patch_transport(
            monkeypatch, make_handler(sections=((SECTION_ID, 0),))
        )
        result = make_task(area=str(AREA_ID), times=5, only_passed=False).run()

        assert result.ok is True
        assert result.data["first_passed"] == [SECTION_ID]
        assert [packet.sectionID for packet in sweep_requests(recorded)] == [SECTION_ID]
        assert sweep_requests(recorded)[0].times == 4
        assert sent_packet_ids(recorded) == [
            AREAS_ID,
            SECTIONS_ID,
            INVENTORY_ID,
            11015,
            11017,
            SWEEP_ID,
            SECTIONS_ID,
        ]

    def test_anti_cheat_unpassed_stage_is_blocked_even_with_allow_first_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """开启了反外挂校验（cheatCheck=1）的未通关关卡，即便勾选允许首通，也坚决阻拦。"""
        no_wait(monkeypatch)
        recorded = patch_transport(
            monkeypatch, make_handler(sections=((129010215, 0),))
        )
        result = make_task(area=str(AREA_ID), times=1, only_passed=False).run()

        assert result.ok is False
        assert result.data["blocked"] == 1
        assert "没有可扫荡的关卡" in result.message
        assert sent_packet_ids(recorded) == [AREAS_ID, SECTIONS_ID]

    def test_read_only_with_allow_unpassed_returns_all_stamina_candidates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """活动区域一关都没通过时（服务端 11004 只返回第一关且 state=0），
        只读预览（times=0, only_passed=False）能够从配置表补全该区域全部体力关卡，且 needs_first_pass=True。
        """
        recorded = patch_transport(
            monkeypatch, make_handler(sections=((129110531, 0),))
        )
        result = make_task(area=str(AREA_ID), times=0, only_passed=False).run()

        assert result.ok is True
        assert result.data["mode"] == "read_only"
        sweepable = result.data["sections"]["sweepable"]
        # 该区域 128110053 有 9 关
        assert len(sweepable) == 9
        assert all(item["needs_first_pass"] is True for item in sweepable)
        assert [s["section_id"] for s in sweepable] == [
            129110531, 129110532, 129110533, 129110534, 129110535,
            129110536, 129110537, 129110538, 129110539,
        ]

    def test_sequential_auto_progression_across_unlocked_stages(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """勾选允许首通且未指定具体关卡时，首通第 1 关后服务端解锁第 2 关，能自动按顺序攻打后续关卡。"""
        no_wait(monkeypatch)

        unlocked_count = 1

        def dynamic_handler(number: int, body: bytes) -> bytes:
            nonlocal unlocked_count
            if number == AREAS_ID:
                return envelope_bytes(11026, areas_payload(AREA_ID))
            if number == SECTIONS_ID:
                if unlocked_count == 1:
                    return envelope_bytes(11004, sections_payload((129110531, 0)))
                elif unlocked_count == 2:
                    return envelope_bytes(11004, sections_payload(129110531, (129110532, 0)))
                else:
                    return envelope_bytes(11004, sections_payload(129110531, 129110532))
            if number == 11015:
                return envelope_bytes(11016, encode_int(1, 0) + encode_int(2, 999))
            if number == 11017:
                unlocked_count += 1
                return envelope_bytes(11018, encode_int(1, 0))
            # ★ 2026-10-02：扫荡前会先用只读的 2007 数扫荡券（本用例只走首通，
            # 但闸门在循环之前，所以这里也要应答；给足 10 张即可）。
            # ★ 2026-10-05：同一条 2008 里给足体力（首轮按体力削峰要用）。
            if number == INVENTORY_ID:
                return envelope_bytes(
                    2008,
                    inventory_payload(
                        (SWEEP_TICKET_ITEM_ID, 10), (ENERGY_ITEM_ID, 9999)
                    ),
                )
            raise AssertionError(f"意外的发包：{number}")

        recorded = patch_transport(monkeypatch, dynamic_handler)
        result = make_task(area=str(AREA_ID), times=1, only_passed=False).run()

        assert result.ok is True
        assert result.data["first_passed"] == [129110531, 129110532]

    def test_specified_unpassed_stage_auto_clears_preceding_stages(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """指定尚未通关的关卡 129110533 时，自动依序攻打前置关卡 129110531、129110532，最后攻打 129110533。"""
        no_wait(monkeypatch)
        recorded = patch_transport(
            monkeypatch,
            make_handler(sections=((129110531, 0), (129110532, 0), (129110533, 0))),
        )
        result = make_task(area=str(AREA_ID), section="129110533", times=5, only_passed=False).run()

        assert result.ok is True
        assert result.data["first_passed"] == [129110531, 129110532, 129110533]
        sweeps = sweep_requests(recorded)
        assert len(sweeps) == 1
        assert sweeps[0].sectionID == 129110533
        assert sweeps[0].times == 4


# ---------------------------------------------------------------------------
# ⑦ ★ 2026-10-04：「扫荡到清空体力」（``times=-1``）
#    ★ 2026-10-05 修订：**每一轮都按当前体力削峰**（复刻客户端
#    ``times = min(档位, 体力 // energyCost)`` 的现算行为）。
#
# 抓包依据（``notes/sweep_capture.md`` §五）：一次操作 = **一个** ``times=N`` 的包，
# 不是 N 个包。但 N 是**按体力现算**出来的 —— raw/08 点 1 次（体力 200），
# raw/10 点 5 次时体力已 160 → 客户端把 5 削成 **4** 才发。
# 本板块的关卡 ``SECTION_ID`` 每关耗 40 体力，正好拿来做削峰断言。
# ---------------------------------------------------------------------------
class TestUntilEmpty:
    @staticmethod
    def _no_wait(monkeypatch: pytest.MonkeyPatch) -> None:
        """把等待压成 0（转调共享的 :func:`no_wait`，避免两套实现走偏）。"""
        no_wait(monkeypatch)

    def test_requires_section(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """清空体力**必须**指定关卡；未指定 → 直接失败，一个 11009 都不发。"""
        recorded = patch_transport(monkeypatch, make_handler())
        result = make_task(area=str(AREA_ID), times=-1).run()

        assert sweep_requests(recorded) == []
        assert result.ok is False
        assert "--section" in result.message

    def test_each_round_is_trimmed_by_current_energy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """★ 本次改造的核心断言：**每一轮都按当前体力削**，不是固定发 5。

        场景（关卡每关耗 40 体力）：背包体力快照 200 → 首轮发 5；
        之后服务端每轮回传的余额递减，次数应随之被削成 3 → 1；
        余额不足 1 次时**本地直接收尾，不再发注定被拒的包**。

        = 复刻抓包 raw/08（体力 200 发 1）与 raw/10（体力 160 把 5 削成 4）。
        """
        self._no_wait(monkeypatch)
        # 每轮 11010 回传的体力余额（按顺序消费）。
        energies = [120, 40, 0]
        state = {"n": 0}

        def handler(number: int, body: bytes) -> bytes:
            if number == AREAS_ID:
                return envelope_bytes(11026, areas_payload(AREA_ID))
            if number == SECTIONS_ID:
                return envelope_bytes(11004, sections_payload(SECTION_ID))
            if number == INVENTORY_ID:
                # 体力快照 200、券充足。
                return envelope_bytes(
                    2008,
                    inventory_payload(
                        (SWEEP_TICKET_ITEM_ID, 100), (ENERGY_ITEM_ID, 200)
                    ),
                )
            if number == SWEEP_ID:
                index = min(state["n"], len(energies) - 1)
                energy_left = energies[index]
                state["n"] += 1
                # costList（字段 3）是 repeated GetObjClass；每个 GetObjClass 的
                # itemList（字段 1）才是 ItemClass{itemSid(1)/itemID(2)/itemAmount(3)}。
                # 所以体力余额要**嵌两层**：3 → 1 → {2: ENERGY_ID, 3: amount}。
                return envelope_bytes(
                    11010,
                    encode_int(1, 0)  # errorCode = 0（成功）
                    + encode_message(
                        3,
                        encode_message(
                            1,
                            encode_int(2, ENERGY_ITEM_ID)
                            + encode_int(3, energy_left),
                        ),
                    ),
                )
            raise AssertionError(f"意外的发包：{number}")

        recorded = patch_transport(monkeypatch, handler)
        result = make_task(
            area=str(AREA_ID), times=-1, section=str(SECTION_ID)
        ).run()

        packets = sweep_requests(recorded)
        # 首轮 200//40=5；之后余额 120→3、40→1；余额 0 → 本地收尾不发包。
        assert [p.times for p in packets] == [5, 3, 1]
        assert {p.sectionID for p in packets} == {SECTION_ID}
        assert all(p.useSweepTicket == 1 for p in packets)
        assert result.ok is True
        assert result.data["mode"] == "sweep_until_empty"
        assert result.data["rounds"] == 3
        assert result.data["times_total"] == 9
        # 最后一轮（已发出的）是成功的；停下来的原因是**本地**判定体力不足，
        # 而不是服务端拒绝 —— 所以没有"最后一次错误码"。
        assert result.data["last_error_code"] == 0
        assert "体力不足" in result.data["stop_reason"]

    def test_first_round_uses_inventory_snapshot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """★ 首轮削峰用的是**背包快照**（否则第一个关卡永远先发一个注定被拒的 5）。

        背包体力 100、每关耗 40 → 首轮就该发 ``min(5, 100//40=2) = 2``，
        而不是 5。服务端在第 2 轮回 ``-2`` 收尾，好让本用例只盯首轮次数。
        """
        self._no_wait(monkeypatch)
        calls = {"n": 0}

        def handler(number: int, body: bytes) -> bytes:
            if number == AREAS_ID:
                return envelope_bytes(11026, areas_payload(AREA_ID))
            if number == SECTIONS_ID:
                return envelope_bytes(11004, sections_payload(SECTION_ID))
            if number == INVENTORY_ID:
                return envelope_bytes(
                    2008,
                    inventory_payload(
                        (SWEEP_TICKET_ITEM_ID, 100), (ENERGY_ITEM_ID, 100)
                    ),
                )
            if number == SWEEP_ID:
                calls["n"] += 1
                if calls["n"] == 1:
                    return envelope_bytes(
                        11010, bytes.fromhex(SWEEP_RESPONSE_ONCE_HEX)
                    )
                return envelope_bytes(11010, encode_int(1, -2))  # 体力不足
            raise AssertionError(f"意外的发包：{number}")

        recorded = patch_transport(monkeypatch, handler)
        result = make_task(
            area=str(AREA_ID), times=-1, section=str(SECTION_ID)
        ).run()

        packets = sweep_requests(recorded)
        assert packets, "至少要发一轮"
        assert packets[0].times == 2  # 100 // 40 = 2（首轮就用背包快照削了）
        assert result.ok is True

    def test_zero_energy_sends_no_sweep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """背包体力不足一次 → 一个 11009 都不发（本地就收尾）。"""
        self._no_wait(monkeypatch)
        recorded = patch_transport(
            monkeypatch, make_handler(energy=10, tickets=100)
        )
        result = make_task(
            area=str(AREA_ID), times=-1, section=str(SECTION_ID)
        ).run()

        assert sweep_requests(recorded) == []
        assert result.ok is True
        assert "体力不足" in result.data["stop_reason"]

    def test_stops_when_server_refuses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """服务端仍是否决者：回 ``-2``（体力不足）→ 立刻收尾、不重试。

        首轮削峰只保证"发的是算得出够的次数"，但服务端可能因别的约束拒绝
        （如并发、状态变化）；此时仍按服务端原话停止。
        """
        self._no_wait(monkeypatch)

        def handler_one_refusal(number: int, body: bytes) -> bytes:
            if number == SWEEP_ID:
                return envelope_bytes(11010, encode_int(1, -2))
            return make_handler(sweep_response_hex=SWEEP_RESPONSE_ONCE_HEX)(
                number, body
            )

        recorded = patch_transport(monkeypatch, handler_one_refusal)
        result = make_task(
            area=str(AREA_ID), times=-1, section=str(SECTION_ID)
        ).run()

        packets = sweep_requests(recorded)
        assert len(packets) == 1  # 只发一轮即被拒
        assert result.ok is True  # -2 是预期内收尾
        assert result.data["last_error_code"] == -2
        assert "体力不足" in result.data["stop_reason"]

    def test_zero_ticket_sends_no_sweep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """0 张券 → 一个 11009 都不发（与普通模式同一道闸门）。"""
        recorded = patch_transport(monkeypatch, make_handler(tickets=0))
        result = make_task(
            area=str(AREA_ID), times=-1, section=str(SECTION_ID)
        ).run()

        assert sweep_requests(recorded) == []
        assert result.data["mode"] == "no_ticket"
        assert result.ok is True

    def test_safety_cap_stops_runaway(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """服务端永远回成功且体力无限时，安全上限必须兜住（默认 200 → 这里 10）。

        【与改造前的区别】现在是"每轮按体力削"，所以上限轮按剩余额度发：
        10 次的上限下，第 1 轮 5 次、第 2 轮 5 次（正好用满）→ 停在额度而非被拒。
        """
        self._no_wait(monkeypatch)
        monkeypatch.setattr(config, "SWEEP_UNTIL_EMPTY_MAX_TIMES", 10)
        # 体力给得足够大，确保"停"的原因只可能是安全上限。
        recorded = patch_transport(
            monkeypatch, make_handler(tickets=100, energy=99999)
        )
        result = make_task(
            area=str(AREA_ID), times=-1, section=str(SECTION_ID)
        ).run()

        packets = sweep_requests(recorded)
        assert sum(p.times for p in packets) == 10  # 总次数正好顶到上限
        assert result.data["times_total"] == 10
        assert "安全上限" in result.data["stop_reason"]

    def test_dry_run_does_not_send(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """演练模式：不联网、不发包，只描述将要做什么。"""
        recorded = patch_transport(monkeypatch, make_handler())
        result = make_task(
            area=str(AREA_ID), times=-1, section=str(SECTION_ID), dry_run=True
        ).run()

        assert recorded == []
        assert result.data["mode"] == "dry_run_until_empty"
        assert result.data["round_times"] == 5


