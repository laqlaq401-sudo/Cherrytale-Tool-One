"""竞技板块三个**只读**模式（竞技场 / 天命对决 / 失控炼成阵）的模型层测试。

运行方式：

    python -m pytest tests/test_models_competition.py -q
    python -m tests.test_models_competition

【本文件钉死的三件事】

1. **字段编号 = dump.cs 的声明顺序**（``challengeCount`` / ``challenge_times`` /
   ``remainCount`` 这几个"次数"字段错位，就会把别的数字当成次数汇报出去）；
2. **空请求真的编成 0 字节**，而 ``WuDouHome`` 的 ``groupID=-1`` 与抓包字节**逐字节一致**；
3. **白名单与禁发清单互不相交**，且禁发清单里确实包含"花钻石买次数"那几个包。
"""

from __future__ import annotations

import pytest

from models.packet_ids import packet_id
from models.proto_fields import field_tags
from models.protobuf_wire import encode_int, encode_string
from models.pvp import (
    ALLOWED_PACKET_IDS as PVP_ALLOWED,
    FORBIDDEN_PACKET_IDS as PVP_FORBIDDEN,
    PVPHomePacket,
    PVPHomeRes,
)
from models.wudou import (
    ALLOWED_PACKET_IDS as WUDOU_ALLOWED,
    FORBIDDEN_PACKET_IDS as WUDOU_FORBIDDEN,
    WuDouChallengeHome,
    WuDouChallengeHomeRes,
    WuDouHome,
    WuDouHomeRes,
)
from models.yimo import (
    ALLOWED_PACKET_IDS as YIMO_ALLOWED,
    FORBIDDEN_PACKET_IDS as YIMO_FORBIDDEN,
    GetYimoState,
    GetYimoStateRes,
)


# ---------------------------------------------------------------------------
# ① 字段编号
# ---------------------------------------------------------------------------
class TestFieldNumbers:
    def test_pvp_home(self) -> None:
        assert field_tags("PVPHomePacket") == {}
        tags = field_tags("PVPHomeRes")
        assert tags["pvpList"] == 1
        # ★ 可挑战次数
        assert tags["challengeCount"] == 4
        assert tags["buyedCount"] == 5
        assert tags["allianceIcon"] == 9
        assert len(tags) == 9

    def test_wudou_home(self) -> None:
        assert field_tags("WuDouHome") == {"groupID": 1}
        tags = field_tags("WuDouHomeRes")
        assert tags["weekRanking"] == 1
        assert tags["myGroupID"] == 5
        # ★ 剩余挑战次数
        assert tags["challenge_times"] == 6
        assert len(tags) == 6

    def test_wudou_challenge_home(self) -> None:
        assert field_tags("WuDouChallengeHome") == {"openTest": 1}
        tags = field_tags("WuDouChallengeHomeRes")
        assert tags["myRanking"] == 1
        assert tags["npc_times"] == 5
        # ★ 剩余挑战次数
        assert tags["challenge_times"] == 6
        assert tags["settlement_time"] == 8
        assert len(tags) == 8

    def test_yimo_state(self) -> None:
        assert field_tags("GetYimoState") == {}
        tags = field_tags("GetYimoStateRes")
        # ★ 剩余次数
        assert tags["remainCount"] == 1
        assert tags["nowScore"] == 3
        assert tags["buyExtraCount"] == 4
        assert tags["bossData"] == 6
        assert len(tags) == 9


# ---------------------------------------------------------------------------
# ② 编码
# ---------------------------------------------------------------------------
class TestEncoding:
    def test_empty_requests(self) -> None:
        assert PVPHomePacket().encode() == b""
        assert GetYimoState().encode() == b""
        # 26033 的 ``openTest`` 是**带 ``[DefaultValue]`` 的字符串**：真实客户端不发它
        # （抓包里该子包就是空的），所以按 ``include_defaults=False`` 编码应当是 0 字节。
        # ⚠️ 这正是"不能对所有包一律 include_defaults=True"的原因：
        # 客户端只在字段**没有** DefaultValue 时才发默认值（例如 39011 的 actionType=0）。
        assert WuDouChallengeHome().encode(include_defaults=False) == b""

    def test_wudou_group_id_minus_one_matches_capture(self) -> None:
        """实测抓包里 ``26031`` 的子包就是 ``08 FF…01``（= ``groupID = -1``）。"""
        encoded = WuDouHome.for_default_group().encode()
        assert encoded == bytes.fromhex("08ffffffffffffffffff01")
        assert WuDouHome.decode(encoded).groupID == -1


# ---------------------------------------------------------------------------
# ③ 解码与摘要
# ---------------------------------------------------------------------------
class TestDecoding:
    def test_pvp_home_res(self) -> None:
        payload = encode_int(4, 3) + encode_int(5, 1) + encode_int(6, 0)
        res = PVPHomeRes.decode(payload)
        assert res.challengeCount == 3
        assert res.buyedCount == 1
        assert res.opponent_count() == 0
        assert "可挑战次数=3" in res.summary()

    def test_wudou_challenge_home_res(self) -> None:
        payload = (
            encode_int(1, 149) + encode_int(5, 2) + encode_int(6, 5) + encode_int(7, 0)
        )
        res = WuDouChallengeHomeRes.decode(payload)
        assert (res.myRanking, res.npc_times, res.challenge_times) == (149, 2, 5)
        assert "剩余挑战次数=5" in res.summary()

    def test_yimo_state_res(self) -> None:
        payload = encode_int(1, 4) + encode_int(3, 53247) + encode_string(7, "活動中")
        res = GetYimoStateRes.decode(payload)
        assert res.remainCount == 4
        assert res.has_boss_data() is False
        assert "剩余次数=4" in res.summary()
        assert "活動中" in res.summary()

    def test_wudou_home_res(self) -> None:
        res = WuDouHomeRes.decode(encode_int(5, 7) + encode_int(6, 9))
        assert (res.myGroupID, res.challenge_times) == (7, 9)
        assert "剩余挑战次数=9" in res.summary()


# ---------------------------------------------------------------------------
# ④ 白名单 / 禁发清单（"绝不进战、绝不买次数"）
# ---------------------------------------------------------------------------
class TestWhitelist:
    @pytest.mark.parametrize(
        ("allowed", "forbidden"),
        [
            (PVP_ALLOWED, PVP_FORBIDDEN),
            (WUDOU_ALLOWED, WUDOU_FORBIDDEN),
            (YIMO_ALLOWED, YIMO_FORBIDDEN),
        ],
    )
    def test_no_overlap(self, allowed: dict[str, int], forbidden: dict[str, int]) -> None:
        """白名单与禁发清单若有交集，说明某处写错了 —— 那是"会花钱/会进战"的隐患。"""
        assert set(allowed) & set(forbidden) == set()

    @pytest.mark.parametrize(
        ("allowed", "forbidden"),
        [
            (PVP_ALLOWED, PVP_FORBIDDEN),
            (WUDOU_ALLOWED, WUDOU_FORBIDDEN),
            (YIMO_ALLOWED, YIMO_FORBIDDEN),
        ],
    )
    def test_ids_match_generated_table(
        self, allowed: dict[str, int], forbidden: dict[str, int]
    ) -> None:
        """两个表里的类名都必须真的存在于消息号表，且号码一致。"""
        for name, number in {**allowed, **forbidden}.items():
            assert packet_id(name) == number

    def test_buy_packets_are_forbidden(self) -> None:
        """三个"花钻石买次数"的包都必须在禁发清单里。"""
        assert PVP_FORBIDDEN["BuyChallengePacket"] == 26015
        assert WUDOU_FORBIDDEN["WuDouBuyChallenge"] == 26047
        assert YIMO_FORBIDDEN["BuyYimoCountPacket"] == 22103

    def test_battle_packets_are_forbidden(self) -> None:
        """进战/结算的包也必须在禁发清单里（它们会扣次数或需要战斗引擎）。"""
        assert PVP_FORBIDDEN["ChallengePacket"] == 26003
        assert WUDOU_FORBIDDEN["WuDouChallengeReady"] == 26037
        assert YIMO_FORBIDDEN["YimoBattleRequest"] == 22107


if __name__ == "__main__":  # pragma: no cover - 便于直接运行本文件
    raise SystemExit(pytest.main([__file__, "-q"]))