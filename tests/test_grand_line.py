"""辉煌航迹搜集物资自动领取单元测试。

【★ 2026-10-03 流程修订】任务改为镜像 10.3 抓包：34019（拉奖励目录）→ 34021（领取），
不再走 34015 主页查询（真实客户端领取流程不发它）。
"""

from __future__ import annotations

from typing import Any

from models.daily import GetObjClass, ItemClass
from models.game_packet import ProtoMessage
from models.grand_line import (
    ALLOWED_PACKET_IDS,
    FORBIDDEN_PACKET_IDS,
    GrandLineCountRewardClass,
    GrandLineCountRewardPacket,
    GrandLineCountRewardRes,
    GrandLineGetCountRewardPacket,
    GrandLineGetCountRewardRes,
)
from tasks.grand_line_supply import GrandLineSupplyTask
from tests.reward_guard import assert_no_bare_identifiers


from types import SimpleNamespace


class FakeGameClient:
    def __init__(self, responses: dict[type[ProtoMessage], Any]) -> None:
        self.responses = responses
        self.sent: list[ProtoMessage] = []

    def __enter__(self) -> FakeGameClient:
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def send(self, packet: ProtoMessage, **kwargs: Any) -> Any:
        self.sent.append(packet)
        resp = self.responses.get(type(packet))
        if resp is None:
            raise KeyError(f"Unexpected packet: {type(packet)}")
        return SimpleNamespace(decode_sub_packet=lambda cls: resp)


def test_packet_guard_whitelist() -> None:
    """白名单必须包含 34015/34019/34021（34019 为 10.3 抓包补入）。"""
    assert ALLOWED_PACKET_IDS["GrandLineHomePacket"] == 34015
    assert ALLOWED_PACKET_IDS["GrandLineCountRewardPacket"] == 34019
    assert ALLOWED_PACKET_IDS["GrandLineGetCountRewardPacket"] == 34021
    assert "GrandLineRebirthPacket" in FORBIDDEN_PACKET_IDS


def test_grand_line_empty_catalog_skips() -> None:
    """奖励目录为空（功能未开放）时平稳退出，不发 34021 领奖。"""
    catalog_res = GrandLineCountRewardRes(rewardItems=[])
    fake_client = FakeGameClient({GrandLineCountRewardPacket: catalog_res})

    task = GrandLineSupplyTask()
    task.open_game_client = lambda: fake_client  # type: ignore[assignment]

    result = task.execute()
    assert result.ok is True
    assert result.data["catalog_size"] == 0
    assert len(fake_client.sent) == 1
    assert isinstance(fake_client.sent[0], GrandLineCountRewardPacket)


def test_grand_line_dry_run() -> None:
    """演练模式下只发 34019 拉目录，不发 34021 领奖。"""
    catalog_res = GrandLineCountRewardRes(
        rewardItems=[GrandLineCountRewardClass(itemID=200491294, amount=24, isBonus=0)]
    )
    fake_client = FakeGameClient({GrandLineCountRewardPacket: catalog_res})

    task = GrandLineSupplyTask(dry_run=True)
    task.open_game_client = lambda: fake_client  # type: ignore[assignment]

    result = task.execute()
    assert result.ok is True
    assert result.data["dry_run"] is True
    assert result.data["catalog_size"] == 1
    assert len(fake_client.sent) == 1
    assert isinstance(fake_client.sent[0], GrandLineCountRewardPacket)


def test_grand_line_claim_success() -> None:
    """正常模式下先发 34019 拉目录，后发 34021 领奖（type=1, rewardID=-1）。

    奖励解析走 ``GetObjClass.itemList[].itemID/itemAmount`` ——
    旧代码读 ``.id/.count``（不存在的属性）会在成功时崩溃，此测试钉死该修复。
    """
    catalog_res = GrandLineCountRewardRes(
        rewardItems=[
            GrandLineCountRewardClass(itemID=200491294, amount=1, isBonus=1),
            GrandLineCountRewardClass(itemID=200490326, amount=24, isBonus=0),
        ]
    )
    claim_res = GrandLineGetCountRewardRes(
        errorCode=0,
        getObjList=[
            GetObjClass(
                itemList=[
                    ItemClass(itemSid=1, itemID=200200374, itemAmount=4432),
                    ItemClass(itemSid=2, itemID=200000050, itemAmount=20067561),
                ]
            )
        ],
    )
    fake_client = FakeGameClient({
        GrandLineCountRewardPacket: catalog_res,
        GrandLineGetCountRewardPacket: claim_res,
    })

    task = GrandLineSupplyTask(dry_run=False)
    task.open_game_client = lambda: fake_client  # type: ignore[assignment]

    result = task.execute()
    assert result.ok is True
    assert result.data["catalog_size"] == 2
    # ★ 2026-10-04「data 一并收敛」：逐条 {id, count} 明细不再进 data，
    # 改为只留**去重后的种数**（34021 的数量口径未实证，不报数量）。
    assert result.data["item_kinds"] == 2
    assert "items" not in result.data
    assert "获得 2 种物品" in result.message
    assert_no_bare_identifiers(result.message)
    assert len(fake_client.sent) == 2
    assert isinstance(fake_client.sent[0], GrandLineCountRewardPacket)
    assert isinstance(fake_client.sent[1], GrandLineGetCountRewardPacket)
    assert fake_client.sent[1].type == 1
    assert fake_client.sent[1].rewardID == -1


def test_grand_line_claim_failure_reports_code() -> None:
    """服务端拒绝领取（errorCode!=0）时如实上报失败。"""
    catalog_res = GrandLineCountRewardRes(
        rewardItems=[GrandLineCountRewardClass(itemID=200491294, amount=1, isBonus=0)]
    )
    claim_res = GrandLineGetCountRewardRes(errorCode=-1)
    fake_client = FakeGameClient({
        GrandLineCountRewardPacket: catalog_res,
        GrandLineGetCountRewardPacket: claim_res,
    })

    task = GrandLineSupplyTask(dry_run=False)
    task.open_game_client = lambda: fake_client  # type: ignore[assignment]

    result = task.execute()
    assert result.ok is False
    assert result.data["error_code"] == -1
