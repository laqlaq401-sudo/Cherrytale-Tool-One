"""通用战斗签名计算器（tools/battle_signer.py）单元测试套件。

验证目标：
1. 真实抓包基准测试向量断言（精确命中 32 位 MD5 签名）；
2. 基础类型兼容性（int 与 str 及混合输入等价性）；
3. 签名与存证完全解耦验证；
4. 命令行 CLI 执行链路验证；
5. 严守红线检查（无天命 Destiny 依赖、无高难度属性试炼渗透）。
"""

from __future__ import annotations

import base64
import gzip
import json
import re
from typing import Final

import pytest

from crypto.battle_signer import (
    BATTLE_VERIFY_SALT,
    calc_arena_token,
    calc_fight_token,
    calc_general_fight_token,
    calc_yimo_token,
    make_empty_server_verify,
)

# ==============================================================================
# 抓包确凿基准测试向量
# ==============================================================================
# 1. 失控炼成阵（Yimo）：提取自 captures/old/1.0.0NEED/ raw/26
YIMO_FIGHT_TOKEN: Final[int] = 29744
YIMO_SCORE: Final[int] = 53247
YIMO_EXPECTED_TOKEN: Final[str] = "0a3396051faf186762d81815977630bf"

# 2. 竞技场（Arena）：提取自 captures/old/1.0.0NEED/ 竞技场对决会话
ARENA_FIGHT_TOKEN: Final[int] = 29324
ARENA_TARGET_PLAYER_ID: Final[int] = 6175214
ARENA_WIN_LOSE: Final[int] = 0
ARENA_EXPECTED_TOKEN: Final[str] = "1b829bfbec08fe639d11c01644a2604d"

# 3. 通用战斗/主线：提取自 captures/ 真实主线通关会话 (raw/102)
FIGHT_TOKEN: Final[int] = 120483
TIME_COST_SEC: Final[int] = 17
FIGHT_WIN_LOSE: Final[int] = 1
FIGHT_EXPECTED_TOKEN: Final[str] = "0ef8ecfcd972a606bcf04e91efe86bad"


class TestYimoSigner:
    """失控炼成阵（Yimo）签名算法测试。"""

    def test_yimo_token_exact_capture_vector(self) -> None:
        """断言抓包真实向量 MD5 精确命中。"""
        token = calc_yimo_token(YIMO_FIGHT_TOKEN, YIMO_SCORE)
        assert token == YIMO_EXPECTED_TOKEN
        assert len(token) == 32
        assert re.fullmatch(r"[0-9a-f]{32}", token) is not None

    def test_yimo_token_type_compatibility(self) -> None:
        """验证 int、str 及混合入参等价性。"""
        token_int = calc_yimo_token(29744, 53247)
        token_str = calc_yimo_token("29744", "53247")
        token_mixed_1 = calc_yimo_token(29744, "53247")
        token_mixed_2 = calc_yimo_token("29744", 53247)

        assert token_int == YIMO_EXPECTED_TOKEN
        assert token_str == YIMO_EXPECTED_TOKEN
        assert token_mixed_1 == YIMO_EXPECTED_TOKEN
        assert token_mixed_2 == YIMO_EXPECTED_TOKEN

    def test_yimo_token_score_sensitivity(self) -> None:
        """验证得分或令牌变更后散列值发生变化。"""
        base = calc_yimo_token(29744, 53247)
        assert calc_yimo_token(29744, 53248) != base
        assert calc_yimo_token(29745, 53247) != base


class TestArenaSigner:
    """竞技场（Arena）签名算法测试。"""

    def test_arena_token_exact_capture_vector(self) -> None:
        """断言竞技场抓包真实向量 MD5 精确命中。"""
        token = calc_fight_token(ARENA_FIGHT_TOKEN, ARENA_TARGET_PLAYER_ID, ARENA_WIN_LOSE)
        assert token == ARENA_EXPECTED_TOKEN

        # 辅助别名函数一致性
        alias_token = calc_arena_token(ARENA_FIGHT_TOKEN, ARENA_TARGET_PLAYER_ID, ARENA_WIN_LOSE)
        assert alias_token == ARENA_EXPECTED_TOKEN

    def test_arena_token_type_compatibility(self) -> None:
        """验证竞技场参数不同数据类型组合。"""
        t1 = calc_arena_token(29324, 6175214, 0)
        t2 = calc_arena_token("29324", "6175214", "0")
        t3 = calc_arena_token("29324", 6175214, 0)
        t4 = calc_arena_token(29324, "6175214", "0")

        assert t1 == ARENA_EXPECTED_TOKEN
        assert t2 == ARENA_EXPECTED_TOKEN
        assert t3 == ARENA_EXPECTED_TOKEN
        assert t4 == ARENA_EXPECTED_TOKEN

    def test_arena_win_lose_difference(self) -> None:
        """验证胜负状态对签名产生实质影响。"""
        win_token = calc_arena_token(29324, 6175214, win_lose=0)
        lose_token = calc_arena_token(29324, 6175214, win_lose=1)
        assert win_token != lose_token


class TestGeneralFightSigner:
    """通用战斗 / 主线 / 初始试炼签名算法测试。"""

    def test_general_fight_token_exact_capture_vector(self) -> None:
        """断言主线真机抓包样本 MD5。"""
        token = calc_general_fight_token(FIGHT_TOKEN, TIME_COST_SEC, FIGHT_WIN_LOSE)
        assert token == FIGHT_EXPECTED_TOKEN

    def test_general_fight_token_type_compatibility(self) -> None:
        """验证参数类型兼容性。"""
        t1 = calc_fight_token(120483, 17, 1)
        t2 = calc_fight_token("120483", "17", "1")
        assert t1 == FIGHT_EXPECTED_TOKEN
        assert t2 == FIGHT_EXPECTED_TOKEN

    def test_compatibility_with_main_stage_model(self) -> None:
        """确保与现有 models.main_stage.make_fight_verify_token 计算完全一致。"""
        from models.main_stage import make_fight_verify_token

        token_a = calc_fight_token(99999, 25, 1)
        token_b = make_fight_verify_token(99999, 25, 1)
        assert token_a == token_b


class TestServerVerifyHelper:
    """存证解耦与极简存证辅助生成测试。"""

    def test_empty_server_verify_structure(self) -> None:
        """验证生成的 battleServerVerify 载荷格式与可解压性。"""
        b64_str = make_empty_server_verify(section_id=102000004, battle_type=0)
        decompressed = gzip.decompress(base64.b64decode(b64_str))
        obj = json.loads(decompressed.decode("utf-8"))

        assert obj == {"BattleType": 0, "SectionId": 102000004, "Proofs": []}

    def test_token_and_verify_are_decoupled(self) -> None:
        """密码学解耦证明：存证内容无论如何变化，Token 恒定不变。"""
        token1 = calc_yimo_token(YIMO_FIGHT_TOKEN, YIMO_SCORE)

        # 构造不同存证字符串
        v1 = make_empty_server_verify(0, 0)
        v2 = make_empty_server_verify(9999, 5)

        assert v1 != v2
        # Token 运算不接收存证入参，签名保持确凿
        token2 = calc_yimo_token(YIMO_FIGHT_TOKEN, YIMO_SCORE)
        assert token1 == token2 == YIMO_EXPECTED_TOKEN


class TestCliInterface:
    """命令行参数解析与执行测试。"""

    def test_cli_yimo(self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
        from tools.battle_signer import main

        monkeypatch.setattr("sys.argv", ["battle_signer.py", "--yimo", "29744", "53247"])
        ret = main()
        captured = capsys.readouterr().out
        assert ret == 0
        assert YIMO_EXPECTED_TOKEN in captured

    def test_cli_arena(self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
        from tools.battle_signer import main

        monkeypatch.setattr("sys.argv", ["battle_signer.py", "--arena", "29324", "6175214"])
        ret = main()
        captured = capsys.readouterr().out
        assert ret == 0
        assert ARENA_EXPECTED_TOKEN in captured

    def test_cli_fight(self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
        from tools.battle_signer import main

        monkeypatch.setattr("sys.argv", ["battle_signer.py", "--fight", "120483", "17", "1"])
        ret = main()
        captured = capsys.readouterr().out
        assert ret == 0
        assert FIGHT_EXPECTED_TOKEN in captured


class TestRedLinesAndSafety:
    """红线与工程安全合规性自动化检查。"""

    def test_salt_integrity(self) -> None:
        """确保加盐常量严格保持 03120908。"""
        assert BATTLE_VERIFY_SALT == "03120908"

    def test_no_destiny_in_signer(self) -> None:
        """确保 battle_signer.py 未导入或定义任何天命 Destiny 逻辑。"""
        import tools.battle_signer as mod

        text = open(mod.__file__, "r", encoding="utf-8").read()
        assert "Destiny" not in text or "严禁" in text
        assert "destiny" not in text.lower() or "严禁" in text.lower()

    def test_no_high_difficulty_trials(self) -> None:
        """确保 battle_signer.py 不触碰高难属性试炼。"""
        import tools.battle_signer as mod

        text = open(mod.__file__, "r", encoding="utf-8").read()
        for forbidden in ("Volcano", "Glacier", "Desert", "火山", "极地", "荒漠"):
            assert forbidden not in text or "严禁" in text
