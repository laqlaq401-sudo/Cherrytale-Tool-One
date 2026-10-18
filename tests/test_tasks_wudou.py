"""天命对决任务（``tasks/wudou_battle.py``）的行为与安全边界测试。

运行方式：

    python -m pytest tests/test_tasks_wudou.py -q
    python -m tests.test_tasks_wudou

【本测试钉死的核心规范】
1. **默认只读（battles=0）**：仅拉取主页（26031）与挑战页（26033），绝不发起挑战（26051）或核验（26053）；
2. **绝对安全边界**：出站报文 ⊆ ALLOWED_PACKET_IDS，绝不发送 26047（花钻石买次数）、26037（进战）或 26039（旧结算）；
3. **选人策略算法**：三种策略（lowest_power / npc_first / rank_first）的精准选择与兜底；
4. **熔断保护**：服务端返回 -1（次数不足）时安全中止，绝不花钻购次；
5. **演练模式**：--dry-run 零网络请求。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import config
from client.game_client import GameClient
from client.session import GameSession
from models.envelope import parse_envelope
from models.protobuf_wire import encode_int, encode_message, encode_string
from models.session_state import GameSessionState
from models.wudou import (
    ALLOWED_PACKET_IDS,
    FORBIDDEN_PACKET_IDS,
    PVPListClass,
)
from tasks.base import RunOptions, create_task
from tasks.wudou_battle import WuDouBattleTask, select_opponent
from tests.test_game_client import FakeResponse

#: 消息号常量
HOME_ID = 26031
HOME_RES_ID = 26032
CHALLENGE_HOME_ID = 26033
CHALLENGE_HOME_RES_ID = 26034
RANK_ID = 26053
RANK_RES_ID = 26054
REPLAY_ID = 26051
REPLAY_RES_ID = 26052
BUY_ID = 26047


# ---------------------------------------------------------------------------
# 离线辅助工具与 Payload 构造
# ---------------------------------------------------------------------------
def make_session() -> GameSession:
    """构造不联网的离线会话。"""
    session = GameSession("https://example.invalid", timeout=1.0)
    session.token_store.update(uid="u1", token="tok")
    session.set_game_state(GameSessionState(token="tok", player_id=1, server_id=1))
    return session


def envelope_bytes(packet_id: int, payload: bytes) -> bytes:
    """构造标准信封响应字节。"""
    return encode_int(1, packet_id) + encode_message(packet_id, payload)


def home_res_payload(*, team_id: int = 14, challenge_times: int = 5) -> bytes:
    """构造 26032 WuDouHomeRes 字节。"""
    team_bytes = encode_int(1, team_id)
    return encode_message(2, team_bytes) + encode_int(6, challenge_times)


def pvp_player_bytes(
    player_id: int,
    ranking: int,
    name: str,
    fighting: int,
    integral: int = 1000,
) -> bytes:
    """构造单个 PVPListClass 字节。"""
    return (
        encode_int(1, player_id)
        + encode_int(2, ranking)
        + encode_string(4, name)
        + encode_int(5, fighting)
        + encode_int(6, integral)
    )


def challenge_home_res_payload(
    *,
    my_ranking: int = 2000,
    challenge_times: int = 5,
    opponents: list[tuple[int, int, str, int, int]] | None = None,
) -> bytes:
    """构造 26034 WuDouChallengeHomeRes 字节。"""
    if opponents is None:
        opponents = [
            (1001, 1950, "玩家A", 150000, 1000),
            (1002, 1920, "玩家B", 120000, 1000),
            (-101, 1900, "NPC训练师", 100000, 0),
        ]
    payload = encode_int(1, my_ranking)
    for pid, rnk, name, fight, integ in opponents:
        payload += encode_message(2, pvp_player_bytes(pid, rnk, name, fight, integ))
    payload += encode_int(5, 0)  # npc_times
    payload += encode_int(6, challenge_times)
    payload += encode_int(7, 0)  # buyedCount
    return payload


def rank_res_payload(error_code: int = 0) -> bytes:
    """构造 26054 WuDouRankRes 字节。"""
    return encode_int(1, error_code)


def replay_res_payload(
    *,
    error_code: int = 0,
    win: bool = True,
    my_ranking: int = 1900,
    challenge_times: int = 4,
) -> bytes:
    """构造 26052 WuDouReplayRes 字节。"""
    replay_json = json.dumps(
        {
            "winOrFail": win,
            "elapsed": 2.5,
            "step": 15,
            "isNpc": False,
            "error": False,
        },
        ensure_ascii=False,
    )
    return (
        encode_int(1, error_code)
        + encode_string(2, replay_json)
        + encode_int(4, my_ranking)
        + encode_int(6, challenge_times)
    )


def patch_transport(monkeypatch: pytest.MonkeyPatch, handler) -> list[bytes]:
    """替换 GameClient.post_raw 以捕获出站字节并返回假响应。"""
    recorded: list[bytes] = []

    def fake_post_raw(self: GameClient, body: bytes) -> FakeResponse:
        recorded.append(body)
        number = parse_envelope(body).packet_id
        return FakeResponse(handler(number, body))

    monkeypatch.setattr(GameClient, "post_raw", fake_post_raw)
    return recorded


def sent_packet_ids(recorded: list[bytes]) -> list[int]:
    """从捕获的请求字节解析消息号。"""
    return [parse_envelope(body).packet_id for body in recorded]


# ---------------------------------------------------------------------------
# ① 选人策略算法测试
# ---------------------------------------------------------------------------
class TestSelectOpponent:
    def test_lowest_power_strategy(self) -> None:
        opponents = [
            PVPListClass(playerID=1, ranking=100, name="强敌", fighting=500000),
            PVPListClass(playerID=2, ranking=120, name="弱鸡", fighting=100000),
            PVPListClass(playerID=3, ranking=110, name="中等", fighting=300000),
        ]
        picked = select_opponent(opponents, "lowest_power")
        assert picked is not None
        assert picked.playerID == 2
        assert picked.fighting == 100000

    def test_npc_first_strategy_with_npc(self) -> None:
        opponents = [
            PVPListClass(playerID=1, ranking=100, name="普通玩家", fighting=50000),
            PVPListClass(playerID=-101, ranking=120, name="NPC训练家A", fighting=80000),
            PVPListClass(playerID=-102, ranking=110, name="NPC训练家B", fighting=60000),
        ]
        # 有 NPC 时优先打 NPC，若有多个 NPC 选战力最低的
        picked = select_opponent(opponents, "npc_first")
        assert picked is not None
        assert picked.playerID == -102
        assert picked.is_npc is True

    def test_npc_first_strategy_fallback_when_no_npc(self) -> None:
        opponents = [
            PVPListClass(playerID=1, ranking=100, name="玩家1", fighting=300000),
            PVPListClass(playerID=2, ranking=120, name="玩家2", fighting=150000),
        ]
        picked = select_opponent(opponents, "npc_first")
        assert picked is not None
        assert picked.playerID == 2
        assert picked.fighting == 150000

    def test_rank_first_strategy(self) -> None:
        opponents = [
            PVPListClass(playerID=1, ranking=1500, name="名次靠后战力低", fighting=100000),
            PVPListClass(playerID=2, ranking=1200, name="名次靠前", fighting=300000),
            PVPListClass(playerID=3, ranking=1400, name="名次中等", fighting=200000),
        ]
        picked = select_opponent(opponents, "rank_first")
        assert picked is not None
        assert picked.playerID == 2
        assert picked.ranking == 1200

    def test_empty_opponents_returns_none(self) -> None:
        assert select_opponent([]) is None


# ---------------------------------------------------------------------------
# ② 任务行为与安全测试
# ---------------------------------------------------------------------------
class TestWuDouBattleTask:
    def test_dry_run_mode(self) -> None:
        session = make_session()
        task = create_task("wudou_pvp_battle", session, dry_run=True, run_options=RunOptions(battles=3))
        result = task.run()

        assert result.ok is True
        assert "演练模式" in result.message
        assert result.data["mode"] == "dry_run"
        assert len(task.sent_packet_ids) == 0

    def test_battles_zero_default_runs_all_available_times(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """默认 battles=0：打完所有剩余可用次数（若有 2 次则打完 2 场）。"""
        monkeypatch.setattr(config, "WUDOU_PVP_INTERVAL_MIN", 0.0)
        monkeypatch.setattr(config, "WUDOU_PVP_INTERVAL_MAX", 0.0)

        remaining = 2

        def handler(number: int, body: bytes) -> bytes:
            nonlocal remaining
            if number == HOME_ID:
                return envelope_bytes(HOME_RES_ID, home_res_payload(challenge_times=remaining))
            if number == CHALLENGE_HOME_ID:
                return envelope_bytes(CHALLENGE_HOME_RES_ID, challenge_home_res_payload(challenge_times=remaining))
            if number == RANK_ID:
                return envelope_bytes(RANK_RES_ID, rank_res_payload(error_code=0))
            if number == REPLAY_ID:
                remaining -= 1
                return envelope_bytes(
                    REPLAY_RES_ID,
                    replay_res_payload(error_code=0, win=True, challenge_times=remaining),
                )
            raise AssertionError(f"未预期的消息号: {number}")

        recorded = patch_transport(monkeypatch, handler)
        task = create_task("wudou_pvp_battle", make_session(), run_options=RunOptions(battles=0))
        result = task.run()

        assert result.ok is True
        assert "完成 2/2 场" in result.message
        assert result.data["battles_done"] == 2
        packets = sent_packet_ids(recorded)
        assert packets.count(REPLAY_ID) == 2
        assert BUY_ID not in packets

    def test_zero_remaining_times_stops_cleanly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """剩余挑战次数为 0 时立即停止，不发起挑战，绝不购买次数。"""
        def handler(number: int, body: bytes) -> bytes:
            if number == HOME_ID:
                return envelope_bytes(HOME_RES_ID, home_res_payload(challenge_times=0))
            if number == CHALLENGE_HOME_ID:
                return envelope_bytes(CHALLENGE_HOME_RES_ID, challenge_home_res_payload(challenge_times=0))
            raise AssertionError(f"零次数时不该发送消息号 {number}")

        recorded = patch_transport(monkeypatch, handler)
        task = create_task("wudou_pvp_battle", make_session(), run_options=RunOptions(battles=2))
        result = task.run()

        assert result.ok is True
        assert "挑战次数不足" in result.message
        assert sent_packet_ids(recorded) == [HOME_ID, CHALLENGE_HOME_ID]

    def test_successful_battles_and_whitelist_packets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """正常完成两场挑战：完整核验出站报文必须全在白名单中，且绝无 26047。"""
        monkeypatch.setattr(config, "WUDOU_PVP_INTERVAL_MIN", 0.0)
        monkeypatch.setattr(config, "WUDOU_PVP_INTERVAL_MAX", 0.0)

        remaining = 5

        def handler(number: int, body: bytes) -> bytes:
            nonlocal remaining
            if number == HOME_ID:
                return envelope_bytes(HOME_RES_ID, home_res_payload(team_id=14, challenge_times=remaining))
            if number == CHALLENGE_HOME_ID:
                return envelope_bytes(CHALLENGE_HOME_RES_ID, challenge_home_res_payload(challenge_times=remaining))
            if number == RANK_ID:
                return envelope_bytes(RANK_RES_ID, rank_res_payload(error_code=0))
            if number == REPLAY_ID:
                remaining -= 1
                return envelope_bytes(
                    REPLAY_RES_ID,
                    replay_res_payload(error_code=0, win=True, my_ranking=1800, challenge_times=remaining),
                )
            raise AssertionError(f"未预期的消息号: {number}")

        recorded = patch_transport(monkeypatch, handler)
        task = create_task(
            "wudou_pvp_battle",
            make_session(),
            run_options=RunOptions(battles=2, interval_seconds=0.0),
            strategy="npc_first",
        )
        result = task.run()

        assert result.ok is True
        assert "完成 2/2 场" in result.message
        assert result.data["wins"] == 2
        assert result.data["strategy"] == "npc_first"

        # 验证发包序列与白名单
        packets = sent_packet_ids(recorded)
        assert packets == [
            HOME_ID,           # 26031 读主页
            CHALLENGE_HOME_ID, # 26033 读挑战页
            RANK_ID,           # 26053 第一场核验
            REPLAY_ID,         # 26051 第一场挑战
            CHALLENGE_HOME_ID, # 26033 刷新挑战页
            RANK_ID,           # 26053 第二场核验
            REPLAY_ID,         # 26051 第二场挑战
        ]
        # 红线断言
        assert set(packets).issubset(set(ALLOWED_PACKET_IDS.values()))
        assert BUY_ID not in packets
        for forbidden_id in FORBIDDEN_PACKET_IDS.values():
            assert forbidden_id not in packets

    def test_circuit_breaker_on_error_code_minus_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """挑战返回 -1（次数已尽）时熔断中止，不继续打下一场，判定为正常完成。"""
        monkeypatch.setattr(config, "WUDOU_PVP_INTERVAL_MIN", 0.0)
        monkeypatch.setattr(config, "WUDOU_PVP_INTERVAL_MAX", 0.0)

        def handler(number: int, body: bytes) -> bytes:
            if number == HOME_ID:
                return envelope_bytes(HOME_RES_ID, home_res_payload(challenge_times=1))
            if number == CHALLENGE_HOME_ID:
                return envelope_bytes(CHALLENGE_HOME_RES_ID, challenge_home_res_payload(challenge_times=1))
            if number == RANK_ID:
                return envelope_bytes(RANK_RES_ID, rank_res_payload(error_code=0))
            if number == REPLAY_ID:
                # 服务端返回 -1：次数不足
                return envelope_bytes(REPLAY_RES_ID, replay_res_payload(error_code=-1, win=False))
            raise AssertionError(f"未预期的消息号: {number}")

        recorded = patch_transport(monkeypatch, handler)
        task = create_task(
            "wudou_pvp_battle",
            make_session(),
            run_options=RunOptions(battles=3, interval_seconds=0.0),
        )
        result = task.run()

        assert result.ok is True
        assert "挑战次数已用完" in result.message
        # 即使计划 3 场，遇到 -1 立即停止，只发了 1 场挑战包
        packets = sent_packet_ids(recorded)
        assert packets.count(REPLAY_ID) == 1

    def test_rank_conflict_recovery(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """26053 返回 -3（排名冲突）时刷新挑战页并重试。"""
        monkeypatch.setattr(config, "WUDOU_PVP_INTERVAL_MIN", 0.0)
        monkeypatch.setattr(config, "WUDOU_PVP_INTERVAL_MAX", 0.0)

        rank_attempts = 0

        def handler(number: int, body: bytes) -> bytes:
            nonlocal rank_attempts
            if number == HOME_ID:
                return envelope_bytes(HOME_RES_ID, home_res_payload(challenge_times=1))
            if number == CHALLENGE_HOME_ID:
                return envelope_bytes(CHALLENGE_HOME_RES_ID, challenge_home_res_payload(challenge_times=1))
            if number == RANK_ID:
                rank_attempts += 1
                if rank_attempts == 1:
                    # 第一次返回 -3 排名冲突
                    return envelope_bytes(RANK_RES_ID, rank_res_payload(error_code=-3))
                # 第二次成功
                return envelope_bytes(RANK_RES_ID, rank_res_payload(error_code=0))
            if number == REPLAY_ID:
                return envelope_bytes(REPLAY_RES_ID, replay_res_payload(error_code=0, win=True, challenge_times=0))
            raise AssertionError(f"未预期的消息号: {number}")

        recorded = patch_transport(monkeypatch, handler)
        task = create_task(
            "wudou_pvp_battle",
            make_session(),
            run_options=RunOptions(battles=1, interval_seconds=0.0),
        )
        result = task.run()

        assert result.ok is True
        assert "完成 1/1 场" in result.message
        assert rank_attempts == 2
        # 验证经历过一次 26033 刷新
        packets = sent_packet_ids(recorded)
        assert packets == [HOME_ID, CHALLENGE_HOME_ID, RANK_ID, CHALLENGE_HOME_ID, RANK_ID, REPLAY_ID]

    def test_battles_minus_one_runs_all_available_times(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """battles=-1（打完所有剩余次数）模式：自动对齐为服务端实际剩余的 3 次并全部打完。"""
        monkeypatch.setattr(config, "WUDOU_PVP_INTERVAL_MIN", 0.0)
        monkeypatch.setattr(config, "WUDOU_PVP_INTERVAL_MAX", 0.0)

        remaining = 3

        def handler(number: int, body: bytes) -> bytes:
            nonlocal remaining
            if number == HOME_ID:
                return envelope_bytes(HOME_RES_ID, home_res_payload(challenge_times=remaining))
            if number == CHALLENGE_HOME_ID:
                return envelope_bytes(CHALLENGE_HOME_RES_ID, challenge_home_res_payload(challenge_times=remaining))
            if number == RANK_ID:
                return envelope_bytes(RANK_RES_ID, rank_res_payload(error_code=0))
            if number == REPLAY_ID:
                remaining -= 1
                return envelope_bytes(
                    REPLAY_RES_ID,
                    replay_res_payload(error_code=0, win=True, challenge_times=remaining),
                )
            raise AssertionError(f"未预期的消息号: {number}")

        recorded = patch_transport(monkeypatch, handler)
        task = create_task(
            "wudou_pvp_battle",
            make_session(),
            run_options=RunOptions(battles=-1, interval_seconds=0.0),
        )
        result = task.run()

        assert result.ok is True
        assert "完成 3/3 场" in result.message
        assert result.data["battles_done"] == 3
        # REPLAY_ID 恰好发了 3 次
        packets = sent_packet_ids(recorded)
        assert packets.count(REPLAY_ID) == 3

    def test_requested_battles_capped_to_actual_available_times(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """用户要求 5 场，但服务端实际只有 2 次：自动对齐为 2 场，绝不发送第 3 场进战包。"""
        monkeypatch.setattr(config, "WUDOU_PVP_INTERVAL_MIN", 0.0)
        monkeypatch.setattr(config, "WUDOU_PVP_INTERVAL_MAX", 0.0)

        remaining = 2

        def handler(number: int, body: bytes) -> bytes:
            nonlocal remaining
            if number == HOME_ID:
                return envelope_bytes(HOME_RES_ID, home_res_payload(challenge_times=remaining))
            if number == CHALLENGE_HOME_ID:
                return envelope_bytes(CHALLENGE_HOME_RES_ID, challenge_home_res_payload(challenge_times=remaining))
            if number == RANK_ID:
                return envelope_bytes(RANK_RES_ID, rank_res_payload(error_code=0))
            if number == REPLAY_ID:
                remaining -= 1
                return envelope_bytes(
                    REPLAY_RES_ID,
                    replay_res_payload(error_code=0, win=True, challenge_times=remaining),
                )
            raise AssertionError(f"未预期的消息号: {number}")

        recorded = patch_transport(monkeypatch, handler)
        task = create_task(
            "wudou_pvp_battle",
            make_session(),
            run_options=RunOptions(battles=5, interval_seconds=0.0),
        )
        result = task.run()

        assert result.ok is True
        assert "完成 2/2 场" in result.message
        packets = sent_packet_ids(recorded)
        assert packets.count(REPLAY_ID) == 2

    def test_rank_verify_error_5_interrupts_packet_sending(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """26053 返回 5 (HaveBeenUsedChallengeCount) 时立即中断发包，绝不发送 26051。"""
        monkeypatch.setattr(config, "WUDOU_PVP_INTERVAL_MIN", 0.0)
        monkeypatch.setattr(config, "WUDOU_PVP_INTERVAL_MAX", 0.0)

        def handler(number: int, body: bytes) -> bytes:
            if number == HOME_ID:
                return envelope_bytes(HOME_RES_ID, home_res_payload(challenge_times=1))
            if number == CHALLENGE_HOME_ID:
                return envelope_bytes(CHALLENGE_HOME_RES_ID, challenge_home_res_payload(challenge_times=1))
            if number == RANK_ID:
                # 服务端返回 5：次数已耗尽
                return envelope_bytes(RANK_RES_ID, rank_res_payload(error_code=5))
            if number == REPLAY_ID:
                raise AssertionError("核验返回次数已尽时绝不应发送 26051！")
            raise AssertionError(f"未预期的消息号: {number}")

        recorded = patch_transport(monkeypatch, handler)
        task = create_task(
            "wudou_pvp_battle",
            make_session(),
            run_options=RunOptions(battles=3, interval_seconds=0.0),
        )
        result = task.run()

        assert result.ok is True
        assert "挑战次数已用完" in result.message
        packets = sent_packet_ids(recorded)
        assert REPLAY_ID not in packets

    def test_offensive_team_and_include_defaults_verified(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """验证 26051 默认使用进攻队 teamID=70，且 26053 显式序列化 isBlock=False。"""
        monkeypatch.setattr(config, "WUDOU_PVP_INTERVAL_MIN", 0.0)
        monkeypatch.setattr(config, "WUDOU_PVP_INTERVAL_MAX", 0.0)

        def handler(number: int, body: bytes) -> bytes:
            if number == HOME_ID:
                return envelope_bytes(HOME_RES_ID, home_res_payload(team_id=14, challenge_times=1))
            if number == CHALLENGE_HOME_ID:
                return envelope_bytes(CHALLENGE_HOME_RES_ID, challenge_home_res_payload(challenge_times=1))
            if number == RANK_ID:
                return envelope_bytes(RANK_RES_ID, rank_res_payload(error_code=0))
            if number == REPLAY_ID:
                return envelope_bytes(REPLAY_RES_ID, replay_res_payload(error_code=0, win=True, challenge_times=0))
            raise AssertionError(f"未预期的消息号: {number}")

        recorded = patch_transport(monkeypatch, handler)
        task = create_task(
            "wudou_pvp_battle",
            make_session(),
            run_options=RunOptions(battles=1, interval_seconds=0.0),
        )
        result = task.run()
        assert result.ok is True

        from models.wudou import WuDouRankPacket, WuDouReplayPacket
        rank_body = next(b for b in recorded if parse_envelope(b).packet_id == RANK_ID)
        replay_body = next(b for b in recorded if parse_envelope(b).packet_id == REPLAY_ID)

        rank_packet = parse_envelope(rank_body).decode_sub_packet(WuDouRankPacket)
        replay_packet = parse_envelope(replay_body).decode_sub_packet(WuDouReplayPacket)

        # 验证 26051 使用进攻队 70 而非防守队 14
        assert replay_packet.teamID == 70
        # 验证 26053 的 isBlock 字段已编码进字节流（Tag 4 = 0 -> 0x20 0x00）
        assert b"\x20\x00" in rank_body

