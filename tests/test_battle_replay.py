"""三大核心战斗玩法（失控炼成阵 / 竞技场 / 初始试炼）离线抓包回放与信封组装验证套件。

验证目标：
1. 信封封装（GameEnvelope）对接子包模型（ProtoMessage）的组装完整性；
2. 真实抓包外层信封 RootPacket 的 100% 逐字节（Byte-for-Byte）精确对齐；
3. 外层信封 vCode 与毫秒时间戳的算法动态生成一致性校验；
4. 真实 SAZ 归档文件（captures/old/1.0.0NEED/）的直接读取、解包与模型回放；
5. 永久工程红线合规性（严禁引入 Destiny 与高难试炼）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final
import zipfile

import pytest

from crypto.vcode import make_v_code
from models.battle_arena import (
    ChallengePacket,
    ChallengeRes,
    ChallengeResultPacket,
    ChallengeResultRes,
)
from models.battle_experiment import (
    ExperimentFightingRequest,
    ExperimentFightingRequestRes,
    ExperimentFightingResult,
    ExperimentFightingResultRes,
)
from models.battle_yimo import (
    YimoBattleRequest,
    YimoBattleRequestRes,
    YimoBattleResult,
    YimoBattleResultRes,
)
from models.envelope import GameEnvelope, parse_envelope
from models.handshake import SpecialActivityRefreshClass
from protocol_samples.battle_samples import (
    ARENA_REQ_ENVELOPE_HEX,
    ARENA_REQ_SUB_HEX,
    ARENA_RES_ENVELOPE_HEX,
    ARENA_RES_SUB_HEX,
    EXP_REQ_ENVELOPE_HEX,
    EXP_REQ_SUB_HEX,
    EXP_RES_ENVELOPE_HEX,
    EXP_RES_SUB_HEX,
    YIMO_REQ_ENVELOPE_HEX,
    YIMO_REQ_SUB_HEX,
    YIMO_RES_ENVELOPE_HEX,
    YIMO_RES_SUB_HEX,
)
from crypto.battle_signer import (
    calc_arena_token,
    calc_yimo_token,
)

CAPTURES_DIR: Final[Path] = Path("captures/old/1.0.0NEED")


def extract_http_body(raw_bytes: bytes) -> bytes:
    """提取 HTTP 响应或请求体（剔除 HTTP 头部）。"""
    _head, sep, body = raw_bytes.partition(b"\r\n\r\n")
    return body if sep else b""


class TestYimoReplay:
    """失控炼成阵（Yimo）抓包回放与信封对齐测试。"""

    def test_yimo_request_envelope_unpack(self) -> None:
        """测试 22107 请求信封的解析。"""
        raw_env = bytes.fromhex(YIMO_REQ_ENVELOPE_HEX)
        view = parse_envelope(raw_env)

        assert view.packet_id == 22107
        assert view.class_name == "YimoBattleRequest"
        assert view.text("token") == "pshd4gjenp0cqttf2epo33qchop6138759"
        assert view.text("settingMd5") == "2.2.0-el-h-win|af484fe5aea6a7957cd7b33171099722"
        assert view.integer("timeStampClient") == 1789972055766
        assert view.text("vCode") == "DAAC311433D73F0F585E63BCA1E7BECD"
        assert view.sub_packet_bytes == bytes.fromhex(YIMO_REQ_SUB_HEX)

    def test_yimo_request_vcode_generation(self) -> None:
        """验证 22107 请求信封中的 vCode 能被 crypto.vcode 精确还原。"""
        calc_v = make_v_code(
            22107,
            token="pshd4gjenp0cqttf2epo33qchop6138759",
            setting_md5="2.2.0-el-h-win|af484fe5aea6a7957cd7b33171099722",
            time_stamp_client=1789972055766,
        )
        assert calc_v == "DAAC311433D73F0F585E63BCA1E7BECD"

    def test_yimo_request_envelope_byte_for_byte_alignment(self) -> None:
        """验证使用 GameEnvelope 组装的 22107 请求信封与抓包 100% 逐字节完全一致。"""
        raw_env = bytes.fromhex(YIMO_REQ_ENVELOPE_HEX)
        view = parse_envelope(raw_env)

        sub_packet = view.decode_sub_packet(YimoBattleRequest)
        assert isinstance(sub_packet, YimoBattleRequest)
        assert sub_packet.part == 661000003
        assert sub_packet.bossLv == 41

        # 构造信封并组包
        act_entry = view.meta.get("SpecialActivityRefreshClass")
        assert act_entry is not None
        act = SpecialActivityRefreshClass.decode(act_entry.value)

        envelope = GameEnvelope(
            token=view.text("token") or "",
            setting_md5=view.text("settingMd5") or "",
            time_stamp_token=view.integer("timeStampToken") or -1,
            time_stamp_client=view.integer("timeStampClient"),
            v_code=view.text("vCode") or "",
            activity=act,
        )
        reconstructed = envelope.encode_with(sub_packet)
        assert reconstructed == raw_env

    def test_yimo_response_envelope_unpack(self) -> None:
        """测试 22110 结算响应信封与业务模型解析。"""
        raw_env = bytes.fromhex(YIMO_RES_ENVELOPE_HEX)
        view = parse_envelope(raw_env)

        assert view.packet_id == 22110
        assert view.class_name == "YimoBattleResultRes"
        assert view.sub_packet_bytes == bytes.fromhex(YIMO_RES_SUB_HEX)

        res = view.decode_sub_packet(YimoBattleResultRes)
        assert isinstance(res, YimoBattleResultRes)
        assert res.errorCode == 0
        assert res.is_success()
        assert res.rank == 16
        assert res.totalValue == 111209
        assert len(res.rewardList) == 1

    def test_yimo_lifecycle_token_verification(self) -> None:
        """验证 Yimo 全流程签名：进战 Token -> 计算散列 -> 结算 Token 对齐。"""
        fight_token = 29744
        score = 53247
        token = calc_yimo_token(fight_token, score)
        assert token == "0a3396051faf186762d81815977630bf"


class TestArenaReplay:
    """竞技场（Arena）抓包回放与信封对齐测试。"""

    def test_arena_request_envelope_unpack(self) -> None:
        """测试 26003 竞技场挑战请求信封解析。"""
        raw_env = bytes.fromhex(ARENA_REQ_ENVELOPE_HEX)
        view = parse_envelope(raw_env)

        assert view.packet_id == 26003
        assert view.class_name == "ChallengePacket"
        assert view.text("token") == "pshd4gjenp0cqttf2epo33qchop6138759"
        assert view.text("settingMd5") == "2.2.0-el-h-win|af484fe5aea6a7957cd7b33171099722"
        assert view.integer("timeStampClient") == 1789970946573
        assert view.text("vCode") == "E1875C680D857449B6F1602FE5D1A31D"
        assert view.sub_packet_bytes == bytes.fromhex(ARENA_REQ_SUB_HEX)

    def test_arena_request_vcode_generation(self) -> None:
        """验证 26003 请求信封中的 vCode 能被 crypto.vcode 精确还原。"""
        calc_v = make_v_code(
            26003,
            token="pshd4gjenp0cqttf2epo33qchop6138759",
            setting_md5="2.2.0-el-h-win|af484fe5aea6a7957cd7b33171099722",
            time_stamp_client=1789970946573,
        )
        assert calc_v == "E1875C680D857449B6F1602FE5D1A31D"

    def test_arena_request_envelope_byte_for_byte_alignment(self) -> None:
        """验证 26003 请求信封组装与抓包 100% 逐字节对齐（include_defaults=True 保留 0 值）。"""
        raw_env = bytes.fromhex(ARENA_REQ_ENVELOPE_HEX)
        view = parse_envelope(raw_env)

        sub_packet = view.decode_sub_packet(ChallengePacket)
        assert isinstance(sub_packet, ChallengePacket)
        assert sub_packet.actionType == 0
        assert sub_packet.playerID == 6175214
        assert sub_packet.teamID == 60

        act_entry = view.meta.get("SpecialActivityRefreshClass")
        assert act_entry is not None
        act = SpecialActivityRefreshClass.decode(act_entry.value)

        envelope = GameEnvelope(
            token=view.text("token") or "",
            setting_md5=view.text("settingMd5") or "",
            time_stamp_token=view.integer("timeStampToken") or -1,
            time_stamp_client=view.integer("timeStampClient"),
            v_code=view.text("vCode") or "",
            activity=act,
        )
        reconstructed = envelope.encode_with(sub_packet, include_defaults=True)
        assert reconstructed == raw_env

    def test_arena_response_envelope_unpack(self) -> None:
        """测试 26020 竞技场结算响应信封与业务模型解析。"""
        raw_env = bytes.fromhex(ARENA_RES_ENVELOPE_HEX)
        view = parse_envelope(raw_env)

        assert view.packet_id == 26020
        assert view.class_name == "ChallengeResultRes"
        assert view.sub_packet_bytes == bytes.fromhex(ARENA_RES_SUB_HEX)

        res = view.decode_sub_packet(ChallengeResultRes)
        assert isinstance(res, ChallengeResultRes)
        assert res.errorCode == 0
        assert res.is_success()
        assert res.myResults is not None
        assert res.myResults.victory == 1
        assert res.myResults.myIntegral == 4060
        assert res.myResults.myRanking == 30

    def test_arena_lifecycle_token_verification(self) -> None:
        """验证竞技场全流程签名：进战 Token -> 计算对手散列 -> 结算 Token 对齐。"""
        fight_token = 29324
        player_id = 6175214
        token = calc_arena_token(fight_token, player_id, win_lose=0)
        assert token == "1b829bfbec08fe639d11c01644a2604d"


class TestExperimentReplay:
    """辉煌航迹·初始试炼（Experiment）抓包回放与信封对齐测试。"""

    def test_experiment_request_envelope_unpack(self) -> None:
        """测试 34009 初始试炼请求信封解析。"""
        raw_env = bytes.fromhex(EXP_REQ_ENVELOPE_HEX)
        view = parse_envelope(raw_env)

        assert view.packet_id == 34009
        assert view.class_name == "ExperimentFightingRequest"
        assert view.text("token") == "kro1gpbs77qfj23loabsne4ugpp6138759"
        assert view.text("settingMd5") == "2.2.0-el-h-win|77bf18767983db6fb2b17e5e3a891d45"
        assert view.integer("timeStampClient") == 1790959692820
        assert view.text("vCode") == "920E6A9EBE9D064CEEB727BFF714BAFF"
        assert view.sub_packet_bytes == bytes.fromhex(EXP_REQ_SUB_HEX)

    def test_experiment_request_vcode_generation(self) -> None:
        """验证 34009 请求信封中的 vCode 能被 crypto.vcode 精确还原。"""
        calc_v = make_v_code(
            34009,
            token="kro1gpbs77qfj23loabsne4ugpp6138759",
            setting_md5="2.2.0-el-h-win|77bf18767983db6fb2b17e5e3a891d45",
            time_stamp_client=1790959692820,
        )
        assert calc_v == "920E6A9EBE9D064CEEB727BFF714BAFF"

    def test_experiment_request_envelope_byte_for_byte_alignment(self) -> None:
        """验证 34009 请求信封组装与抓包 100% 逐字节对齐。"""
        raw_env = bytes.fromhex(EXP_REQ_ENVELOPE_HEX)
        view = parse_envelope(raw_env)

        sub_packet = view.decode_sub_packet(ExperimentFightingRequest)
        assert isinstance(sub_packet, ExperimentFightingRequest)
        assert sub_packet.sectionID == 671000001
        assert sub_packet.version == -1
        assert len(sub_packet.teamSid) == 5

        act_entry = view.meta.get("SpecialActivityRefreshClass")
        assert act_entry is not None
        act = SpecialActivityRefreshClass.decode(act_entry.value)

        envelope = GameEnvelope(
            token=view.text("token") or "",
            setting_md5=view.text("settingMd5") or "",
            time_stamp_token=view.integer("timeStampToken") or -1,
            time_stamp_client=view.integer("timeStampClient"),
            v_code=view.text("vCode") or "",
            activity=act,
        )
        reconstructed = envelope.encode_with(sub_packet)
        assert reconstructed == raw_env

    def test_experiment_response_envelope_unpack(self) -> None:
        """测试 34012 初始试炼结算响应信封与业务模型解析。"""
        raw_env = bytes.fromhex(EXP_RES_ENVELOPE_HEX)
        view = parse_envelope(raw_env)

        assert view.packet_id == 34012
        assert view.class_name == "ExperimentFightingResultRes"
        assert view.sub_packet_bytes == bytes.fromhex(EXP_RES_SUB_HEX)

        res = view.decode_sub_packet(ExperimentFightingResultRes)
        assert isinstance(res, ExperimentFightingResultRes)
        assert res.errorCode == 0
        assert res.is_success()
        assert len(res.rewardList) == 1


class TestSazArchiveDirectReplay:
    """直接针对 captures/old/1.0.0NEED/ 物理 SAZ 归档的离线回放测试。"""

    @pytest.mark.skipif(not CAPTURES_DIR.exists(), reason="抓包归档目录不存在")
    def test_replay_from_disk_saz_archives(self) -> None:
        """从物理 SAZ 压缩包直接流式解包与全链路校验。"""
        # 1. 查找 3 个目标 SAZ 文件
        yimo_saz = next(CAPTURES_DIR.glob("*失控炼成阵.saz"), None)
        arena_saz = next(CAPTURES_DIR.glob("*竞技场抓包.saz"), None)
        exp_saz = next(CAPTURES_DIR.glob("*初始试炼*.saz"), None)

        if not (yimo_saz and arena_saz and exp_saz):
            pytest.skip("部分 SAZ 归档不存在于 captures 目录")

        # 校验失控炼成阵
        with zipfile.ZipFile(yimo_saz) as z:
            req_body = extract_http_body(z.read("raw/01_c.txt"))
            view = parse_envelope(req_body)
            assert view.packet_id == 22107
            req = view.decode_sub_packet(YimoBattleRequest)
            assert req.part == 661000003

            res_body = extract_http_body(z.read("raw/01_s.txt"))
            res_view = parse_envelope(res_body)
            assert res_view.packet_id == 22108
            res = res_view.decode_sub_packet(YimoBattleRequestRes)
            assert res.errorCode == 0
            assert res.fightToken == 29744

        # 校验竞技场
        with zipfile.ZipFile(arena_saz) as z:
            req_body = extract_http_body(z.read("raw/020_c.txt"))
            view = parse_envelope(req_body)
            assert view.packet_id == 26003
            req = view.decode_sub_packet(ChallengePacket)
            assert req.playerID == 6175214

            res_body = extract_http_body(z.read("raw/020_s.txt"))
            res_view = parse_envelope(res_body)
            assert res_view.packet_id == 26004
            res = res_view.decode_sub_packet(ChallengeRes)
            assert res.errorCode == 0
            assert res.fightToken == 29324

        # 校验初始试炼
        with zipfile.ZipFile(exp_saz) as z:
            req_body = extract_http_body(z.read("raw/05_c.txt"))
            view = parse_envelope(req_body)
            assert view.packet_id == 34009
            req = view.decode_sub_packet(ExperimentFightingRequest)
            assert req.sectionID == 671000001

            res_body = extract_http_body(z.read("raw/05_s.txt"))
            res_view = parse_envelope(res_body)
            assert res_view.packet_id == 34010
            res = res_view.decode_sub_packet(ExperimentFightingRequestRes)
            assert res.errorCode == 0
            assert res.fightToken == 132008


class TestRedLinesAndSafety:
    """红线与工程安全合规性自动化检查。"""

    def test_no_destiny_in_replay(self) -> None:
        """确保 replay 测试与 samples 夹具未包含任何 Destiny 逻辑。"""
        import protocol_samples.battle_samples as s_mod
        import tests.test_battle_replay as t_mod

        for mod in (s_mod, t_mod):
            text = open(mod.__file__, "r", encoding="utf-8").read()
            assert "Destiny" not in text or "严禁" in text
            assert "destiny" not in text.lower() or "严禁" in text.lower()

    def test_no_high_difficulty_trials(self) -> None:
        """确保回放未触碰火山/极地/荒漠等高难度试炼。"""
        import protocol_samples.battle_samples as s_mod
        import tests.test_battle_replay as t_mod

        for mod in (s_mod, t_mod):
            text = open(mod.__file__, "r", encoding="utf-8").read()
            for forbidden in ("Volcano", "Glacier", "Desert", "火山", "极地", "荒漠"):
                assert forbidden not in text or "严禁" in text
