"""三个只读任务（竞技场 / 天命对决 / 失控炼成阵）的行为与安全边界测试。

运行方式：

    python -m pytest tests/test_tasks_competition_status.py -q
    python -m tests.test_tasks_competition_status

【本文件最重要的两条断言】

1. **每个任务只发自己白名单里的消息号**：从**请求字节**里解出 packetID 再逐一核对 ——
   不是"相信代码写对了"，而是拿证据说话；
2. **dry-run 与只读语义**：演练一个包都不发；真跑时也只发查询包
   （进战/结算/买次数的包在模型层就没有，源码级再查一遍）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from client.game_client import GameClient
from models.protobuf_wire import encode_int
from tasks.base import create_task
from tasks.packet_guard import guarded_send

# 复用荣耀之巅测试里的伪传输工具与临时会话（先例：tests/test_mail.py 复用 test_game_client）
from tests.test_tasks_top_pvp import (
    envelope_bytes,
    make_session,
    patch_transport,
    sent_packet_ids,
)

import tasks.arena_status  # noqa: F401  导入即注册
import tasks.wudou_status  # noqa: F401
import tasks.yimo_status  # noqa: F401

ARENA_HOME_ID = 26001
ARENA_HOME_RES_ID = 26002
WUDOU_HOME_ID = 26031
WUDOU_HOME_RES_ID = 26032
WUDOU_CHALLENGE_ID = 26033
WUDOU_CHALLENGE_RES_ID = 26034
YIMO_STATE_ID = 22101
YIMO_STATE_RES_ID = 22102


# ---------------------------------------------------------------------------
# 响应载荷（手工构造，字段号与 models/ 里声明的一致）
# ---------------------------------------------------------------------------
def arena_home_payload() -> bytes:
    """26002：``challengeCount``(4)=3、``buyedCount``(5)=1、``reflashCount``(6)=0。"""
    return encode_int(4, 3) + encode_int(5, 1) + encode_int(6, 0)


def wudou_home_payload() -> bytes:
    """26032：``myGroupID``(5)=7、``challenge_times``(6)=9。"""
    return encode_int(5, 7) + encode_int(6, 9)


def wudou_challenge_payload() -> bytes:
    """26034：``myRanking``(1)=149、``npc_times``(5)=2、``challenge_times``(6)=5。"""
    return (
        encode_int(1, 149)
        + encode_int(5, 2)
        + encode_int(6, 5)
        + encode_int(7, 0)
        + encode_int(8, 1790002800000)
    )


def yimo_state_payload() -> bytes:
    """22102：``remainCount``(1)=4、``nowScore``(3)=53247。"""
    return encode_int(1, 4) + encode_int(3, 53247)


def handler(number: int, _body: bytes) -> bytes:
    """按消息号返回构造好的响应（未预期的消息号直接报错，说明任务发错包了）。"""
    table = {
        ARENA_HOME_ID: (ARENA_HOME_RES_ID, arena_home_payload()),
        WUDOU_HOME_ID: (WUDOU_HOME_RES_ID, wudou_home_payload()),
        WUDOU_CHALLENGE_ID: (WUDOU_CHALLENGE_RES_ID, wudou_challenge_payload()),
        YIMO_STATE_ID: (YIMO_STATE_RES_ID, yimo_state_payload()),
    }
    if number not in table:
        raise AssertionError(f"测试替身收到未预期的消息号：{number}（说明任务发错包了）")
    response_id, payload = table[number]
    return envelope_bytes(response_id, payload)


def run_task(name: str, *, dry_run: bool = False):
    """构造并运行一个只读任务（会话是保留域名，不会真的联网）。"""
    return create_task(name, make_session(), dry_run=dry_run).run()


# ---------------------------------------------------------------------------
# ① 三个任务各自只发自己的只读包
# ---------------------------------------------------------------------------
class TestArenaStatus:
    def test_sends_only_home_packet(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded = patch_transport(monkeypatch, handler)
        result = run_task("arena_status")

        assert result.ok is True
        assert sent_packet_ids(recorded) == [ARENA_HOME_ID]
        assert result.data["challenge_count"] == 3
        assert "可挑战次数=3" in result.message

    def test_dry_run_sends_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded = patch_transport(monkeypatch, handler)
        result = run_task("arena_status", dry_run=True)

        assert recorded == []
        assert result.data["sent_packets"] == ()


class TestWuDouStatus:
    def test_sends_both_read_only_packets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded = patch_transport(monkeypatch, handler)
        result = run_task("wudou_status")

        assert result.ok is True
        assert sent_packet_ids(recorded) == [WUDOU_HOME_ID, WUDOU_CHALLENGE_ID]
        assert result.data["challenge_times"] == 5
        assert result.data["npc_times"] == 2
        assert result.data["my_ranking"] == 149

    def test_dry_run_sends_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded = patch_transport(monkeypatch, handler)
        result = run_task("wudou_status", dry_run=True)

        assert recorded == []
        assert result.data["planned_packets"] == (WUDOU_HOME_ID, WUDOU_CHALLENGE_ID)


class TestYimoStatus:
    def test_sends_only_state_packet(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded = patch_transport(monkeypatch, handler)
        result = run_task("yimo_status")

        assert result.ok is True
        assert sent_packet_ids(recorded) == [YIMO_STATE_ID]
        assert result.data["remain_count"] == 4
        assert result.data["now_score"] == 53247

    def test_dry_run_sends_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded = patch_transport(monkeypatch, handler)
        result = run_task("yimo_status", dry_run=True)

        assert recorded == []
        assert result.data["sent_packets"] == ()


# ---------------------------------------------------------------------------
# ② 公共守卫本身（tasks/packet_guard.py）—— "绝不进战/绝不买次数"的最后一道
# ---------------------------------------------------------------------------
class TestGuard:
    def test_forbidden_packet_is_rejected(self) -> None:
        """命中禁发清单 → 直接拒绝（连消息号都不查，更不会发包）。"""
        from models.pvp import ALLOWED_PACKET_IDS, FORBIDDEN_PACKET_IDS

        # 用同名假类触发"禁发"分支：ChallengePacket 按设计**没有**建模，无法实例化真类
        fake_challenge = type("ChallengePacket", (), {})()

        with pytest.raises(ValueError, match="被禁止"):
            guarded_send(
                None,  # type: ignore[arg-type]  不会走到网络层
                fake_challenge,  # type: ignore[arg-type]
                object,  # type: ignore[arg-type]
                allowed=ALLOWED_PACKET_IDS,
                forbidden=FORBIDDEN_PACKET_IDS,
            )

    def test_packet_outside_whitelist_is_rejected(self) -> None:
        """不在白名单里 → 拒绝（"默认禁止"比黑名单安全）。"""
        from models.pvp import FORBIDDEN_PACKET_IDS, PVPHomePacket

        with pytest.raises(ValueError, match="白名单"):
            guarded_send(
                None,  # type: ignore[arg-type]
                PVPHomePacket(),
                object,  # type: ignore[arg-type]
                allowed={},  # 空白名单 = 一个包都不允许
                forbidden=FORBIDDEN_PACKET_IDS,
            )


# ---------------------------------------------------------------------------
# ③ 非协议响应 → 可读失败（实测遇到过 22 字节 text/html 的 ack）
# ---------------------------------------------------------------------------
class TestNonProtocolResponse:
    def test_html_ack_is_reported_clearly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded = patch_transport(
            monkeypatch, lambda number, body: b'{"response_code":"OK"}'
        )
        result = run_task("yimo_status")

        assert result.ok is False
        assert "不是游戏协议" in result.message
        assert sent_packet_ids(recorded) == [YIMO_STATE_ID], "第一个包就异常，不该继续发"


# ---------------------------------------------------------------------------
# ④ 源码级回归：任务里**不许**实例化任何"买次数"的包
# ---------------------------------------------------------------------------
BUY_PACKET_NAMES = ("BuyChallengePacket", "WuDouBuyChallenge", "BuyYimoCountPacket")


@pytest.mark.parametrize(
    "module_path",
    [
        "tasks/arena_status.py",
        "tasks/wudou_status.py",
        "tasks/yimo_status.py",
        "tasks/top_pvp.py",
    ],
)
def test_task_sources_never_instantiate_buy_packets(module_path: str) -> None:
    """源码级回归：任务文件里不出现 `XxxBuy...(` 这种实例化。

    【为什么用"名字后面跟括号"而不是纯子串匹配】
    ``FORBIDDEN_PACKET_IDS`` 这个**变量名**是允许出现的（任务要把它交给守卫），
    而真正危险的是**实例化**。用正则要求"名字 + 左括号"能精确区分两者。
    """
    import re

    root = Path(__file__).resolve().parents[1]
    source = (root / module_path).read_text(encoding="utf-8")

    for name in BUY_PACKET_NAMES:
        assert re.search(rf"\b{name}\s*\(", source) is None, f"{module_path} 不该实例化 {name}"


if __name__ == "__main__":  # pragma: no cover - 便于直接运行本文件
    raise SystemExit(pytest.main([__file__, "-q"]))