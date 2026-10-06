"""钻石购买体力模型与任务的测试。

测试覆盖：
1. 模型序列化与反序列化（VIPDetailPacket 24011, VIPDetailRes 24012, BuyEnergyPacket 24005, BuyEnergyRes 24006）；
2. 阶梯费用计算（StepTollSystemData 表规则验证）；
3. 任务只读分支（times=0 仅查询不花钱）；
4. 任务花钱分支的 fail-closed 防护（未授权闸门拦截）；
5. 任务演练分支（dry_run 模式组装但不发 24005）；
6. 任务真实发包分支与上限控制（max_cost 熔断）。
"""

from __future__ import annotations

import pytest

from client.exceptions import SpendBlockedError
from client.game_client import GameClient
from client.session import GameSession
from client.spending import SpendPolicy, SpendPurpose
from models.buy_energy import (
    ALLOWED_PACKET_IDS,
    BuyEnergyPacket,
    BuyEnergyRes,
    VIPDetailPacket,
    VIPDetailRes,
    calculate_energy_cost,
)
from models.const_ids import CONSTS
from models.daily import GetObjClass, ItemClass
from models.envelope import GameEnvelope, parse_envelope
from models.session_state import GameSessionState
from tasks.buy_energy import BuyEnergyTask
from tests.test_game_client import FakeResponse


class TestBuyEnergyModels:
    def test_calculate_energy_cost(self) -> None:
        """阶梯费用计算：1~5次每次100，6~10次每次200，11次及以上每次300。"""
        assert calculate_energy_cost(0, 1) == 100
        assert calculate_energy_cost(0, 5) == 500
        assert calculate_energy_cost(0, 6) == 700
        assert calculate_energy_cost(5, 2) == 400
        assert calculate_energy_cost(10, 1) == 300

    def test_vip_detail_packets(self) -> None:
        """24011 为空包，24012 包含已购计数。"""
        req = VIPDetailPacket()
        env = GameEnvelope(token="test_tok", player_id=1001)
        raw = env.encode_with(req)
        assert len(raw) > 0

        res = VIPDetailRes.model_validate(
            {"buyEnergyCount": 3, "pointGoldCount": 1, "buySkillCount": 0}
        )
        assert res.buyEnergyCount == 3
        assert res.pointGoldCount == 1

    def test_buy_energy_packets(self) -> None:
        """24005 包含 buyTimes，24006 包含 errorCode、addList、costList。"""
        req = BuyEnergyPacket(buyTimes=2)
        env = GameEnvelope(token="test_tok", player_id=1001)
        raw = env.encode_with(req)
        assert len(raw) > 0

        energy_id = int(CONSTS["ENERGY_ID"])
        diamond_id = int(CONSTS["DIAMOND_ID"])
        res = BuyEnergyRes.model_validate(
            {
                "errorCode": 0,
                "addList": [
                    {
                        "itemList": [
                            {"itemSid": 1, "itemID": energy_id, "itemAmount": 240}
                        ]
                    }
                ],
                "costList": [
                    {
                        "itemList": [
                            {"itemSid": 2, "itemID": diamond_id, "itemAmount": 200}
                        ]
                    }
                ],
            }
        )
        assert res.is_success()
        assert res.energy_gained() == 240
        assert res.diamond_spent() == 200


def _make_fake_session() -> GameSession:
    state = GameSessionState(
        token="fake_token_123",
        player_id=999888,
        server_id=1,
        server_name="测试1区",
    )
    return GameSession(game_state=state)


class TestBuyEnergyTask:
    def test_read_only_when_times_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """times=0 时只查 VIPDetailPacket，不发购买包，不扣钻石。"""
        session = _make_fake_session()
        task = BuyEnergyTask(session, times=0)

        env = GameEnvelope(token="tok", player_id=123)
        vip_res = VIPDetailRes(buyEnergyCount=2)
        encoded_res = env.encode_with(vip_res, include_defaults=True)

        posted_packets: list[int] = []

        def fake_post_raw(self_client: GameClient, body: bytes) -> FakeResponse:
            view = parse_envelope(body)
            posted_packets.append(view.packet_id)
            return FakeResponse(encoded_res)

        monkeypatch.setattr(GameClient, "post_raw", fake_post_raw)

        res = task.execute()
        assert res.ok
        assert "今日已购买体力 2 次" in res.message
        assert posted_packets == [24011]

    def test_blocked_when_diamond_not_allowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """times > 0 但未开放钻石总闸时，被 fail-closed 拦截。"""
        session = _make_fake_session()
        task = BuyEnergyTask(
            session, times=1, spend_policy=SpendPolicy(allow_diamond=False)
        )

        env = GameEnvelope(token="tok", player_id=123)
        vip_res = VIPDetailRes(buyEnergyCount=0)
        encoded_res = env.encode_with(vip_res, include_defaults=True)

        def fake_post_raw(self_client: GameClient, body: bytes) -> FakeResponse:
            return FakeResponse(encoded_res)

        monkeypatch.setattr(GameClient, "post_raw", fake_post_raw)

        with pytest.raises(SpendBlockedError) as exc_info:
            task.execute()
        assert "钻石" in str(exc_info.value)

    def test_blocked_when_purpose_not_authorized(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """打开了钻石总闸但未授权 BUY_ENERGY 用途时，同样被拦截。"""
        session = _make_fake_session()
        task = BuyEnergyTask(
            session,
            times=1,
            spend_policy=SpendPolicy(
                allow_diamond=True,
                allowed_purposes=frozenset({SpendPurpose.BBQ_INVITE}),
            ),
        )

        env = GameEnvelope(token="tok", player_id=123)
        vip_res = VIPDetailRes(buyEnergyCount=0)
        encoded_res = env.encode_with(vip_res, include_defaults=True)

        def fake_post_raw(self_client: GameClient, body: bytes) -> FakeResponse:
            return FakeResponse(encoded_res)

        monkeypatch.setattr(GameClient, "post_raw", fake_post_raw)

        with pytest.raises(SpendBlockedError) as exc_info:
            task.execute()
        assert "未被授权" in str(exc_info.value)

    def test_dry_run_does_not_send_buy_packet(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """演练模式：仅发送 24011 查询，不发 24005 购买。"""
        session = _make_fake_session()
        policy = SpendPolicy(
            allow_diamond=True,
            allowed_purposes=frozenset({SpendPurpose.BUY_ENERGY}),
        )
        task = BuyEnergyTask(session, times=2, dry_run=True, spend_policy=policy)

        env = GameEnvelope(token="tok", player_id=123)
        vip_res = VIPDetailRes(buyEnergyCount=0)
        encoded_res = env.encode_with(vip_res, include_defaults=True)

        posted_packets: list[int] = []

        def fake_post_raw(self_client: GameClient, body: bytes) -> FakeResponse:
            view = parse_envelope(body)
            posted_packets.append(view.packet_id)
            return FakeResponse(encoded_res)

        monkeypatch.setattr(GameClient, "post_raw", fake_post_raw)

        res = task.execute()
        assert res.ok
        assert "[演练]" in res.message
        assert posted_packets == [24011]

    def test_max_cost_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """超过 max_cost 上限时，不花钻石，终止执行。"""
        session = _make_fake_session()
        policy = SpendPolicy(
            allow_diamond=True,
            allowed_purposes=frozenset({SpendPurpose.BUY_ENERGY}),
        )
        task = BuyEnergyTask(
            session, times=2, max_cost=100, spend_policy=policy
        )

        env = GameEnvelope(token="tok", player_id=123)
        vip_res = VIPDetailRes(buyEnergyCount=0)
        encoded_res = env.encode_with(vip_res, include_defaults=True)

        posted_packets: list[int] = []

        def fake_post_raw(self_client: GameClient, body: bytes) -> FakeResponse:
            view = parse_envelope(body)
            posted_packets.append(view.packet_id)
            return FakeResponse(encoded_res)

        monkeypatch.setattr(GameClient, "post_raw", fake_post_raw)

        res = task.execute()
        assert not res.ok
        assert "超过上限" in res.message
        assert posted_packets == [24011]

    def test_real_buy_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """真实购买流程：发 24011 查询 → 发 24005 购买 → 解析 24006 结果。"""
        session = _make_fake_session()
        policy = SpendPolicy(
            allow_diamond=True,
            allowed_purposes=frozenset({SpendPurpose.BUY_ENERGY}),
        )
        task = BuyEnergyTask(session, times=1, spend_policy=policy)

        env = GameEnvelope(token="tok", player_id=123)
        vip_res = VIPDetailRes(buyEnergyCount=1)

        energy_id = int(CONSTS["ENERGY_ID"])
        diamond_id = int(CONSTS["DIAMOND_ID"])
        buy_res = BuyEnergyRes(
            errorCode=0,
            addList=[
                GetObjClass(
                    itemList=[
                        ItemClass(itemSid=1, itemID=energy_id, itemAmount=120)
                    ]
                )
            ],
            costList=[
                GetObjClass(
                    itemList=[
                        ItemClass(itemSid=2, itemID=diamond_id, itemAmount=100)
                    ]
                )
            ],
        )

        posted_packets: list[int] = []

        def fake_post_raw(self_client: GameClient, body: bytes) -> FakeResponse:
            view = parse_envelope(body)
            posted_packets.append(view.packet_id)
            if view.packet_id == 24011:
                return FakeResponse(env.encode_with(vip_res, include_defaults=True))
            if view.packet_id == 24005:
                return FakeResponse(env.encode_with(buy_res, include_defaults=True))
            raise ValueError(f"Unexpected packet {view.packet_id}")

        monkeypatch.setattr(GameClient, "post_raw", fake_post_raw)

        res = task.execute()
        assert res.ok
        assert "成功购买 1 次体力" in res.message
        assert posted_packets == [24011, 24005]
