"""荣耀之巅任务（``tasks/top_pvp.py``）的行为与安全边界测试。

运行方式：

    python -m pytest tests/test_tasks_top_pvp.py -q
    python -m tests.test_tasks_top_pvp

【本文件最重要的两条断言（其余都是围绕它们的细节）】

1. **只读模式（``battles=0``，默认）绝不发挑战包** —— 用一个"记账"的传输替身
   记录每个出站消息号，然后逐个核对；
2. **出站消息号集合 ⊆ 白名单，且永远不出现 39005（花钻石买次数）**。
   这不是"相信代码写对了"，而是**从请求字节里解出 packetID** 再核对 ——
   只有这样才能真正挡住"某处顺手加了个购买分支"。

字节是照实测结构手工构造的（外层 ``RootPacket``：字段 1 = 消息号，
业务子包槽位编号 = 该子包的消息号），所以 ``send → parse_envelope → decode_sub_packet``
这条链路是真跑的，只有"字节怎么过网络"这一步被替换。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import config
from client.game_client import GameClient
from client.session import GameSession
from models.envelope import parse_envelope
from models.protobuf_wire import encode_int, encode_message, encode_string
from models.session_state import GameSessionState
from models.top_pvp import ALLOWED_PACKET_IDS, FORBIDDEN_PACKET_IDS
from tasks.base import RunOptions, create_task

# 复用 game_client 测试里的假响应对象，避免两份"手工构造响应"的代码各写各的
from tests.test_game_client import FakeResponse

import tasks.top_pvp as top_pvp_task  # noqa: F401  导入即注册

#: 本板块四个消息号（写死成常量，任何一处改动都会让测试红）
HOME_ID = 39001
CHALLENGE_ID = 39007
TEAM_EDIT_ID = 39011
TOGGLE_ID = 39019
BUY_ID = 39005


# ---------------------------------------------------------------------------
# 构造工具（全部离线）
# ---------------------------------------------------------------------------
def make_session() -> GameSession:
    """构造一个**不会联网**的会话（域名是保留域名），并带上最小可用登录态。"""
    session = GameSession("https://example.invalid", timeout=1.0)
    session.token_store.update(uid="u1", token="tok")
    session.set_game_state(GameSessionState(token="tok", player_id=1, server_id=1))
    return session


def envelope_bytes(packet_id: int, payload: bytes) -> bytes:
    """拼一段响应字节：字段 1 = 消息号，业务子包槽位编号 = 消息号本身。"""
    return encode_int(1, packet_id) + encode_message(packet_id, payload)


def home_payload(*, error_code: int = 0, is_skip_battle: bool = False) -> bytes:
    """39002 的子包载荷：``errorCode``(1) + ``isSkipBattle``(16)。"""
    payload = encode_int(1, error_code)
    if is_skip_battle:
        payload += encode_int(16, 1)
    return payload


def versus_payload(point: int = 1000, point_change: int = 12) -> bytes:
    """``TopPvpVersusInfoClass``：1 point、6 pointChange。"""
    return encode_int(1, point) + encode_int(6, point_change)


def team_edit_payload(*, def_teams: int = 3) -> bytes:
    """39012 的子包载荷：``roleRankList``(1) + ``lastUpdateTime``(2) + ``defTeamClass``(3)。

    保守队伍用占位字节（``TeamClass`` 未建模，按原始字节保留）。
    """
    payload = encode_int(2, 1789938036000)
    for index in range(def_teams):
        payload += encode_message(3, encode_int(1, index + 1))
    return payload


def challenge_payload(
    *,
    error_code: int = 0,
    win: bool = True,
    point_change: int = 12,
) -> bytes:
    """39008 的子包载荷：``errorCode``(1) + ``versusInfo_Me``(4) + ``replayResult``(6)。

    战报按实测结构给一个**小号 JSON**（真实的那份 46KB，但字段名一致）。
    """
    replay = json.dumps(
        {
            "elapsed": 1.0,
            "step": 418,
            "winOrFail": win,
            "isNpc": False,
            "error": False,
            "replay_file_name": "ABCD_DEF",
        },
        ensure_ascii=False,
    )
    payload = encode_int(1, error_code)
    if error_code == 0:
        payload += encode_message(4, versus_payload(point_change=point_change))
        payload += encode_string(6, replay)
    return payload


def patch_transport(monkeypatch: pytest.MonkeyPatch, handler) -> list[bytes]:
    """把 ``GameClient.post_raw`` 换成"记录请求 + 返回构造好的响应"的替身。

    :param handler: ``(消息号, 请求体) -> 响应字节``。
    :return: 记录到的请求体列表（断言"发了什么"用）。
    """
    recorded: list[bytes] = []

    def fake_post_raw(self: GameClient, body: bytes) -> FakeResponse:
        recorded.append(body)
        number = parse_envelope(body).packet_id
        return FakeResponse(handler(number, body))

    monkeypatch.setattr(GameClient, "post_raw", fake_post_raw)
    return recorded


def sent_packet_ids(recorded: list[bytes]) -> list[int]:
    """从记录下来的请求字节里解出消息号（**这就是"到底发了什么"的证据**）。"""
    return [parse_envelope(body).packet_id for body in recorded]


def make_task(**kwargs: object):
    """构造任务（默认只读：``RunOptions()`` 的 battles=0）。"""
    options = kwargs.pop("run_options", RunOptions())
    return create_task(
        "top_pvp_battle",
        make_session(),
        run_options=options,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# ① 只读模式（默认）：一个消耗次数的包都不许发
# ---------------------------------------------------------------------------
class TestReadOnlyMode:
    def test_sends_only_home_packet(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded = patch_transport(
            monkeypatch, lambda number, body: envelope_bytes(HOME_ID, home_payload())
        )
        result = make_task().run()

        assert result.ok is True
        assert sent_packet_ids(recorded) == [HOME_ID], "默认只读只该发一个 39001"

    def test_no_challenge_packet(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded = patch_transport(
            monkeypatch, lambda number, body: envelope_bytes(HOME_ID, home_payload())
        )
        make_task().run()

        assert CHALLENGE_ID not in sent_packet_ids(recorded)

    def test_skip_toggle_is_not_written_in_read_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """只读模式承诺"什么都不改"：即使开关是关闭的，也不去写 39019。"""
        recorded = patch_transport(
            monkeypatch,
            lambda number, body: envelope_bytes(
                HOME_ID, home_payload(is_skip_battle=False)
            ),
        )
        result = make_task().run()

        assert TOGGLE_ID not in sent_packet_ids(recorded)
        assert "未改动" in str(result.data.get("skip_battle_note", ""))

    def test_result_marks_read_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        patch_transport(
            monkeypatch, lambda number, body: envelope_bytes(HOME_ID, home_payload())
        )
        result = make_task().run()

        assert result.data["mode"] == "read_only"
        assert "只读" in result.message


def make_handler(
    *,
    home_skip: bool = False,
    home_error_code: int = 0,
    challenge_script: list[dict[str, object]] | None = None,
):
    """按消息号分支的响应工厂。

    :param home_skip: 39002 里 ``isSkipBattle`` 的值（服务端当前状态）。
    :param challenge_script: 每场挑战的响应参数（按顺序取；取完后复用最后一个）。
    """
    script: list[dict[str, object]] = list(challenge_script or [{}])

    def handler(number: int, _body: bytes) -> bytes:
        if number == HOME_ID:
            return envelope_bytes(
                HOME_ID, home_payload(error_code=home_error_code, is_skip_battle=home_skip)
            )
        if number == TOGGLE_ID:
            return envelope_bytes(TOGGLE_ID, encode_int(1, 0))  # errorCode=0
        if number == TEAM_EDIT_ID:
            # 39012：拉取队伍信息（挑战前的必发步骤）
            return envelope_bytes(TEAM_EDIT_ID, team_edit_payload())
        if number == CHALLENGE_ID:
            params = script.pop(0) if len(script) > 1 else script[0]
            return envelope_bytes(CHALLENGE_ID, challenge_payload(**params))  # type: ignore[arg-type]
        raise AssertionError(f"测试替身收到未预期的消息号：{number}（说明任务发错包了）")

    return handler


def battle_options(battles: int, *, max_battles: int = 10) -> RunOptions:
    """真打用的运行参数（间隔设 0：测试里不真的 sleep）。"""
    return RunOptions(battles=battles, max_battles=max_battles, interval_seconds=0)


# ---------------------------------------------------------------------------
# ② 「跳过战斗演出」：默认开启，但只在"真打之前"写
# ---------------------------------------------------------------------------
class TestSkipBattleToggle:
    def test_enabled_before_battle_when_server_says_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """服务端说它是关闭的 → 真打前先写 39019（需求：该选项默认开启）。"""
        recorded = patch_transport(monkeypatch, make_handler(home_skip=False))
        result = make_task(run_options=battle_options(1)).run()

        assert sent_packet_ids(recorded) == [HOME_ID, TOGGLE_ID, TEAM_EDIT_ID, CHALLENGE_ID]
        assert result.ok is True

    def test_not_written_when_already_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """已经是开启状态 → 不做多余写操作。"""
        recorded = patch_transport(monkeypatch, make_handler(home_skip=True))
        make_task(run_options=battle_options(1)).run()

        assert sent_packet_ids(recorded) == [HOME_ID, TEAM_EDIT_ID, CHALLENGE_ID]

    def test_escape_hatch_env_skips_toggle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """逃生舱（``CHERRYTALE_TOP_PVP_SKIP_BATTLE=0``）：连开关都不碰。

        ★ 2026-09-25：原来的「保留战斗演出」参数与 ``--keep-battle-animation``
        旗标都已按需求删除（行为固定为跳过），这里测的是**唯一**还能让工具不去动
        服务端开关的通道。它存在的意义只是"万一 39019 行为有变，留一条不改
        账号设置的路"，界面上没有任何开关可点。
        """
        monkeypatch.setattr(config, "TOP_PVP_SKIP_BATTLE_DEFAULT", False)
        recorded = patch_transport(monkeypatch, make_handler(home_skip=False))
        make_task(run_options=battle_options(1)).run()

        assert sent_packet_ids(recorded) == [HOME_ID, TEAM_EDIT_ID, CHALLENGE_ID]

    def test_toggle_failure_does_not_block_battle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """写开关失败只是"没跳过演出"，不该阻断挑战（结果里会写明原因）。"""

        def handler(number: int, body: bytes) -> bytes:
            if number == TOGGLE_ID:
                return envelope_bytes(TOGGLE_ID, encode_int(1, -13))  # data_error
            return make_handler(home_skip=False)(number, body)

        recorded = patch_transport(monkeypatch, handler)
        result = make_task(run_options=battle_options(1)).run()

        assert sent_packet_ids(recorded) == [
            HOME_ID,
            TOGGLE_ID,
            TEAM_EDIT_ID,
            CHALLENGE_ID,
        ]
        assert "失败" in str(result.data["skip_battle_note"])

    def test_team_edit_precedes_challenge(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """实测：真实客户端在挑战前先发 39011 —— 本任务镜像这个顺序。

        39011 缺失时的现象是"读得到状态、打不了架"（服务端回 22 字节
        ``{"response_code":"OK"}`` 而不是游戏协议），所以这一步的顺序被单独钉住。
        """
        recorded = patch_transport(monkeypatch, make_handler(home_skip=True))
        make_task(run_options=battle_options(1)).run()
        ids = sent_packet_ids(recorded)

        assert TEAM_EDIT_ID in ids
        assert ids.index(TEAM_EDIT_ID) < ids.index(CHALLENGE_ID)


# ---------------------------------------------------------------------------
# ③ 战斗循环：胜负都继续、次数不足即停、绝不重试
# ---------------------------------------------------------------------------
class TestBattleLoop:
    def test_two_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded = patch_transport(
            monkeypatch,
            make_handler(home_skip=True, challenge_script=[{"win": True}, {"win": True}]),
        )
        result = make_task(run_options=battle_options(2)).run()

        assert sent_packet_ids(recorded) == [
            HOME_ID,
            TEAM_EDIT_ID,
            CHALLENGE_ID,
            CHALLENGE_ID,
        ]
        assert result.data["wins"] == 2
        assert result.data["losses"] == 0
        assert "完成 2/2 场" in result.message

    def test_loss_does_not_stop_the_loop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """需求：不管胜负都按计算结果执行 —— 输了也要继续打下一场。"""
        recorded = patch_transport(
            monkeypatch,
            make_handler(
                home_skip=True,
                challenge_script=[
                    {"win": False, "point_change": -8},
                    {"win": True, "point_change": 12},
                ],
            ),
        )
        result = make_task(run_options=battle_options(2)).run()

        assert len(sent_packet_ids(recorded)) == 4
        assert (result.data["wins"], result.data["losses"]) == (1, 1)
        assert result.data["point_change_total"] == 4
        assert result.ok is True

    def test_no_challenge_times_stops_without_buying(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """服务器回 -1（次数不足）→ 立刻收工，**绝不**转去购买。"""
        recorded = patch_transport(
            monkeypatch,
            make_handler(home_skip=True, challenge_script=[{"error_code": -1}]),
        )
        result = make_task(run_options=battle_options(3)).run()
        ids = sent_packet_ids(recorded)

        assert ids == [HOME_ID, TEAM_EDIT_ID, CHALLENGE_ID], "次数不足后不该再发任何包"
        assert BUY_ID not in ids
        assert "次数已用完" in result.message
        assert result.ok is True

    def test_abnormal_error_marks_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """非 -1 的错误码要如实报失败（不猜、不掩盖）。"""
        patch_transport(
            monkeypatch,
            make_handler(home_skip=True, challenge_script=[{"error_code": -12}]),
        )
        result = make_task(run_options=battle_options(2)).run()

        assert result.ok is False
        assert "休赛期" in result.message

    def test_home_failure_stops_before_any_challenge(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorded = patch_transport(monkeypatch, make_handler(home_error_code=-12))
        result = make_task(run_options=battle_options(2)).run()

        assert sent_packet_ids(recorded) == [HOME_ID]
        assert result.ok is False

    def test_hard_cap_truncates_requested_battles(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """硬上限是第二道防线：命令行写 999 也只打上限那么多场。"""
        recorded = patch_transport(monkeypatch, make_handler(home_skip=True))
        result = make_task(
            run_options=RunOptions(battles=999, max_battles=2, interval_seconds=0)
        ).run()

        assert sent_packet_ids(recorded) == [
            HOME_ID,
            TEAM_EDIT_ID,
            CHALLENGE_ID,
            CHALLENGE_ID,
        ]
        assert result.data["planned_battles"] == 2


# ---------------------------------------------------------------------------
# ④ 安全边界：绝不购买次数
# ---------------------------------------------------------------------------
class TestSafetyGuards:
    def test_every_sent_packet_is_whitelisted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """把整条流程跑一遍，逐个核对**实际发出的消息号**。"""
        recorded = patch_transport(monkeypatch, make_handler(home_skip=False))
        make_task(run_options=battle_options(3)).run()
        ids = set(sent_packet_ids(recorded))

        assert ids <= set(ALLOWED_PACKET_IDS.values())
        assert ids & set(FORBIDDEN_PACKET_IDS.values()) == set()

    def test_buy_id_is_the_known_forbidden_one(self) -> None:
        assert FORBIDDEN_PACKET_IDS["TopPvpBuyChallengeCountPacket"] == BUY_ID

    def test_task_source_never_references_forbidden_packets(self) -> None:
        """源码级回归：任务文件里**不许出现**任何禁发报文类名。"""
        source = Path(str(top_pvp_task.__file__)).read_text(encoding="utf-8")

        for name in FORBIDDEN_PACKET_IDS:
            assert name not in source, f"tasks/top_pvp.py 不该引用禁发报文 {name}"

    def test_task_source_has_no_buy_call_sites(self) -> None:
        source = Path(str(top_pvp_task.__file__)).read_text(encoding="utf-8")

        assert re.search(r"TopPvpBuyChallengeCount\w*\s*\(", source) is None

    def test_dry_run_sends_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded = patch_transport(monkeypatch, make_handler())
        result = make_task(run_options=battle_options(2), dry_run=True).run()

        assert recorded == []
        assert result.ok is True
        assert result.data["planned_challenges"] == 2
        assert result.data["sent_packets"] == ()


class TestNonProtocolResponse:
    """实测踩到的"非协议响应"必须被翻译成**可读的业务失败**，而不是抛堆栈。"""

    def test_html_ack_is_reported_clearly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """服务端对战斗侧请求回 22 字节 ``{"response_code":"OK"}``（``text/html``）。

        这段文本**不在客户端字符串池里**（0 命中），说明它不是游戏协议的一部分 ——
        真机实测时它表现为"读得到状态、打不了架"，而原始堆栈对使用者毫无意义。
        """
        recorded = patch_transport(
            monkeypatch, lambda number, body: b'{"response_code":"OK"}'
        )
        result = make_task().run()

        assert result.ok is False
        assert "不是游戏协议" in result.message
        assert "response_code" in result.message
        assert sent_packet_ids(recorded) == [HOME_ID], "第一个包就异常，不该继续发"


if __name__ == "__main__":  # pragma: no cover - 便于直接运行本文件
    raise SystemExit(pytest.main([__file__, "-q"]))