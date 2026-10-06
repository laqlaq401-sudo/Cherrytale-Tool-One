"""登录签到钩子（``tasks/login_signin.py``）的单元测试。

运行方式：

    python -m pytest tests/test_tasks_login_signin.py -q

【本文件要钉死的六条规则】
1. **只发 33055 一个空查询**（消息号从请求字节解出核对；白名单外一律拒发）；
2. **三路入账的文案口径**：每日=dailyAddList 按 GAINED（与
   ``LoginReWardLoopData`` 逐字节吻合过）、活动/新手按 UNKNOWN（数值语义
   未实证，不许报数字）—— 用 ``protocol_samples`` 的真实抓包字节回放；
3. **每自然日只发一次**（0 点口径：同日第二次调用零发包，跨日重发）；
4. **发送失败不记账、不上抛**（下次登录自动重试；登录主流程绝不被连坐）；
5. 会话缺区服/角色信息 → 直接跳过；
6. **禁发表**钉住通行证三件套（33057/33059/33061）—— 签到与通行证是两个系统。

消息号写死在本文件（不 import 生产常量）：协议编号被改动会在这里直接暴露。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import pytest

from client.exceptions import NetworkError
from client.game_client import GameClient
from models.envelope import parse_envelope
from models.login_rewards import (
    SIGNIN_ALLOWED_PACKET_IDS,
    SIGNIN_FORBIDDEN_PACKET_IDS,
    GetActivityLoginRewards,
    GetActivityLoginRewardsRes,
)
from models.protobuf_wire import encode_int, encode_message
from models.session_state import GameSessionState
from models.wallet import GetAllItemPacket, GetAllItemRes
from tasks.login_signin import TITLE, describe_credits, run_if_first_login_today
from tasks.packet_guard import guarded_send

from tests.test_game_client import FakeResponse

from protocol_samples.login_reward_samples import (
    LOGIN_REWARD17_REQUEST_HEX,
    LOGIN_REWARD17_RESPONSE_HEX,
)

#: 消息号写死在测试里（依据见 models/packet_ids.py 的 33xxx 段）
PACKET_LOGIN_REWARDS = 33055
PACKET_LOGIN_REWARDS_RES = 33056
#: 通行证三件套（签到链路的**禁发**对象，见 models/login_rewards.py）
PACKET_GET_LOGIN_PASS = 33057
PACKET_BUY_LOGIN_PASS = 33059
PACKET_RECEIVE_LOGIN_PASS_REWARD = 33061

#: 与真实抓包同一天的"今天"（只会影响记账日期，不影响断言）
DAY_ONE = datetime(2026, 10, 6, 3, 9, 0)
DAY_TWO = datetime(2026, 10, 7, 0, 0, 30)


def make_state(**overrides: object) -> GameSessionState:
    """一个带区服/角色信息的会话状态（钩子只读这两个字段）。"""
    fields: dict[str, object] = {"token": "tok", "player_id": 1, "server_id": 1}
    fields.update(overrides)
    return GameSessionState(**fields)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 报文构造（字段编号见 models/proto_fields.py）
# ---------------------------------------------------------------------------
def envelope_bytes(packet_id: int, payload: bytes) -> bytes:
    """拼响应字节：字段 1 = 消息号，业务子包槽位编号 = 消息号本身。"""
    return encode_int(1, packet_id) + encode_message(packet_id, payload)


def daily_add_payload(item_id: int, amount: int) -> bytes:
    """一段 ``dailyAddList``：1 itemList → {2 itemID、3 itemAmount}。"""
    return encode_message(1, encode_int(2, item_id) + encode_int(3, amount))


def patch_transport(
    monkeypatch: pytest.MonkeyPatch, response_payload: bytes | None = None
) -> list[bytes]:
    """把 ``GameClient.post_raw`` 换成"记录请求 + 回放 33056"的替身。

    :param response_payload: 33056 的业务子包字节；缺省回放**真实抓包样本**
        （2026-10-06 登录：每日 高級寶石袋×2 + 三个活动各一条入账）。
    """
    recorded: list[bytes] = []
    payload = (
        response_payload
        if response_payload is not None
        else bytes.fromhex(LOGIN_REWARD17_RESPONSE_HEX)
    )

    def fake_post_raw(self: GameClient, body: bytes) -> FakeResponse:
        recorded.append(body)
        packet_id = parse_envelope(body).packet_id
        if packet_id == PACKET_LOGIN_REWARDS:
            return FakeResponse(envelope_bytes(PACKET_LOGIN_REWARDS_RES, payload))
        raise AssertionError(f"测试替身收到了意料之外的请求：{packet_id}")

    monkeypatch.setattr(GameClient, "post_raw", fake_post_raw)
    return recorded


def sent_packet_ids(recorded: list[bytes]) -> list[int]:
    return [parse_envelope(body).packet_id for body in recorded]


def read_targets(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))["targets"]


# ---------------------------------------------------------------------------
# 真实抓包样本的模型往返（字段映射的"已验证基线"）
# ---------------------------------------------------------------------------
class TestRealSampleRoundTrip:
    def test_request_is_empty_and_matches_capture(self) -> None:
        """33055 请求必须是**空子包**，且与真实抓包逐字节一致。"""
        assert GetActivityLoginRewards().encode() == b""
        assert bytes.fromhex(LOGIN_REWARD17_REQUEST_HEX) == b""

    def test_three_channels_are_parsed_from_real_bytes(self) -> None:
        """真实 33056：3 个活动 + 每日 1 条 + 新手 0 条，道具与数量都对上。"""
        res = GetActivityLoginRewardsRes.decode(bytes.fromhex(LOGIN_REWARD17_RESPONSE_HEX))
        assert [a.sid for a in res.activityData] == [607000083, 607000084, 607000085]
        assert [a.title for a in res.activityData] == [
            "間宮麻理繪登入獎勵",
            "間宮麻理華登入獎勵",
            "月下盛宴登入獎勵",
        ]
        # 每日侧：200707002×2 == LoginReWardLoopData 第 11 天（抓包定论的锚点）
        daily_items = [i for g in res.dailyAddList for i in g.itemList]
        assert [(i.itemID, i.itemAmount) for i in daily_items] == [(200707002, 2)]
        assert res.newPlayerAddList == []


# ---------------------------------------------------------------------------
# 文案口径
# ---------------------------------------------------------------------------
class TestDescribeCredits:
    def test_real_sample_mixes_gained_and_unknown(self) -> None:
        """每日按 GAINED 报数量；活动按 UNKNOWN 只报件数（Q-021 纪律）。"""
        res = GetActivityLoginRewardsRes.decode(bytes.fromhex(LOGIN_REWARD17_RESPONSE_HEX))
        line = describe_credits(res)
        assert line.startswith(f"{TITLE} ✔ ")
        assert "每日签到 获得：高級寶石袋×2" in line
        assert "「月下盛宴登入獎勵」获得 1 件道具" in line
        # 活动侧的 13/23/33 语义未实证，绝不能进文案
        assert "×13" not in line and "×23" not in line and "×33" not in line

    def test_empty_response_is_reported_as_settled(self) -> None:
        """三路全空 → "今日已结算"（可能已在游戏客户端领过）。"""
        res = GetActivityLoginRewardsRes()
        assert describe_credits(res) == (
            f"{TITLE} ✔ 今日已结算，无新入账（可能已在游戏客户端领取）"
        )

    def test_daily_with_unnamed_item_falls_back_to_count(self) -> None:
        """每日侧道具不在名称表时降级成"获得 N 件道具"，不能一句话都没有。"""
        from models.daily import GetObjClass, ItemClass

        res = GetActivityLoginRewardsRes(
            dailyAddList=[GetObjClass(itemList=[ItemClass(itemID=999999999, itemAmount=7)])]
        )
        line = describe_credits(res)
        assert "每日签到 获得 1 件道具" in line


# ---------------------------------------------------------------------------
# 钩子主流程
# ---------------------------------------------------------------------------
class TestRunIfFirstLoginToday:
    def test_sends_33055_once_and_marks_bookkeeping(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """发一次 33055 → 记账文件出现本角色今天的记录 + 一条 ✔ 摘要。"""
        state_file = tmp_path / "signin.json"
        recorded = patch_transport(monkeypatch)
        with caplog.at_level(logging.INFO, logger="tasks.login_signin"):
            run_if_first_login_today(make_state(), now=DAY_ONE, path=state_file)

        assert sent_packet_ids(recorded) == [PACKET_LOGIN_REWARDS]
        # 请求必须是空子包（33055 没有任何字段；post_raw 记录的是整封信封，
        # 子包槽位要剥出来看 —— 与真实抓包 raw/17 请求一致）
        assert parse_envelope(recorded[0]).sub_packet_bytes == b""
        assert bytes.fromhex(LOGIN_REWARD17_REQUEST_HEX) == b""
        entry = read_targets(state_file)["s=1|p=1"]
        assert entry["date"] == "2026-10-06"  # 0 点口径的自然日
        summary = [r.getMessage() for r in caplog.records if TITLE in r.getMessage()]
        assert summary and "✔" in summary[-1] and "高級寶石袋×2" in summary[-1]

    def test_second_call_same_day_sends_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """同一天再登录 → 记账命中，一次包都不发（0 点口径的判重）。"""
        state_file = tmp_path / "signin.json"
        recorded = patch_transport(monkeypatch)
        run_if_first_login_today(make_state(), now=DAY_ONE, path=state_file)
        run_if_first_login_today(make_state(), now=DAY_ONE, path=state_file)
        assert sent_packet_ids(recorded) == [PACKET_LOGIN_REWARDS]

    def test_new_day_sends_again(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """跨过自然日 0 点 → 重新发送（与 05:00 游戏日口径刻意不同）。"""
        state_file = tmp_path / "signin.json"
        recorded = patch_transport(monkeypatch)
        run_if_first_login_today(make_state(), now=DAY_ONE, path=state_file)
        run_if_first_login_today(make_state(), now=DAY_TWO, path=state_file)
        assert sent_packet_ids(recorded) == [PACKET_LOGIN_REWARDS, PACKET_LOGIN_REWARDS]

    def test_different_role_gets_its_own_send(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """记账按 s=|p= 分桶：换角色（或换区）后是"新的一天第一次"。"""
        state_file = tmp_path / "signin.json"
        recorded = patch_transport(monkeypatch)
        run_if_first_login_today(make_state(), now=DAY_ONE, path=state_file)
        run_if_first_login_today(
            make_state(player_id=2), now=DAY_ONE, path=state_file
        )
        assert sent_packet_ids(recorded) == [PACKET_LOGIN_REWARDS, PACKET_LOGIN_REWARDS]
        assert set(read_targets(state_file)) == {"s=1|p=1", "s=1|p=2"}

    def test_empty_sub_packet_response_is_reported_as_settled(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """服务端回"纯元信息、无子包"的 33056（无活动且无入账时可能出现）
        → 等同三路全空，如实报"已结算"并正常记账（不许当成协议错误）。"""
        state_file = tmp_path / "signin.json"
        recorded = patch_transport(monkeypatch, response_payload=b"")
        with caplog.at_level(logging.INFO, logger="tasks.login_signin"):
            run_if_first_login_today(make_state(), now=DAY_ONE, path=state_file)
        assert sent_packet_ids(recorded) == [PACKET_LOGIN_REWARDS]
        summary = [r.getMessage() for r in caplog.records if TITLE in r.getMessage()]
        assert summary and "今日已结算" in summary[-1]
        assert read_targets(state_file)["s=1|p=1"]["date"] == "2026-10-06"

    def test_failure_does_not_raise_nor_mark(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """网络失败 → 只留一条 ✘，不记账（下次登录重试）、绝不上抛。"""
        state_file = tmp_path / "signin.json"

        def broken_post_raw(self: GameClient, body: bytes) -> FakeResponse:
            raise NetworkError("测试替身：网络断了")

        monkeypatch.setattr(GameClient, "post_raw", broken_post_raw)
        with caplog.at_level(logging.WARNING, logger="tasks.login_signin"):
            # 不抛 = 登录主流程不会被签到连坐
            run_if_first_login_today(make_state(), now=DAY_ONE, path=state_file)

        assert read_targets(state_file) == {}
        failures = [r.getMessage() for r in caplog.records if TITLE in r.getMessage()]
        assert failures and "✘" in failures[-1]

    def test_session_without_identity_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """区服/角色缺一个 → 不发包也不记账（连"是谁"都不知道）。"""
        state_file = tmp_path / "signin.json"
        recorded = patch_transport(monkeypatch)
        run_if_first_login_today(
            make_state(player_id=0, server_id=0), now=DAY_ONE, path=state_file
        )
        assert recorded == []
        assert read_targets(state_file) == {}

    def test_corrupt_bookkeeping_file_degrades_to_send(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """记账文件损坏 → 静默当空、照常发送（多发一次幂等查询无害）。"""
        state_file = tmp_path / "signin.json"
        state_file.write_text("{不是 JSON", encoding="utf-8")
        recorded = patch_transport(monkeypatch)
        run_if_first_login_today(make_state(), now=DAY_ONE, path=state_file)
        assert sent_packet_ids(recorded) == [PACKET_LOGIN_REWARDS]
        assert read_targets(state_file)["s=1|p=1"]["date"] == "2026-10-06"


# ---------------------------------------------------------------------------
# 发包闸门
# ---------------------------------------------------------------------------
class TestSendGate:
    def test_whitelist_rejects_anything_but_33055(self) -> None:
        """白名单外的包（哪怕是只读的 2007）也必须被拒 —— 钩子不是后门。"""
        with GameClient() as game:
            with pytest.raises(ValueError, match="不在本任务的白名单里"):
                guarded_send(
                    game,
                    GetAllItemPacket(),
                    GetAllItemRes,
                    allowed=SIGNIN_ALLOWED_PACKET_IDS,
                    forbidden=SIGNIN_FORBIDDEN_PACKET_IDS,
                )

    def test_forbidden_table_pins_pass_packets(self) -> None:
        """禁发表钉死通行证三件套：签到链路永远不该碰它们。"""
        assert SIGNIN_FORBIDDEN_PACKET_IDS == {
            "GetLoginPassPacket": PACKET_GET_LOGIN_PASS,
            "BuyLoginPassPacket": PACKET_BUY_LOGIN_PASS,
            "ReceiveLoginPassRewardPacket": PACKET_RECEIVE_LOGIN_PASS_REWARD,
        }
        # 白名单与禁发表不许有交集（guarded_send 先查禁发，交集会让白名单形同虚设）
        assert not set(SIGNIN_ALLOWED_PACKET_IDS) & set(SIGNIN_FORBIDDEN_PACKET_IDS)


if __name__ == "__main__":  # 支持 `python -m tests.test_tasks_login_signin`
    raise SystemExit(pytest.main([__file__, "-v", "--no-header"]))
