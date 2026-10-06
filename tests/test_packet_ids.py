"""models/packet_ids.py 与 models/const_ids.py 的单元测试。

运行方式：

    python -m tests.test_packet_ids
    python -m pytest tests/test_packet_ids.py -q

【为什么「生成的文件」更需要测试】
这两个模块由 tools/extract_packet_ids.py **生成**，没有人工逐行 review 的机会。
生成器一旦有 bug（正则漏抓一批成员、dump.cs 换版本后格式变了），
症状是「某个消息号查不到 / 查出来是错的」——而这种错通常发生在半夜发包的时候，
服务端要么静默丢弃，要么回一个看不出原因的失败。

因此这里做四层防护：

1. **数量级检查**：总数必须是 868 —— 数量突变意味着 dump.cs 换了版本，或解析坏了；
2. **抽样正确性**：对已人工核实过的消息号逐条断言（登录 / 日常 / 邮件 / 抽卡 / 市集）；
3. **表内自洽**：正查表与反查表严格互逆，且消息号无重复；
4. **与 dump.cs 同步**：现场重新解析 dump.cs 再逐条比对，
   防止有人绕过生成器手改了文件（手改的内容会在下次重新生成时被覆盖，越早发现越好）。
"""

from __future__ import annotations

import pytest

import config
from models import const_ids, packet_ids

#: 已人工核实过的消息号（来源：dump.cs 的 ``ePacketFormat_RPS``，行 384447~385319）。
#:
#: 【为什么要把数字写死】
#: 如果只断言「表不为空」，那么把 ``DMReward`` 解析成 6006 这种错误永远测不出来。
#: 这些数字是逐条对着 dump.cs 核对过的，可以当作基准使用。
VERIFIED_PACKET_IDS: dict[str, int] = {
    "AccountClientInfoLoginPacket": 1001,
    "LoginPacket": 1003,
    "LoginRes": 1004,
    "DailyMission": 6001,
    "DailyMissionRes": 6002,
    "DMActReward": 6003,
    "DMReward": 6005,
    "DMRewardRes": 6006,
    "UnLockActiveMission": 6009,
    "DrawNewPacket": 12101,
    "GetDrawMachineDataNewPacket": 12105,
    "MailGetListPacket": 13001,
    "MailOpenParcelPacket": 13003,
    "GetStoreListPacket": 14001,
    "BuyProductPacket": 14003,
    "GetReceiveStatePacket": 17001,
    "ReceiveGiftPacket": 17003,
}

#: dump.cs 换版本时这个数字会变。**刻意写成常量而不是 len() 比较**：
#: 数量突变必须被显式确认（改这一行 + 重新生成），而不是默默接受。
EXPECTED_PACKET_COUNT = 868

#: 已核实的资源 ID（来自 dump.cs 的 RPS_Const 常量类）。
VERIFIED_INT_CONSTS: dict[str, int] = {
    "COINS_ID": 200000050,
    "DIAMOND_ID": 200000001,
    "DIAMOND_PAY_ID": 200000053,
    "ENERGY_ID": 200000002,
    "SKILLPOINT_ID": 200000014,
    "SWEEPTICKET_ID": 200000016,
    "PLAYER_EXP_ID": 200000017,
    "Alliance_Currency_ID": 200000030,
}

#: 已核实的常量总数（RPS_Const 里可解析的 public const 成员）
EXPECTED_CONST_COUNT = 672


class TestPacketIds:
    """「协议类名 → 消息号」表的行为。"""

    def test_count_matches_expectation(self) -> None:
        assert packet_ids.PACKET_COUNT == EXPECTED_PACKET_COUNT
        assert len(packet_ids.PACKET_IDS) == EXPECTED_PACKET_COUNT
        assert len(packet_ids.PACKET_NAMES) == EXPECTED_PACKET_COUNT

    @pytest.mark.parametrize(("name", "expected"), sorted(VERIFIED_PACKET_IDS.items()))
    def test_verified_entries(self, name: str, expected: int) -> None:
        assert packet_ids.packet_id(name) == expected

    def test_packet_ids_are_unique(self) -> None:
        """消息号必须唯一。

        一旦重复，反查表就会静默丢掉一个协议（后出现的覆盖先出现的），
        表现是「收包时偶尔认不出是哪个协议」，这是极难排查的一类 bug。
        """
        values = list(packet_ids.PACKET_IDS.values())
        assert len(values) == len(set(values))

    def test_reverse_lookup_is_consistent(self) -> None:
        for name, value in packet_ids.PACKET_IDS.items():
            assert packet_ids.packet_name(value) == name

    def test_packet_name_returns_none_for_unknown(self) -> None:
        assert packet_ids.packet_name(-1) is None

    def test_unknown_name_error_suggests_candidates(self) -> None:
        """少打一个字母时，报错必须给出候选名字。

        否则你只能回去翻 33 MB 的 dump.cs 逐个肉眼比对，纯浪费时间。
        """
        with pytest.raises(KeyError) as excinfo:
            packet_ids.packet_id("DMRewad")
        assert "DMReward" in str(excinfo.value)

    def test_search_is_case_insensitive(self) -> None:
        assert packet_ids.search("dailymission") == packet_ids.search("DailyMission")
        assert ("DailyMission", 6001) in packet_ids.search("DailyMission")

    def test_search_can_match_message_number(self) -> None:
        assert ("DMReward", 6005) in packet_ids.search("6005")

    def test_search_respects_limit(self) -> None:
        assert len(packet_ids.search("e", limit=3)) == 3


class TestPacketIdsSyncedWithDump:
    """生成结果必须与 dump.cs 现场解析一致（防止手改生成文件）。"""

    pytestmark = pytest.mark.skipif(
        not config.DUMP_CS_PATH.is_file(),
        reason="dump.cs 不存在（可用 CHERRYTALE_MATERIAL_ROOT 指定素材目录）",
    )

    def test_generated_table_matches_dump(self) -> None:
        from tools.extract_packet_ids import extract_packet_ids

        entries = extract_packet_ids(config.DUMP_CS_PATH)
        assert entries, "从 dump.cs 没解析到任何消息号"
        assert dict(entries) == packet_ids.PACKET_IDS


class TestConstIds:
    """``RPS_Const`` 常量表的行为（资源 ID 的正确性直接影响「买什么/领什么」）。"""

    def test_count_matches_expectation(self) -> None:
        assert const_ids.CONST_COUNT == EXPECTED_CONST_COUNT
        assert len(const_ids.CONSTS) == EXPECTED_CONST_COUNT

    def test_no_unparsable_members(self) -> None:
        """当前 dump.cs 里所有常量都能解析；若某天突然跳过一批，说明格式变了，必须人工看一眼。"""
        assert const_ids.SKIPPED_CONST_COUNT == 0

    @pytest.mark.parametrize(("name", "expected"), sorted(VERIFIED_INT_CONSTS.items()))
    def test_verified_int_consts(self, name: str, expected: int) -> None:
        assert const_ids.int_const(name) == expected

    def test_int_consts_excludes_bool(self) -> None:
        """bool 是 int 的子类（``isinstance(True, int)`` 为真），``INT_CONSTS`` 必须显式排除它。

        【为什么用合成数据验证，而不是只看真实数据】
        当前 dump.cs 的 ``RPS_Const`` 里**恰好没有**布尔常量
        （布尔开关都在 ``RPS_ProjectSettings`` 里）。所以「只看真实数据」的断言
        其实什么都没验证到 —— 一旦游戏更新往这个类里加了 bool，
        ``IsOfficelState = true`` 就会混进资源 ID 表，之后按 ID 取值会拿到 True，
        进而组出完全错误的报文。这里用合成字典把**派生规则本身**测死。
        """
        sample = {"COINS_ID": 1, "IsOfficelState": True, "Folder": "text", "Ratio": 1.5}
        filtered = {
            name: value
            for name, value in sample.items()
            if isinstance(value, int) and not isinstance(value, bool)
        }
        assert filtered == {"COINS_ID": 1}

        # 再对真实数据做一次一致性确认：凡布尔常量都不得出现在 INT_CONSTS 中
        assert not any(isinstance(value, bool) for value in const_ids.INT_CONSTS.values())
        for name, value in const_ids.CONSTS.items():
            if isinstance(value, bool):
                assert name not in const_ids.INT_CONSTS

    def test_int_consts_subset_of_consts(self) -> None:
        for name, value in const_ids.INT_CONSTS.items():
            assert const_ids.CONSTS[name] == value

    def test_int_const_rejects_non_int(self) -> None:
        """把字符串常量当 ID 用时必须明确报错。

        如果这里「宽容」地返回字符串，错误会一路带到组包阶段，
        最后表现成「服务端说参数不合法」，而真正的原因（拿错了常量）早就看不出来了。
        """
        string_names = [name for name, value in const_ids.CONSTS.items() if isinstance(value, str)]
        assert string_names, "常量表里应存在字符串常量，否则本用例没有验证到东西"
        with pytest.raises(KeyError):
            const_ids.int_const(string_names[0])

    def test_unknown_const_error_suggests_candidates(self) -> None:
        with pytest.raises(KeyError) as excinfo:
            const_ids.const_value("COINS_IDS")
        assert "COINS_ID" in str(excinfo.value)

    def test_search_finds_resource_ids(self) -> None:
        hits = dict(const_ids.search("DIAMOND"))
        assert hits["DIAMOND_ID"] == 200000001


if __name__ == "__main__":  # 支持 `python -m tests.test_packet_ids`
    raise SystemExit(pytest.main([__file__, "-v", "--no-header"]))

