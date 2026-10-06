"""工会金币捐献任务（``tasks/alliance_donate.py``）的单元测试。

运行方式：

    python -m pytest tests/test_tasks_alliance_donate.py -q
    python -m tests.test_tasks_alliance_donate

【本文件要钉死的四条规则】
1. **钻石捐献通道不存在**：解码实际发出的 21017 字节，``actionType`` 恒为
   693000001（金币档）；693000002 / 693000003（钻石档）必须一个都不出现 ——
   这是"不打通钻石捐献通道"的字节级保证；
2. **固定捐 5 次、被拒即停**：第 3 次被拒就只发 3 次请求，"已捐 2/5"如实上报；
3. **演练模式零发包**；
4. **"没入会"是 ok=True 的跳过**，不是失败。

消息号写死在本文件（不 import 生产常量）：协议编号被改动会在这里直接暴露。
"""

from __future__ import annotations

import pytest

from client.game_client import GameClient
from client.session import GameSession
from models.alliance import DonatePacket
from models.envelope import parse_envelope
from models.protobuf_wire import encode_int, encode_message
from models.session_state import GameSessionState
from tasks.base import create_task

from tests.test_game_client import FakeResponse

import tasks.alliance_donate  # noqa: F401  导入即注册

#: 消息号写死在测试里（依据见 models/packet_ids.py 的 Alliance 段）
PACKET_ALLIANCE_HOME = 21009
PACKET_ALLIANCE_HOME_RES = 21010
PACKET_DONATE = 21017
PACKET_DONATE_RES = 21018

ALLIANCE_SUCCESS = 1
ERR_NOT_IN_ALLIANCE = 19
ERR_DONATE_COUNT_USED_UP = 10

#: 档位 ID（= AllianceDonateData.donateID；写死以防生成表被改后悄悄漂移）
GOLD_TIER = 693000001
DIAMOND_TIERS = (693000002, 693000003)

GOLD_ITEM_ID = 200000050
PLANNED_TIMES = 5


@pytest.fixture(autouse=True)
def _no_polite_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """把"两次连捐之间的拟人化间隔"压成 0。

    生产代码在捐献循环里用 ``config.polite_interval()``（默认随机 1~2 秒，
    见 ``tasks/alliance_donate.py``）隔开连续请求。这里只把**取值**改成 0，
    不动"用可中断等待"这件事本身 —— 否则 5 次连捐的用例每个都要多等好几秒。
    """
    import config

    monkeypatch.setattr(config, "polite_interval", lambda: 0.0)


def make_session() -> GameSession:
    """构造一个**不会联网**的会话（域名是保留域名）。"""
    session = GameSession("https://example.invalid", timeout=1.0)
    session.token_store.update(uid="u1", token="tok")
    session.set_game_state(GameSessionState(token="tok", player_id=1, server_id=1))
    return session


# ---------------------------------------------------------------------------
# 报文构造（字段编号见 models/proto_fields.py）
# ---------------------------------------------------------------------------
def home_payload(
    *, error_code: int = ALLIANCE_SUCCESS, donate_count: int = 0, in_alliance: bool = True
) -> bytes:
    """21010 载荷：1 errorCode、2 myAlliance（AllianceClass）、3 donateCount。"""
    payload = encode_int(1, error_code)
    if in_alliance:
        # AllianceClass：1 allianceSid、2 name、13 dailyDonateAmount
        payload += encode_message(
            2, encode_int(1, 555) + encode_int(13, donate_count * 10)
        )
    payload += encode_int(3, donate_count)
    return payload


def donate_res_payload(
    *,
    error_code: int = ALLIANCE_SUCCESS,
    donate_count: int,
    gold_cost: int | None = 5000,
) -> bytes:
    """21018 载荷：1 errorCode、3 donateCount、6 costList（GetObjClass → ItemClass）。"""
    payload = encode_int(1, error_code) + encode_int(3, donate_count)
    if gold_cost is not None:
        # GetObjClass: 1 itemList；ItemClass: 2 itemID、3 itemAmount
        payload += encode_message(
            6, encode_message(1, encode_int(2, GOLD_ITEM_ID) + encode_int(3, gold_cost))
        )
    return payload


def envelope_bytes(packet_id: int, payload: bytes) -> bytes:
    """拼响应字节：字段 1 = 消息号，业务子包槽位编号 = 消息号本身。"""
    return encode_int(1, packet_id) + encode_message(packet_id, payload)


def patch_transport(
    monkeypatch: pytest.MonkeyPatch,
    *,
    home: bytes | None = None,
    donate_results: list[bytes] | None = None,
) -> list[bytes]:
    """把 ``GameClient.post_raw`` 换成"记录请求 + 按消息号回放响应"的替身。

    ``donate_results``：21017 每次请求按序回放的响应；用尽后回放最后一个。
    """
    recorded: list[bytes] = []
    resolved_home = home if home is not None else home_payload()
    results = list(donate_results) if donate_results is not None else []
    donate_calls = 0

    def fake_post_raw(self: GameClient, body: bytes) -> FakeResponse:
        nonlocal donate_calls
        recorded.append(body)
        packet_id = parse_envelope(body).packet_id
        if packet_id == PACKET_ALLIANCE_HOME:
            return FakeResponse(envelope_bytes(PACKET_ALLIANCE_HOME_RES, resolved_home))
        if packet_id == PACKET_DONATE:
            payload = results[min(donate_calls, len(results) - 1)] if results else (
                donate_res_payload(donate_count=donate_calls + 1)
            )
            donate_calls += 1
            return FakeResponse(envelope_bytes(PACKET_DONATE_RES, payload))
        raise AssertionError(f"测试替身收到了意料之外的请求：{packet_id}")

    monkeypatch.setattr(GameClient, "post_raw", fake_post_raw)
    return recorded


def sent_packet_ids(recorded: list[bytes]) -> list[int]:
    """从记录的请求体里解出全部消息号（按发送顺序）。"""
    return [parse_envelope(body).packet_id for body in recorded]


def sent_donate_action_types(recorded: list[bytes]) -> list[int]:
    """解码所有 21017 的 ``actionType``（复用生产模型，顺带核对字段编号）。"""
    types: list[int] = []
    for body in recorded:
        view = parse_envelope(body)
        if view.packet_id == PACKET_DONATE:
            types.append(DonatePacket.decode(view.sub_packet_bytes).actionType)
    return types


class TestDonatesFiveTimesGoldOnly:
    def test_five_donations_all_gold_tier(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """正常路径：固定 5 次、每次都是金币档，结果 ok=True 且按响应对账。"""
        results = [donate_res_payload(donate_count=n + 1) for n in range(5)]
        recorded = patch_transport(monkeypatch, donate_results=results)
        task = create_task("alliance_donate", make_session())
        result = task.run()

        assert sent_packet_ids(recorded)[0] == PACKET_ALLIANCE_HOME
        assert sent_packet_ids(recorded).count(PACKET_DONATE) == 5
        types = sent_donate_action_types(recorded)
        assert len(types) == 5
        assert all(t == GOLD_TIER for t in types), f"发出的档位不是金币档：{types}"
        # 钻石档一个都不许出现（字节级保证）
        assert all(t not in DIAMOND_TIERS for t in types)
        assert result.ok is True
        assert "5/5" in result.message
        assert result.data["gold_spent"] == 5 * 5000

    def test_stops_on_rejection_and_reports_honestly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """第 3 次被拒（次数用完）→ 只发 3 次请求，"已捐 2/5"如实上报。"""
        results = [
            donate_res_payload(donate_count=1),
            donate_res_payload(donate_count=2),
            donate_res_payload(error_code=ERR_DONATE_COUNT_USED_UP, donate_count=2),
        ]
        recorded = patch_transport(monkeypatch, donate_results=results)
        task = create_task("alliance_donate", make_session())
        result = task.run()

        assert sent_packet_ids(recorded).count(PACKET_DONATE) == 3, "被拒后不应继续发捐献请求"
        assert all(t == GOLD_TIER for t in sent_donate_action_types(recorded))
        assert result.ok is False
        assert "2/5" in result.message
        assert "NonAchieve_EnoughDonateCount" in result.message  # 错误码带可读名

    def test_gold_shortage_stops_immediately(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """第一次就被拒（金币不足）→ 一次都没捐出去，ok=False。"""
        recorded = patch_transport(
            monkeypatch,
            donate_results=[donate_res_payload(error_code=7, donate_count=0, gold_cost=None)],
        )
        task = create_task("alliance_donate", make_session())
        result = task.run()

        assert sent_packet_ids(recorded).count(PACKET_DONATE) == 1
        assert result.ok is False
        assert "NonAchieve_CoinRequest" in result.message

    def test_not_in_alliance_is_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """21009 回 errorCode=19（未入会）→ ok=True，不发捐献包。"""
        recorded = patch_transport(
            monkeypatch,
            home=home_payload(error_code=ERR_NOT_IN_ALLIANCE, in_alliance=False),
        )
        task = create_task("alliance_donate", make_session())
        result = task.run()

        assert sent_packet_ids(recorded) == [PACKET_ALLIANCE_HOME]
        assert result.ok is True
        assert "未加入工会" in result.message


class TestDryRun:
    def test_dry_run_sends_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded: list[bytes] = []

        def fake_post_raw(self: GameClient, body: bytes) -> FakeResponse:
            recorded.append(body)
            raise AssertionError("演练模式不该发出任何请求")

        monkeypatch.setattr(GameClient, "post_raw", fake_post_raw)
        task = create_task("alliance_donate", make_session(), dry_run=True)
        result = task.run()

        assert recorded == []
        assert result.ok is True
        assert "演练" in result.message
        assert "25000" in result.message  # 5 × 5000 金币的意图说明


if __name__ == "__main__":  # 支持 `python -m tests.test_tasks_alliance_donate`
    raise SystemExit(pytest.main([__file__, "-v", "--no-header"]))
