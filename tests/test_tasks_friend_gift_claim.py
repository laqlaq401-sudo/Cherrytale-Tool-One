"""「好友赠礼一键领取」（``tasks/friend_gift_claim.py``）的单元测试。

运行方式：

    python -m pytest tests/test_tasks_friend_gift_claim.py
    python -m tests.test_tasks_friend_gift_claim

【本文件要钉死的四条】
1. **报文字节与抓包一致**：10009 子包 = 13 字节 ``{actionType=3, playerID=-1}``
   （``captures/好友礼物 一键赠送 一键领取.saz`` 的 raw/13）；
2. **白名单只有 10009**：连好友列表 10001 都发不出去；
3. **三种响应都是软成功**：领到 / 空列表 / 非 0 错误码 —— 只有网络与鉴权异常才算失败；
4. **口径**：道具数量未实证 ⇒ 文案里不出现具体数字与裸道具 ID。

全部离线：不读网络、不依赖任何真实环境。
"""

from __future__ import annotations

from types import SimpleNamespace

from client.session import GameSession
from models.daily import GetObjClass, ItemClass
from models.friend import FriendOperationPacket, FriendOperationRes
from models.session_state import GameSessionState
from tasks.base import create_task
from tests.reward_guard import assert_no_bare_identifiers

import tasks.friend_gift_claim as claim_mod  # 导入即注册


def make_session() -> GameSession:
    """构造一个**不会联网**的会话（域名是保留域名）。"""
    session = GameSession("https://example.invalid", timeout=1.0)
    session.token_store.update(uid="u1", token="tok")
    session.set_game_state(GameSessionState(token="tok", player_id=1, server_id=1))
    return session


class FakeGuard:
    """替换模块级 ``guarded_send``：记录调用参数 + 回放固定响应。"""

    def __init__(self, res: FriendOperationRes) -> None:
        self.res = res
        self.calls: list[SimpleNamespace] = []

    def __call__(
        self,
        game: object,
        packet: object,
        expect: type,
        *,
        allowed: dict[str, int],
        forbidden: dict[str, int],
        record: list[int] | None = None,
        purpose: dict[int, str] | None = None,
        logger: object = None,
        include_defaults: bool = False,
    ) -> object:
        self.calls.append(
            SimpleNamespace(
                packet=packet,
                expect=expect,
                allowed=dict(allowed),
                forbidden=dict(forbidden),
                purpose=dict(purpose or {}),
                include_defaults=include_defaults,
            )
        )
        if record is not None:
            record.append(10009)
        return SimpleNamespace(decode_sub_packet=lambda cls, _r=self.res: _r)


class FakeGame:
    """只实现演练分支用到的 ``build()``。"""

    def build(self, packet: object, include_defaults: bool = False) -> object:
        return SimpleNamespace(describe=lambda: f"{type(packet).__name__}（演练）")


def make_res(**kwargs: object) -> FriendOperationRes:
    return FriendOperationRes(**kwargs)  # type: ignore[arg-type]


class TestPacketBytes:
    def test_matches_capture(self) -> None:
        """raw/13：子包 13 B，``08 03 10`` + uint64(-1) 的 10 字节 varint。"""
        packet = FriendOperationPacket(actionType=3, playerID=-1)
        assert packet.encode(include_defaults=True).hex() == (
            "080310ffffffffffffffffff01"
        )
        assert len(packet.encode(include_defaults=True)) == 13


class TestClaim:
    def test_sends_only_gain_all_gift_with_minus_one(self, monkeypatch) -> None:
        task = create_task("friend_gift_claim", make_session())
        guard = FakeGuard(make_res(errorCode=0, friendList=[1, 2]))
        monkeypatch.setattr(claim_mod, "guarded_send", guard)

        task._run(None)  # type: ignore[arg-type]

        assert len(guard.calls) == 1
        call = guard.calls[0]
        assert call.expect is FriendOperationRes
        assert call.packet.actionType == 3  # type: ignore[attr-defined]
        assert call.packet.playerID == -1  # type: ignore[attr-defined]
        # 白名单只有 10009：连好友列表 10001 都不在名单里
        assert call.allowed == {"FriendOperationPacket": 10009}
        assert call.include_defaults is True
        assert task.sent_packet_ids == [10009]

    def test_success_reports_friend_and_item_counts(self, monkeypatch) -> None:
        task = create_task("friend_gift_claim", make_session())
        res = make_res(
            errorCode=0,
            friendList=list(range(1, 26)),  # 与抓包同规模：25 位好友
            itemList=[
                GetObjClass(
                    itemList=[ItemClass(itemSid=1, itemID=200000002, itemAmount=67)]
                )
            ],
        )
        monkeypatch.setattr(claim_mod, "guarded_send", FakeGuard(res))

        result = task._run(None)  # type: ignore[arg-type]

        assert result.ok is True
        assert result.data["friend_count"] == 25
        assert result.data["item_group_count"] == 1
        assert result.data["error_code"] == 0
        assert "25 位好友" in result.message
        assert "1 件道具" in result.message
        # 口径未实证：不许出现具体数量，也不许把道具 ID 当名字念出来
        assert "67" not in result.message
        assert_no_bare_identifiers(result.message)

    def test_empty_result_is_soft_success(self, monkeypatch) -> None:
        task = create_task("friend_gift_claim", make_session())
        monkeypatch.setattr(
            claim_mod, "guarded_send", FakeGuard(make_res(errorCode=0, friendList=[]))
        )

        result = task._run(None)  # type: ignore[arg-type]

        assert result.ok is True
        assert result.data["friend_count"] == 0
        assert "没有可领取" in result.message

    def test_nonzero_error_is_soft_success(self, monkeypatch) -> None:
        task = create_task("friend_gift_claim", make_session())
        monkeypatch.setattr(
            claim_mod, "guarded_send", FakeGuard(make_res(errorCode=-1))
        )

        result = task._run(None)  # type: ignore[arg-type]

        assert result.ok is True
        assert result.data["error_code"] == -1
        assert "无待领" in result.message


class TestDryRun:
    def test_sends_nothing(self, monkeypatch, capsys) -> None:
        task = create_task("friend_gift_claim", make_session(), dry_run=True)
        guard = FakeGuard(make_res(errorCode=0))
        monkeypatch.setattr(claim_mod, "guarded_send", guard)

        result = task._dry_run(FakeGame())  # type: ignore[arg-type]

        assert guard.calls == []  # 一个包都没发
        assert result.ok is True
        assert result.data["sent_packets"] == ()
        printed = capsys.readouterr().out
        assert "10009" in printed and "一键领取" in printed
