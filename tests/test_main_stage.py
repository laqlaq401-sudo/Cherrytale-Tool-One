"""主线关卡通关主动任务（tasks/main_stage.py）单元测试。

【测试覆盖范围】
1. 算法与存证构造：
   - ``make_fight_verify_token`` 签名算法（MD5 拼接与固定盐值 03120908）。
   - ``make_battle_server_verify`` 存证报文结构（gzip + base64 解码后的 JSON 校验）。
2. 安全护栏与风控规则（最高优先级）：
   - 遇 ``cheatCheck=1`` 敏感校验关卡：必须绝对停机，严禁发进战/结算包，绝不提供绕过。
   - 体力不足：必须平稳停机，绝不调用购买体力协议。
   - 禁发清单与白名单：严禁出站未授权或扣钻报文（BuyEnergyPacket 24005 与 VIPDetailPacket 24011）。
3. 业务推进与结算：
   - 三星结果保证（``starCount=[1, 1, 1]``）。
   - 小章节最后一关通关后自动领取满星宝箱（11007 ReceiveStarRewardPacket）。
   - 目标关卡达成与最大推关上限控制。
4. 演练模式（Dry-Run）：零网络请求，报文结构与主线统计展示。
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
from typing import Any

import pytest

from client.game_client import GameClient
from client.session import GameSession
from models.envelope import parse_envelope
from models.game_config import MainStageSection, load_main_stage_index
from models.main_stage import (
    ALLOWED_PACKET_IDS,
    FORBIDDEN_PACKET_IDS,
    TEAM_TYPE_PVE1,
    FightingRequestPacket,
    FightingResultPacket,
    GetLastSectionPacket,
    ReceiveStarRewardPacket,
    SetUseTeamIDPacket,
    make_battle_server_verify,
    make_fight_verify_token,
)
from models.protobuf_wire import encode_int, encode_message, encode_string
from models.session_state import GameSessionState
from tasks.base import create_task
from tasks.main_stage import MainStageTask
from tests.test_game_client import FakeResponse

import tasks.main_stage as main_stage_task


# ---------------------------------------------------------------------------
# 测试固件与辅助工具
# ---------------------------------------------------------------------------
def no_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """把任务里"模拟真人节奏"的等待压成 0，测试不必真等。

    【为什么不是 ``monkeypatch.setattr("time.sleep", ...)``】
    任务用的是 ``cancellation.sleep_interruptible()``，内部走
    ``threading.Event.wait()`` + ``time.monotonic()``，**不经过 ``time.sleep``**；
    对 ``time.sleep`` 打补丁完全无效，会让每次首通真的空等
    ``time_cost_sec``（15~22 秒），多关累积即为"测试挂起"（2026-10-05 定位）。
    """
    monkeypatch.setattr(main_stage_task.cancellation, "sleep_interruptible", lambda _: None)


def make_test_session() -> GameSession:
    """构造离线会话，注入最小可用登录态。"""
    session = GameSession("https://example.invalid", timeout=1.0)
    session.token_store.update(uid="test_user", token="test_token")
    session.set_game_state(GameSessionState(token="test_token", player_id=10000003, server_id=113))
    return session


def envelope_bytes(packet_id: int, payload: bytes) -> bytes:
    """构造合法的游戏响应信封字节。"""
    return encode_int(1, packet_id) + encode_message(packet_id, payload)


def patch_transport(monkeypatch: pytest.MonkeyPatch, handler) -> list[bytes]:
    """拦截 GameClient.post_raw，记录出站字节并返回 handler 构造的响应。"""
    recorded: list[bytes] = []

    def fake_post_raw(self: GameClient, body: bytes) -> FakeResponse:
        recorded.append(body)
        envelope = parse_envelope(body)
        pid = envelope.packet_id
        return FakeResponse(handler(pid, body))

    monkeypatch.setattr(GameClient, "post_raw", fake_post_raw)
    return recorded


def make_energy_payload(stamina_amount: int) -> bytes:
    """构造 2008 GetAllItemRes 子包载荷（ItemClass: 1=itemSid, 2=itemID, 3=itemAmount）。"""
    item = encode_int(1, 1) + encode_int(2, 200000002) + encode_int(3, stamina_amount)
    return encode_message(1, item)


# ---------------------------------------------------------------------------
# 1. 签名与存证算法测试
# ---------------------------------------------------------------------------
class TestCryptoAndVerify:
    def test_make_fight_verify_token(self) -> None:
        """测试 11017 战斗结算 MD5 签名生成算法。"""
        fight_token = 12345678
        time_cost_sec = 20
        win_lose = 1
        expected_raw = f"{fight_token}{time_cost_sec}{win_lose}03120908"
        expected_md5 = hashlib.md5(expected_raw.encode("utf-8")).hexdigest().lower()

        token = make_fight_verify_token(fight_token, time_cost_sec, win_lose)
        assert token == expected_md5
        assert len(token) == 32
        assert token.islower()

        # 真机抓包「主线通关抓包.saz」（raw/102）字节级基准测试向量：
        # fightToken=120483, timeCostSec=17, winLose=1, salt="03120908"
        capture_token = make_fight_verify_token(120483, 17, 1)
        assert capture_token == "0ef8ecfcd972a606bcf04e91efe86bad"

    def test_make_battle_server_verify(self) -> None:
        """测试 cheatCheck=0 关卡的 battleServerVerify 存证生成格式。"""
        section_id = 102000004
        encoded = make_battle_server_verify(section_id)
        raw_json_bytes = gzip.decompress(base64.b64decode(encoded))
        data = json.loads(raw_json_bytes.decode("utf-8"))

        assert data["BattleType"] == 0
        assert data["SectionId"] == section_id
        assert data["Proofs"] == []

    def test_fighting_result_packet_wire_alignment_with_capture(self) -> None:
        """测试 11017 组包 wire format 严格对齐真机抓包 raw/102。

        验证重点：
        1. starCount 必须为 unpacked repeated varint (18 01 18 01 18 01)，不能是 packed 压缩。
        2. ComboRecord (08 00 10 00)、InterupRecord (08 01)、AwakeRecord (08 00) 非空必发。
        3. BattleResultClass.yimoLv 必须为 -1 补码 (28 ff ff ff ff ff ff ff ff ff 01)。
        """
        from models.main_stage import (
            BattleAchiAwakeClass,
            BattleAchiComboClass,
            BattleAchiInterupClass,
            BattleResultClass,
            FightDataClass,
            FightingResultPacket,
        )

        pkt = FightingResultPacket(
            fightData=FightDataClass(token=120483, sectionId=102000088),
            winLose=1,
            starCount=[1, 1, 1],
            timeCostSec=17,
            ComboRecord=BattleAchiComboClass(totalComboCount=0, killBossCount=0),
            InterupRecord=BattleAchiInterupClass(totalInterupCount=1),
            AwakeRecord=BattleAchiAwakeClass(totalInterupCount=0),
            battleResult=BattleResultClass(
                teamId=40,
                fightingPower=87756,
                damageContent="",
                damage=8053,
                yimoLv=-1,
                battleServerVerify="dGVzdA==",
            ),
            token="0ef8ecfcd972a606bcf04e91efe86bad",
        )
        encoded = pkt.encode(include_defaults=True)

        # 1. starCount 必须是独立的 3 个 varint (18 01 18 01 18 01)
        assert b"\x18\x01\x18\x01\x18\x01" in encoded
        # 且绝不能包含 packed 形式 (tag 3 wire 2 = 0x1a 03 01 01 01)
        assert b"\x1a\x03\x01\x01\x01" not in encoded

        # 2. 存证对象必发断言
        assert b"\x2a\x04\x08\x00\x10\x00" in encoded  # Tag 5 ComboRecord
        assert b"\x32\x02\x08\x01" in encoded          # Tag 6 InterupRecord
        assert b"\x3a\x02\x08\x00" in encoded          # Tag 7 AwakeRecord

        # 3. yimoLv = -1 (64位补码 10 字节)
        assert b"\x28\xff\xff\xff\xff\xff\xff\xff\xff\xff\x01" in encoded


# ---------------------------------------------------------------------------
# 2. 安全护栏与风控规则测试
# ---------------------------------------------------------------------------
class TestSecurityGuards:
    def test_zero_stamina_buying_guarantee(self) -> None:
        """断言本任务绝对不花钻石、BuyEnergyPacket/VIPDetailPacket 严格处于禁发黑名单。"""
        assert MainStageTask.spends_diamond is False
        assert "BuyEnergyPacket" in FORBIDDEN_PACKET_IDS
        assert FORBIDDEN_PACKET_IDS["BuyEnergyPacket"] == 24005
        assert "VIPDetailPacket" in FORBIDDEN_PACKET_IDS
        assert FORBIDDEN_PACKET_IDS["VIPDetailPacket"] == 24011

    def test_sensitive_stage_cheat_check_halts_immediately(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """遇 cheatCheck=1 敏感校验关卡：必须立即停止，不得发送任何战斗相关请求。"""
        session = make_test_session()
        # 1-1-1 (102000001) 是主线第 1 关，配置表中明确标注 cheatCheck=1
        sec_map, _, _ = load_main_stage_index()
        assert sec_map[102000001].is_cheat_check_required

        # 模拟 11011 返回玩家尚未通关任何关卡，当前首关即为 102000001
        def handler(pid: int, body: bytes) -> bytes:
            if pid == 2007:
                return envelope_bytes(2008, make_energy_payload(100))
            if pid == 11011:  # GetLastSectionPacket
                sub = encode_message(2, encode_int(1, 102000001) + encode_int(2, 0))
                return envelope_bytes(11012, sub)
            raise AssertionError(f"不应发出其他请求：pid={pid}")

        recorded = patch_transport(monkeypatch, handler)

        task = MainStageTask(session, delay_min=5.0, delay_max=5.0)
        result = task.run()

        assert result.ok is True
        assert "cheatCheck=1" in result.message
        assert "手动通关" in result.message
        assert result.data["cleared_count"] == 0

        # 校验发出的包：只发了 2007 查询体力和 11011 查询边界，绝没有 11015 开战包
        pids = [parse_envelope(b).packet_id for b in recorded]
        assert 11015 not in pids
        assert 11017 not in pids

    def test_insufficient_stamina_halts_cleanly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """体力不足时：平稳停机，绝不发送 11015 开战请求，更绝不买体力。"""
        session = make_test_session()

        # 模拟已通关 1-1（102000001~3），下一关为常规关卡 102000004（cheatCheck=0，体力耗费 6）
        def handler(pid: int, body: bytes) -> bytes:
            if pid == 2007:  # 体力只有 2 点（小于关卡所需的 6 点）
                return envelope_bytes(2008, make_energy_payload(2))
            if pid == 11011:
                p = encode_int(1, 102000003) + encode_int(2, 2)
                return envelope_bytes(11012, encode_message(2, p))
            raise AssertionError(f"不应发出其他请求：pid={pid}")

        recorded = patch_transport(monkeypatch, handler)

        task = MainStageTask(session, delay_min=5.0, delay_max=5.0)
        result = task.run()

        assert result.ok is True
        assert "体力不足" in result.message
        assert result.data["cleared_count"] == 0

        pids = [parse_envelope(b).packet_id for b in recorded]
        assert 11015 not in pids
        assert 24005 not in pids
        assert 24011 not in pids


# ---------------------------------------------------------------------------
# 3. 战斗推进与三星/宝箱领取流程测试
# ---------------------------------------------------------------------------
class TestProgressionAndRewards:
    def test_normal_stage_clearing_and_three_stars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """测试常规关卡推进流程：正确获得 fightToken、模拟延迟、上报三星结果。"""
        session = make_test_session()
        no_wait(monkeypatch)

        # 模拟玩家已过 1-1（102000003），下一关为 1-2-1（102000004，常规关）
        def handler(pid: int, body: bytes) -> bytes:
            if pid == 2007:  # 查询体力 100
                return envelope_bytes(2008, make_energy_payload(100))
            if pid == 11011:  # 边界查询，最高通关 102000003
                p = encode_int(1, 102000003) + encode_int(2, 2)
                return envelope_bytes(11012, encode_message(2, p))
            if pid == 5013:  # 设定出战队伍 40 (PVE1)
                env = parse_envelope(body)
                team_packet = SetUseTeamIDPacket.decode(env.sub_packet_bytes)
                assert team_packet.teamID == TEAM_TYPE_PVE1
                return envelope_bytes(5014, encode_int(1, 0))
            if pid == 11015:  # 进战申请：返回 errorCode=0, fightToken=88888
                env = parse_envelope(body)
                req_packet = FightingRequestPacket.decode(env.sub_packet_bytes)
                assert req_packet.teamID == TEAM_TYPE_PVE1
                sub = encode_int(1, 0) + encode_int(2, 88888)
                return envelope_bytes(11016, sub)
            if pid == 11017:  # 战斗结算：解析并断言 starCount=[1, 1, 1]
                env = parse_envelope(body)
                packet = FightingResultPacket.decode(env.sub_packet_bytes)
                assert packet.starCount == [1, 1, 1]
                assert packet.winLose == 1
                assert packet.fightData.token == 88888
                assert packet.fightData.sectionId == 102000004
                assert packet.battleResult.teamId == TEAM_TYPE_PVE1
                assert len(packet.token) == 32
                assert packet.token == make_fight_verify_token(88888, packet.timeCostSec, 1)
                # 返回成功结算与剩余体力 94
                cost_item = encode_int(1, 1) + encode_int(2, 200000002) + encode_int(3, 94)
                cost_obj = encode_message(1, cost_item)
                sub = encode_int(1, 0) + encode_message(3, cost_obj)
                return envelope_bytes(11018, sub)
            raise AssertionError(f"未预期的请求：pid={pid}")

        recorded = patch_transport(monkeypatch, handler)

        task = MainStageTask(session, max_stages=1, delay_min=5.0, delay_max=5.0)
        result = task.run()

        assert result.ok is True
        assert result.data["cleared_count"] == 1
        assert 102000004 in result.data["cleared_stages"]
        assert task.energy_left == 94

        pids = [parse_envelope(b).packet_id for b in recorded]
        assert pids == [2007, 11011, 5013, 11015, 11017]

    def test_area_completion_triggers_star_chest_claiming(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """小章节最后一关通关后，自动触发 11007 领取章节满星宝箱。"""
        session = make_test_session()
        no_wait(monkeypatch)

        chest_claimed_count = 0

        # 我们模拟攻打 1-2 的最后一关 102000009 (is_last_in_area=True)
        # 1-2 对应 areaID = 101000002
        def handler(pid: int, body: bytes) -> bytes:
            nonlocal chest_claimed_count
            if pid == 2007:
                return envelope_bytes(2008, make_energy_payload(100))
            if pid == 11011:
                # 模拟已通关至 102000008，下一关为 102000009
                p = encode_int(1, 102000008) + encode_int(2, 2)
                return envelope_bytes(11012, encode_message(2, p))
            if pid == 5013:
                return envelope_bytes(5014, encode_int(1, 0))
            if pid == 11015:
                return envelope_bytes(11016, encode_int(1, 0) + encode_int(2, 66666))
            if pid == 11017:
                return envelope_bytes(11018, encode_int(1, 0))
            if pid == 11007:  # 领小章满星宝箱
                env = parse_envelope(body)
                chest_packet = ReceiveStarRewardPacket.decode(env.sub_packet_bytes)
                assert chest_packet.areaID == 101000002
                chest_claimed_count += 1
                if chest_claimed_count <= 2:
                    reward_item = encode_int(1, 1) + encode_int(2, 200000001) + encode_int(3, 50)
                    reward_obj = encode_message(1, reward_item)
                    sub = encode_int(1, 0) + encode_message(2, reward_obj)
                    return envelope_bytes(11008, sub)
                else:
                    return envelope_bytes(11008, encode_int(1, 0))
            raise AssertionError(f"未预期的请求：pid={pid}")

        patch_transport(monkeypatch, handler)

        task = MainStageTask(session, max_stages=1, delay_min=5.0, delay_max=5.0)
        result = task.run()

        assert result.ok is True
        assert result.data["cleared_count"] == 1
        # ★ 2026-10-04「data 一并收敛」：逐条宝箱文本改为计数
        assert result.data["claimed_chest_count"] == 2

    def test_target_section_stop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """测试指定 target_section 达成后准时停机。"""
        session = make_test_session()
        no_wait(monkeypatch)

        # 玩家已过 102000003，目标为 102000004
        def handler(pid: int, body: bytes) -> bytes:
            if pid == 2007:
                return envelope_bytes(2008, make_energy_payload(100))
            if pid == 11011:
                p = encode_int(1, 102000003) + encode_int(2, 2)
                return envelope_bytes(11012, encode_message(2, p))
            if pid == 5013:
                return envelope_bytes(5014, encode_int(1, 0))
            if pid == 11015:
                return envelope_bytes(11016, encode_int(1, 0) + encode_int(2, 123))
            if pid == 11017:
                return envelope_bytes(11018, encode_int(1, 0))
            raise AssertionError(f"未预期的请求：pid={pid}")

        patch_transport(monkeypatch, handler)

        # 设定目标关卡为 102000004
        task = MainStageTask(session, target_section=102000004, delay_min=5.0, delay_max=5.0)
        result = task.run()

        assert result.ok is True
        assert result.data["cleared_count"] == 1
        assert "达成目标关卡" in result.data["stop_reason"]

    def test_already_cleared_target_returns_early(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """目标关卡若已经通关，直接返回无需推进提示。"""
        session = make_test_session()

        def handler(pid: int, body: bytes) -> bytes:
            if pid == 2007:
                return envelope_bytes(2008, make_energy_payload(100))
            if pid == 11011:
                # 玩家已通关至 102000005
                p = encode_int(1, 102000005) + encode_int(2, 2)
                return envelope_bytes(11012, encode_message(2, p))
            raise AssertionError(f"未预期的请求：pid={pid}")

        patch_transport(monkeypatch, handler)

        task = MainStageTask(session, target_section=102000004)
        result = task.run()

        assert result.ok is True
        assert "已经通关，无需重复推进" in result.message
        assert result.data["cleared_stages"] == []


# ---------------------------------------------------------------------------
# 4. 演练模式（Dry-Run）测试
# ---------------------------------------------------------------------------
class TestDryRun:
    def test_dry_run_success_and_packet_integrity(self) -> None:
        """演练模式下不产生任何网络请求，正确输出关卡统计与报文大小。"""
        session = make_test_session()
        task = MainStageTask(session, dry_run=True)
        result = task.run()

        assert result.ok is True
        assert "演练完成" in result.message
        assert result.data["total_stages"] == 537
        assert result.data["cheat_check_stages"] == 23
        assert result.data["normal_stages"] == 514
        assert result.data["sample_verify_len"] > 0


# ---------------------------------------------------------------------------
# 5. 主线进度只读查询任务测试
# ---------------------------------------------------------------------------
class TestMainStageStatusTask:
    def test_main_stage_status_fetch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """测试 main_stage_status 任务只读拉取 11011 解析最高通关关卡。"""
        from tasks.main_stage import MainStageStatusTask

        session = make_test_session()

        def handler(pid: int, body: bytes) -> bytes:
            if pid == 11011:
                p = encode_int(1, 102000009) + encode_int(2, 2)
                return envelope_bytes(11012, encode_message(2, p))
            raise AssertionError(f"只读任务不应发起其它包：pid={pid}")

        patch_transport(monkeypatch, handler)

        task = MainStageStatusTask(session)
        result = task.run()

        assert result.ok is True
        assert "当前已通关最高主线" in result.message
        assert result.data["highest_passed_id"] == 102000009
        assert "1-2" in result.data["highest_passed_desc"]
        assert result.data["next_section_id"] == 102000010
        assert result.data["is_all_cleared"] is False

    def test_main_stage_status_dry_run(self) -> None:
        """测试 main_stage_status 演练模式。"""
        from tasks.main_stage import MainStageStatusTask

        session = make_test_session()
        task = MainStageStatusTask(session, dry_run=True)
        result = task.run()

        assert result.ok is True
        assert "演练" in result.message
        assert result.data["next_section_id"] == 102000001

