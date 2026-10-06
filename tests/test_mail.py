"""邮件拉取与一键领取（``models/mail.py`` + ``tasks/mail.py``）的单元测试。

运行方式：

    python -m pytest tests/test_mail.py -q
    python -m tests.test_mail

    # 需要真实连通性时（会真的向网关发包）：
    CHERRYTALE_RUN_NETWORK_TESTS=1 python -m pytest tests/test_mail.py -q

【本文件验证的闭环】

    13001（空包）→ 13002（邮件列表）→ 取 mailID（字段 1）→ 13003（ID 列表）→ 13004（奖励清单）

联网部分用 ``GameClient.post_raw`` 的替身拦住，但**字节是照实测结构手工构造的**
（外层 ``RootPacket``：字段 1 = 消息号，业务子包槽位编号 = 该子包的消息号），
所以 ``send → parse_envelope → decode_sub_packet`` 这条链路是真跑的，
只有"字节怎么过网络"这一步被替换。

【为什么要钉死"只认字段 1"】
邮件结构（``MailClass``）里字段 1 就是 ``mailID``。若哪天有人把 ID 改去读
别的字段、或按"第几个字节"去猜，本文件的第一个测试类会立刻变红 ——
而那种错误在真机上表现为「领奖请求发出去被拒」，极难往回追。
"""

from __future__ import annotations

import os

import pytest

import config
from client.exceptions import ApiError, NetworkError
from client.game_client import UNITY_HEADERS, GameClient
from client.session import GameSession
from models.envelope import parse_envelope
from models.mail import (
    MAIL_ID_FIELD,
    MAIL_IS_READ_FIELD,
    MailGetListPacket,
    MailGetListRes,
    MailOpenParcelPacket,
    MailOpenParcelRes,
    mail_id_of,
    mail_is_read_of,
)
from models.protobuf_wire import (
    encode_int,
    encode_message,
    encode_packed_ints,
    encode_string,
    fields_by_number,
    read_int_list,
)
from client.session_store import resolve_session_state
from models.session_state import GameSessionState
from tasks.base import create_task
from tests.reward_guard import assert_no_bare_identifiers

# 复用 game_client 测试里的假响应对象，避免两份"手工构造响应"的代码各写各的
from tests.test_game_client import FakeResponse

import tasks.mail as mail_task  # noqa: F401  导入即注册（同时便于 monkeypatch 模块内引用）

#: 是否允许跑真实网络用例（与 tests/test_platform.py 同一约定）
NETWORK_TESTS_ENABLED = os.getenv("CHERRYTALE_RUN_NETWORK_TESTS", "0") == "1"

requires_network = pytest.mark.skipif(
    not NETWORK_TESTS_ENABLED,
    reason="默认不发起真实网络请求；需要时设 CHERRYTALE_RUN_NETWORK_TESTS=1",
)

has_credentials = pytest.mark.skipif(
    not (config.ACCOUNT_UID and config.ACCOUNT_TOKEN),
    reason="缺少账号凭据；需要时设 CHERRYTALE_UID / CHERRYTALE_TOKEN",
)

#: 四个消息号（与 ``models/packet_ids.py`` 一致）
MAIL_LIST_ID = 13001
MAIL_LIST_RES_ID = 13002
MAIL_OPEN_ID = 13003
MAIL_OPEN_RES_ID = 13004


# ---------------------------------------------------------------------------
# 构造工具（全部离线）
# ---------------------------------------------------------------------------
def make_session() -> GameSession:
    """构造一个**不会联网**的会话（域名是保留域名）。

    同时准备两份"登录态"：

    - ``game_state``（游戏会话状态）：业务任务发包的**硬要求** ——
      ``BaseTask.open_game_client()`` 没有它就不会构造客户端（宁可明确报错）；
    - ``token_store``（平台令牌）：供 ``requires_auth`` 前置检查与 HTTP 头路径使用。
    """
    session = GameSession("https://example.invalid", timeout=1.0)
    session.token_store.update(uid="u1", token="tok")
    session.set_game_state(GameSessionState(token="tok", player_id=1, server_id=1))
    return session


def mail_bytes(mail_id: int, *, is_read: int = 0, subject: str = "测试邮件") -> bytes:
    """拼一封 ``MailClass`` 的字节。

    字段顺序（``tools/extract_proto_fields.py --show MailClass``）：

        1 mailID(int)  2 isRead(int)  3 sendTime(string)  4 message(MailMessageClass)

    刻意把 2/3/4 也填上 —— 用来证明"字段 1 之外还有内容"时，
    取 ID 的逻辑依然正确（这是"按编号找，而不是按位置猜"的证据）。
    """
    message = encode_string(1, "system@cherrytale") + encode_string(2, subject)
    return (
        encode_int(1, mail_id)
        + encode_int(2, is_read)
        + encode_string(3, "2026-09-20 00:00:00")
        + encode_message(4, message)
    )


def mail_list_payload(*mail_ids: int) -> bytes:
    """13002 的子包载荷：``mailList``（字段 1，repeated MailClass）。"""
    return b"".join(encode_message(1, mail_bytes(mid)) for mid in mail_ids)


def mail_list_bytes(entries: tuple[tuple[int, int], ...]) -> bytes:
    """按 ``(mailID, isRead)`` 组合拼 13002 的 ``mailList``。

    :param entries: ``(邮件 ID, isRead)`` 序列；``isRead=1`` 表示**已读**。

    【为什么需要它（2026-09-22 真机缺陷的回归载体）】
    服务端对批量 13003 是**整包拒绝**的：清单里只要有一封已读邮件，
    整批就回 ``errorCode=-2``、一封都不发。所以「已读邮件必须被筛掉」
    这条规则需要能**稳定构造出"已读邮件"**的测试数据 —— 就是这个函数。
    """
    return b"".join(
        encode_message(1, mail_bytes(mail_id, is_read=is_read))
        for mail_id, is_read in entries
    )


def item_bytes(item_id: int, amount: int) -> bytes:
    """``ItemClass``：1 itemSid、2 itemID、3 itemAmount。"""
    return encode_int(1, 0) + encode_int(2, item_id) + encode_int(3, amount)


def open_parcel_payload(*items: tuple[int, int], error_code: int = 0) -> bytes:
    """13004 的子包载荷：``errorCode``（字段 1）+ ``addList``（字段 2，repeated GetObjClass）。

    ``GetObjClass`` 的字段 1 是 ``itemList``（repeated ItemClass）。
    """
    add_list = b"".join(encode_message(1, item_bytes(i, a)) for i, a in items)
    return encode_int(1, error_code) + encode_message(2, add_list)


def envelope_bytes(packet_id: int, payload: bytes) -> bytes:
    """拼一段响应字节：字段 1 = 消息号，业务子包槽位编号 = 消息号本身。

    这正是 ``parse_envelope`` 期望的结构（实测：99016 / 13002 都是这样）。
    """
    return encode_int(1, packet_id) + encode_message(packet_id, payload)


def patch_transport(monkeypatch: pytest.MonkeyPatch, handler) -> list[bytes]:
    """把 ``GameClient.post_raw`` 换成"记录请求 + 返回构造好的响应"的替身。

    :param handler: ``(请求体) -> 响应字节`` 的函数（测试里按消息号分支）。
    :return: 记录到的请求体列表（断言"发了什么"用）。
    """
    recorded: list[bytes] = []

    def fake_post_raw(self: GameClient, body: bytes) -> FakeResponse:
        recorded.append(body)
        return FakeResponse(handler(body))

    monkeypatch.setattr(GameClient, "post_raw", fake_post_raw)
    return recorded


def request_packet_id(body: bytes) -> int:
    """从**请求**字节里读出消息号（请求同样是 ``RootPacket``）。"""
    return parse_envelope(body).packet_id


def requested_ids(body: bytes) -> list[int]:
    """从 **13003 请求体**里读出 ``mailIDList``（字段 1，兼容 packed / 非 packed）。

    用来断言"我们到底把哪几个 mailID 发了出去" —— 判断"已读邮件有没有被筛掉"
    必须看真实字节，而不是看代码意图。
    """
    sub_packet = parse_envelope(body).sub_packet_bytes
    return read_int_list(fields_by_number(sub_packet), 1)


# ---------------------------------------------------------------------------
# ① 从邮件字节里取 ID —— **只认字段 1**
# ---------------------------------------------------------------------------
class TestMailIdExtraction:
    def test_takes_field_one(self) -> None:
        assert mail_id_of(mail_bytes(90210)) == 90210

    def test_field_order_does_not_matter(self) -> None:
        """字段顺序变了照样能取到（按编号找，不按位置猜）。"""
        scrambled = encode_string(3, "2026-01-01") + encode_int(1, 555) + encode_int(2, 1)
        assert mail_id_of(scrambled) == 555

    def test_missing_field_returns_none(self) -> None:
        """没有字段 1 的邮件 → ``None``（而不是抛异常、也不是回退到别的字段）。"""
        assert mail_id_of(encode_int(2, 1) + encode_string(3, "x")) is None

    def test_empty_bytes_returns_none(self) -> None:
        assert mail_id_of(b"") is None

    def test_field_number_constant(self) -> None:
        """把"ID 在字段 1"这件事钉死 —— 改动它意味着协议理解变了，必须显式确认。"""
        assert MAIL_ID_FIELD == 1


# ---------------------------------------------------------------------------
# ② 报文编解码（空包 / packed ID 列表 / 奖励清单）
# ---------------------------------------------------------------------------
class TestReadFlag:
    """``isRead``（字段 2）与"未读筛选" —— 2026-09-22 真机缺陷的回归测试。

    真机事实（#113，5 封邮件）：把「3 封未读 + 2 封已读」一次性批量发 → 整包
    ``errorCode=-2``、**一封都没到账**；改逐封发时 3 封未读全部成功、2 封已读各回 ``-2``。
    所以"只领未读"不是优化，而是**能不能领到**的前提。
    """

    def test_field_number_constant(self) -> None:
        assert MAIL_IS_READ_FIELD == 2

    def test_reads_flag(self) -> None:
        assert mail_is_read_of(mail_bytes(90210, is_read=1)) == 1
        assert mail_is_read_of(mail_bytes(90210, is_read=0)) == 0

    def test_missing_flag_is_none(self) -> None:
        """服务端省略 ``isRead``（默认值 0）时按"未读"处理，不能误判成已读而跳过。"""
        assert mail_is_read_of(encode_int(1, 5)) is None

    def test_unread_ids_skip_read_mails(self) -> None:
        res = MailGetListRes.decode(mail_list_bytes(((9001, 0), (9002, 1), (9003, 0))))

        assert res.mail_ids() == [9001, 9002, 9003], "全量取 ID 要如实反映邮箱"
        assert res.unread_mail_ids() == [9001, 9003], "待领清单必须跳过已读"
        assert res.read_mail_count() == 1

    def test_missing_flag_counts_as_unread(self) -> None:
        payload = b"".join(encode_message(1, encode_int(1, mid)) for mid in (7, 8))
        res = MailGetListRes.decode(payload)

        assert res.unread_mail_ids() == [7, 8]
        assert res.read_mail_count() == 0


class TestPackets:
    def test_list_request_is_empty_packet(self) -> None:
        """13001 是空报文：字段表为空、编码结果为零字节。"""
        packet = MailGetListPacket()
        assert packet.field_tag_map() == {}
        assert packet.encode() == b""

    def test_open_parcel_encodes_ids_as_packed(self) -> None:
        """13003 的 mailIDList 是 ``List<int>`` → packed（长度前缀 + 连续 varint）。"""
        packet = MailOpenParcelPacket.for_ids([7, 8, 300])
        assert packet.encode() == encode_packed_ints(1, [7, 8, 300])
        assert packet.to_hex().startswith("0a")  # 字段 1，线类型 2（length-delimited）

    def test_open_parcel_for_ids_normalises_elements(self) -> None:
        """工厂方法把元素强制成 ``int``（避免"看起来像整数"的值直接进模型）。"""
        packet = MailOpenParcelPacket.for_ids([1, 2])
        assert packet.mailIDList == [1, 2]
        assert all(isinstance(value, int) for value in packet.mailIDList)

    def test_decode_mail_list_yields_ids(self) -> None:
        res = MailGetListRes.decode(mail_list_payload(1001, 1002, 1003))
        # 明细不展开（保留原始字节），所以只断言"几封"与"ID 是什么"
        assert len(res.mailList) == 3
        assert res.mail_ids() == [1001, 1002, 1003]

    def test_decode_skips_mail_without_id(self) -> None:
        """某封邮件取不到 ID 时**跳过它**，不让整个邮箱拉取失败。"""
        payload = encode_message(1, mail_bytes(11)) + encode_message(1, encode_int(2, 1))
        res = MailGetListRes.decode(payload)
        assert len(res.mailList) == 2, "两封的原始字节都要保留"
        assert res.mail_ids() == [11]

    def test_decode_open_parcel_rewards(self) -> None:
        res = MailOpenParcelRes.decode(open_parcel_payload((200000001, 60), (200000016, 2)))
        assert res.is_success() is True
        got = [item.itemID for group in res.addList for item in group.itemList]
        assert got == [200000001, 200000016]
        assert "200000001×60" in res.reward_summary()

    def test_open_parcel_error_code_is_reported(self) -> None:
        res = MailOpenParcelRes.decode(open_parcel_payload(error_code=1234))
        assert res.is_success() is False
        assert res.errorCode == 1234
        assert res.reward_summary() == "<无道具>"


# ---------------------------------------------------------------------------
# ③ 任务层：拉列表 / 一键领取（网络被替身拦住，链路其余部分真跑）
# ---------------------------------------------------------------------------
def make_handler(
    *,
    mail_ids: tuple[int, ...] = (9001, 9002),
    items: tuple[tuple[int, int], ...] = ((200000001, 60),),
    error_code: int = 0,
):
    """按请求的消息号返回构造好的响应字节（模拟服务端）。"""

    def handler(body: bytes) -> bytes:
        packet_id = request_packet_id(body)
        if packet_id == MAIL_LIST_ID:
            return envelope_bytes(MAIL_LIST_RES_ID, mail_list_payload(*mail_ids))
        if packet_id == MAIL_OPEN_ID:
            return envelope_bytes(
                MAIL_OPEN_RES_ID, open_parcel_payload(*items, error_code=error_code)
            )
        raise AssertionError(f"任务发出了意料之外的消息号：{packet_id}")

    return handler


def make_dynamic_handler(
    *,
    mail_entries: tuple[tuple[int, int], ...] = ((9001, 0), (9002, 0)),
    items: tuple[tuple[int, int], ...] = ((200000001, 60),),
    reject_batch: bool = False,
    rejected_ids: tuple[int, ...] = (),
):
    """按**请求里到底带了哪些 ID** 决定回成功还是 ``-2`` 的服务端替身。

    :param mail_entries: 13002 返回的 ``(mailID, isRead)`` 组合（``isRead=1`` = 已读）。
    :param items: 13004 成功时带的奖励。
    :param reject_batch: ``True`` = 一次请求里含 **2 个以上** ID 就回 ``-2``
        （复现真机现象：批量里混进不可领的邮件 → 整包被拒）。
    :param rejected_ids: 这些 ID **单独发**也会被拒（真机上就是"已读 / 已领过"那些）。

    与 :func:`make_handler` 的区别：后者只看消息号，无法表达"批量被拒、逐封却有成功"
    这种真实场景 —— 而"降级逐封"的整个价值就在这个场景里。
    """

    def handler(body: bytes) -> bytes:
        packet_id = request_packet_id(body)
        if packet_id == MAIL_LIST_ID:
            return envelope_bytes(MAIL_LIST_RES_ID, mail_list_bytes(mail_entries))
        if packet_id == MAIL_OPEN_ID:
            ids = requested_ids(body)
            if (reject_batch and len(ids) > 1) or any(mid in rejected_ids for mid in ids):
                return envelope_bytes(MAIL_OPEN_RES_ID, open_parcel_payload(error_code=-2))
            return envelope_bytes(MAIL_OPEN_RES_ID, open_parcel_payload(*items))
        raise AssertionError(f"任务发出了意料之外的消息号：{packet_id}")

    return handler


@pytest.fixture(autouse=True)
def fast_single_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    """把"逐封领取"的间隔压成 0 秒。

    只影响**快慢**：请求的个数与顺序一个都不变（那正是要断言的），
    但不这么做的话，每个降级用例都要白等 0.3s × N。
    """
    monkeypatch.setattr(mail_task, "SINGLE_CLAIM_INTERVAL_SECONDS", 0.0)


class TestFetchMailList:
    def test_sends_empty_packet_and_returns_ids(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = patch_transport(monkeypatch, make_handler(mail_ids=(9001, 9002)))
        task = create_task("mail", make_session())

        res = task.fetch_mail_list()

        assert res.mail_ids() == [9001, 9002]
        assert len(calls) == 1, "拉列表只该发一个包"
        assert request_packet_id(calls[0]) == MAIL_LIST_ID
        # 13001 是空报文 → 业务子包槽位里的字节数为 0
        assert parse_envelope(calls[0]).sub_packet_bytes == b""

    def test_empty_mailbox_is_not_an_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """服务端回一个长度为 0 的 13002 子包（邮箱为空）→ 0 封邮件，而不是报错。"""
        calls = patch_transport(monkeypatch, make_handler(mail_ids=()))
        task = create_task("mail", make_session())

        res = task.fetch_mail_list()

        assert res.mailList == []
        assert res.mail_ids() == []
        assert len(calls) == 1


class TestClaimAllMails:
    def test_returns_rewards_and_sends_ids(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = patch_transport(
            monkeypatch, make_handler(items=((200000001, 60), (200000016, 2)))
        )
        task = create_task("mail", make_session())

        rewards = task.claim_all_mails([9001, 9002])

        got = [item.itemID for group in rewards for item in group.itemList]
        assert got == [200000001, 200000016]
        assert len(calls) == 1, "一键领取就是**一个**请求（协议把批量做进了 ID 列表）"
        assert request_packet_id(calls[0]) == MAIL_OPEN_ID
        # 子包内字段 1 = mailIDList，且是 packed 形态
        assert encode_packed_ints(1, [9001, 9002]) in calls[0]

    def test_error_code_raises_api_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """业务失败必须抛异常：返回空清单会让任务"成功但没奖励"（最糟的失败方式）。"""
        patch_transport(monkeypatch, make_handler(error_code=1234))
        task = create_task("mail", make_session())

        with pytest.raises(ApiError) as excinfo:
            task.claim_all_mails([9001])

        assert "1234" in str(excinfo.value)

    def test_empty_id_list_sends_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = patch_transport(monkeypatch, make_handler())
        task = create_task("mail", make_session())

        assert task.claim_all_mails([]) == []
        assert calls == []


class TestClaimIndividually:
    """逐封领取（批量被服务端拒绝后的降级路径）。"""

    def test_mixed_success_and_rejection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = patch_transport(monkeypatch, make_dynamic_handler(rejected_ids=(9002,)))
        task = create_task("mail", make_session())

        outcome = task.claim_individually([9001, 9002])

        assert outcome.claimed == 1
        assert outcome.failed == [(9002, -2)]
        got = [item.itemID for group in outcome.rewards for item in group.itemList]
        assert got == [200000001]
        # 每封**单独**一个请求，且每封只发一次（实测已读邮件重发也不会变成功）
        assert [request_packet_id(body) for body in calls] == [MAIL_OPEN_ID, MAIL_OPEN_ID]
        assert [requested_ids(body) for body in calls] == [[9001], [9002]]

    def test_empty_input_sends_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = patch_transport(monkeypatch, make_dynamic_handler())

        outcome = create_task("mail", make_session()).claim_individually([])

        assert outcome.claimed == 0
        assert outcome.failed == []
        assert calls == []

    def test_network_failure_marks_only_that_mail(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """单封网络失败只毁掉那一封：其余照领，失败记进 ``failed``（不抛异常）。"""
        handler = make_dynamic_handler()
        sent = {"count": 0}

        def flaky(self: GameClient, body: bytes) -> FakeResponse:
            sent["count"] += 1
            if sent["count"] == 1:
                raise NetworkError("测试替身：第一封网络失败")
            return FakeResponse(handler(body))

        monkeypatch.setattr(GameClient, "post_raw", flaky)
        outcome = create_task("mail", make_session()).claim_individually([9001, 9002])

        assert outcome.claimed == 1
        assert outcome.failed == [(9001, None)], "网络失败没有业务码，记 None"


class TestTaskRun:
    def test_full_loop_two_packets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """闭环：13001 → 13002 → 13003 → 13004，恰好两个请求。"""
        calls = patch_transport(
            monkeypatch, make_handler(mail_ids=(9001, 9002), items=((200000001, 60),))
        )
        task = create_task("mail", make_session())

        result = task.run()

        assert result.ok is True
        assert result.data["claimed"] == 2
        assert [request_packet_id(body) for body in calls] == [MAIL_LIST_ID, MAIL_OPEN_ID]
        # ★ 2026-10-04 邮件结果文案只报封数：不含奖励明细、不含裸标识符。
        assert result.message == "一键领取 2 封邮件成功"
        assert "rewards" not in result.data
        assert_no_bare_identifiers(result.message)

    def test_empty_mailbox_stops_after_first_packet(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = patch_transport(monkeypatch, make_handler(mail_ids=()))
        result = create_task("mail", make_session()).run()

        assert result.ok is True
        assert result.data["claimed"] == 0
        assert [request_packet_id(body) for body in calls] == [MAIL_LIST_ID]

    def test_network_failure_becomes_failed_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """网络失败 → 失败结果（而不是抛到调用方），每日流程才能继续往后跑。"""

        def boom(self: GameClient, body: bytes) -> FakeResponse:
            raise NetworkError("测试替身：禁止在单元测试中真的发包")

        monkeypatch.setattr(GameClient, "post_raw", boom)
        result = create_task("mail", make_session()).run()

        assert result.ok is False

    def test_dry_run_sends_nothing(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        calls = patch_transport(monkeypatch, make_handler())
        task = create_task("mail", make_session(), dry_run=True)

        result = task.run()

        assert result.ok is True
        assert "演练完成" in result.message
        assert calls == [], "演练模式不该发出任何请求"
        assert "13003" in capsys.readouterr().out


class TestBatchFallback:
    """任务级闭环：只领未读 + 批量被拒时逐封降级（复现 2026-09-22 真机现象）。

    真机日志长这样（网页与命令行走同一份代码）：

        邮件共 5 封，解析出 5 个 mailID：[1251764650, ...]
        ✘ 业务错误 code=-2：一键领取 5 封邮件被服务端拒绝

    而这 5 封里恰好有 2 封 ``isRead=1``（已领过但服务端仍留在列表里）。
    """

    def test_sends_only_unread_ids(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = patch_transport(
            monkeypatch, make_dynamic_handler(mail_entries=((9001, 0), (9002, 1)))
        )

        result = create_task("mail", make_session()).run()

        assert result.ok is True
        assert result.data["claimed"] == 1
        assert [request_packet_id(body) for body in calls] == [MAIL_LIST_ID, MAIL_OPEN_ID]
        assert requested_ids(calls[1]) == [9001], "已读邮件绝不能出现在 13003 里"

    def test_batch_rejection_falls_back_to_singles(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = patch_transport(
            monkeypatch, make_dynamic_handler(reject_batch=True, rejected_ids=(9002,))
        )

        result = create_task("mail", make_session()).run()

        assert result.ok is True, "两封里成功一封就不该整批报失败"
        assert result.data["claimed"] == 1
        assert result.data["failed"] == 1
        # ★ 2026-10-04 结果视图纪律：邮件不再报"哪些邮件"（`failed_ids` 已从 data 收敛掉），
        # 逐封失败的 mailID 只进 DEBUG 开发者日志。
        assert "failed_ids" not in result.data
        assert "9002" not in result.message
        assert "逐封" in result.message
        # ★ 2026-10-04「结果视图纪律」：运行视图只说"领了几封"，
        # 既不带裸标识符，也不把 13004 的持有量当"获得"念。
        assert_no_bare_identifiers(result.message)
        assert "获得" not in result.message
        # 13001 → 批量（2 个 ID，被拒）→ 逐封 9001（成功）→ 逐封 9002（-2）
        assert [request_packet_id(body) for body in calls] == [
            MAIL_LIST_ID,
            MAIL_OPEN_ID,
            MAIL_OPEN_ID,
            MAIL_OPEN_ID,
        ]
        assert requested_ids(calls[1]) == [9001, 9002]
        assert [requested_ids(body) for body in calls[2:]] == [[9001], [9002]]

    def test_all_rejected_is_a_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        patch_transport(
            monkeypatch,
            make_dynamic_handler(reject_batch=True, rejected_ids=(9001, 9002)),
        )

        result = create_task("mail", make_session()).run()

        assert result.ok is False, "一封都没领到才算失败"
        assert "-2" in result.message

    def test_all_read_mailbox_is_ok_and_sends_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """全是已读邮件（= 都已领过）不是错误，别去打扰服务端。"""
        calls = patch_transport(
            monkeypatch, make_dynamic_handler(mail_entries=((9001, 1), (9002, 1)))
        )

        result = create_task("mail", make_session()).run()

        assert result.ok is True
        assert result.data["claimed"] == 0
        assert "没有未读邮件" in result.message
        assert [request_packet_id(body) for body in calls] == [MAIL_LIST_ID]


# ---------------------------------------------------------------------------
# ④ 真实连通性（默认跳过）
# ---------------------------------------------------------------------------
def make_live_session() -> GameSession:
    """用真实凭据构造会话（只有联网用例才会用到）。

    游戏会话状态按 ``client/session_store.py`` 的优先级取
    （环境变量 ``CHERRYTALE_SESSION_TOKEN`` > ``config_local.py`` > 会话文件）——
    与 ``main.py run`` 的行为保持一致，免得测试里能用、命令行里不能用。
    """
    session = GameSession(
        config.GAME_GATEWAY_URL,
        headers=UNITY_HEADERS,
        timeout=config.GAME_SEND_TIMEOUT,
    )
    session.token_store.update(uid=config.ACCOUNT_UID, token=config.ACCOUNT_TOKEN)
    state, _source = resolve_session_state()
    if state is not None:
        session.set_game_state(state)
    return session


@pytest.mark.network
@requires_network
@has_credentials
class TestMailNetwork:
    """对真实网关跑一次**只读**的邮件拉取（13001 → 13002）。

    .. note::
       这里刻意**不发** 13003（领取）：领奖有副作用，不该因为"跑测试"就动账号里的邮件。
       要真的领取，先 ``python main.py run mail --dry-run`` 看演练，
       确认无误后再 ``python main.py run mail``。
    """

    def test_fetch_mail_list_reaches_server(self) -> None:
        task = create_task("mail", make_live_session())

        res = task.fetch_mail_list()

        print(f"\n邮件 {len(res.mailList)} 封，mailID={res.mail_ids()}")
        assert isinstance(res.mailList, list)


if __name__ == "__main__":  # 支持 `python -m tests.test_mail`
    raise SystemExit(pytest.main([__file__, "-v", "--no-header"]))
