"""游戏网关客户端：**HTTP POST + 裸 protobuf**。

【它负责什么】
把业务子包装进 ``RootPacket``（见 ``models/envelope.py``），POST 到
``https://game-ct-labs.ecchi.xxx:1893/``，再把响应解析回信封视图。
一句话：**负责「怎么把字节送出去、怎么把字节读回来」**，不掺业务语义。

【为什么不自己写 HTTP 层】
超时、重试、TLS 信任库、代理、异常翻译都已在 ``client/session.py`` 里实现并测试过。
这里只是把二进制体塞进它现成的 ``prepare_request(data=...)``。

【三条安全默认（都来自实测或踩坑）】

1. **POST 永不自动重试** —— 沿用 ``GameSession`` 的默认值（只重试 GET）。
   领奖类接口不幂等：超时后重发可能造成重复领取甚至触发风控；
2. **请求头与抓包逐项一致** —— 固定的 5 个，其余交给 HTTP 库。
   多出来的头反而是「非官方客户端」的破绽；
3. **``build()`` 与 ``send()`` 严格分离** —— 前者只组装字节、绝不落网。
   排查阶段永远先 ``build()`` 看字节，与抓包对齐后再 ``send()``。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Mapping

import requests

import config
from client.exceptions import AbnormalPacketError, SessionKickedError
from client.session import GameSession
from models.envelope import EnvelopeError, EnvelopeView, GameEnvelope, parse_envelope
from models.game_error import (
    GAME_ERROR_CLASS_NAME,
    GAME_ERROR_PACKET_ID,
    ServerExceptionRes,
    is_session_kicked,
)
from models.game_packet import ProtoMessage
from models.handshake import VersionControlServerRes, VersionControlServerPacket
from models.protobuf_wire import ProtobufWireError, fields_by_number

_LOGGER: Final[logging.Logger] = logging.getLogger("client.game")

#: 实测的请求头（**逐字符照抄抓包**）。
#:
#: 刻意不含 ``Host`` 与 ``Content-Length`` —— 那两个由 HTTP 库按实际连接与包体长度
#: 自动生成，手写反而容易写错（例如 Content-Length 与实际不符，服务端会直接拒绝）。
UNITY_HEADERS: Final[dict[str, str]] = {
    "User-Agent": config.UNITY_USER_AGENT,
    "Accept": "*/*",
    "Accept-Encoding": config.GAME_ACCEPT_ENCODING,
    "Content-Type": config.GAME_CONTENT_TYPE,
    "X-Unity-Version": config.UNITY_VERSION,
}


@dataclass(frozen=True)
class OutgoingRequest:
    """一次**将要发出**的请求（``--dry-run`` 时拿它给用户看）。

    :param method: HTTP 方法（实测固定为 POST）。
    :param url: 完整地址。
    :param headers: 请求头（含 HTTP 库自动生成的那两个，便于逐项对照抓包）。
    :param body: 请求体字节（裸 protobuf）。
    """

    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes

    def hex(self) -> str:
        """请求体十六进制（与 Fiddler 的 HexView 逐字节对照用）。"""
        return self.body.hex()

    def hex_lines(self, width: int = 32) -> list[str]:
        """按固定宽度切分的 hex 行（便于人眼比对）。"""
        text = self.hex()
        return [text[index : index + width] for index in range(0, len(text), width)]

    def preview_bytes(self, count: int = 16) -> str:
        """请求体前 N 字节的 hex —— 最常拿来与抓包比对的一小段。"""
        return self.body[:count].hex()

    def describe(self) -> str:
        """多行可读描述（**先看这个，再发**）。"""
        lines = [f"{self.method} {self.url}", "请求头："]
        for name, value in self.headers.items():
            lines.append(f"  {name}: {value}")
        lines.append(f"请求体（{len(self.body)} 字节）：")
        lines.extend(f"  {line}" for line in self.hex_lines())
        return "\n".join(lines)


class GameClient:
    """游戏网关客户端。

    :param session: 复用已有会话；``None`` 表示按实测配置新建一个。
    :param envelope: 信封状态（跨请求保持会话）；``None`` 表示新建。
    :param timeout: 单次请求超时；``None`` 表示用 ``config.GAME_SEND_TIMEOUT``。
    :param logger: 日志器。

    .. note::
       与 ``PlatformClient``（平台网关）的分工：
       本类只跟**游戏服**打交道，用的是一套完全不同的协议
       （HTTP POST + 裸 protobuf，见模块文档）。两者互不复用对方的常量。
    """

    def __init__(
        self,
        *,
        session: GameSession | None = None,
        envelope: GameEnvelope | None = None,
        timeout: float | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.logger = logger or _LOGGER
        self.envelope = envelope or GameEnvelope()
        self.timeout = config.GAME_SEND_TIMEOUT if timeout is None else timeout

        # token_field=None 是**刻意**的：令牌放在 RootPacket 的字段里，
        # **不在** HTTP 头（抓包实测）。若误配成某个头，请求会平白多出一个鉴权字段，
        # 而服务端的反应是含糊的失败 —— 这类问题最难往回追。
        self.session = session or GameSession(
            base_url=config.GAME_GATEWAY_URL,
            headers=UNITY_HEADERS,
            timeout=self.timeout,
            token_field=None,
        )

    # ------------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------------
    @classmethod
    def for_session(
        cls,
        session: GameSession,
        *,
        envelope: GameEnvelope | None = None,
        timeout: float | None = None,
        logger: logging.Logger | None = None,
    ) -> "GameClient":
        """用任务层的 ``GameSession`` 构造客户端，并**把会话状态注入信封**。

        :param session: 携带游戏会话状态的会话对象（``session.game_state``）。
        :param envelope: 已有信封（想继承配置指纹时传）；``None`` 表示新建。
        :param timeout: 单次请求超时；``None`` 表示用 ``config.GAME_SEND_TIMEOUT``。
        :param logger: 日志器。
        :return: 已经把 ``token``（信封字段 2）与 ``player_id``（字段 8）写好的客户端。
        :raises client.exceptions.AuthenticationError: 会话里没有可用的游戏会话状态时。

        【为什么业务任务必须走它】
        1004 的令牌**不会自己跑到**后续请求里 —— 实测它在子包 ``playerInitClass.token``，
        而不在信封元信息中。以前每个任务各写各的 ``GameClient(...)``，
        结果就是发包时信封里根本没有令牌，而服务端的抱怨只有一句含糊的"包异常"。
        统一走这里，才能在"发不出去"之前就把原因说清楚。

        【为什么不复用 ``session`` 的 HTTP 层】
        ``session`` 的 ``base_url`` 指平台 API（门户），而游戏网关是另一个域名
        （``config.GAME_GATEWAY_URL``）。图省事把同一个 ``GameSession`` 传进去，
        游戏请求就会被发到**平台域名**上 —— 这类"域名串了"的问题从报错里极难看出来。
        所以这里只**借它的状态**，HTTP 层仍按实测配置新建（请求头固定为 ``UNITY_HEADERS``）。
        """
        state = session.require_game_state()
        client = cls(envelope=envelope, timeout=timeout, logger=logger)
        state.apply_to(client.envelope)
        client.logger.debug("会话状态已注入信封：%s", state.describe())
        return client

    # ------------------------------------------------------------------
    # 组装（不发网）
    # ------------------------------------------------------------------
    def build(
        self,
        sub_packet: ProtoMessage,
        *,
        include_defaults: bool = False,
        include_activity: bool = True,
    ) -> OutgoingRequest:
        """组装一次请求，**不产生任何网络流量**。

        这是本项目的工作方式：先把「将要发出的字节」打出来，
        与抓包逐字节对齐，再真正发送。

        :param include_defaults: 子包是否连默认值一起发。
            实测两种模式都存在（握手省略、登录发送），详见
            ``models/envelope.py`` 的 ``encode_with`` 对照表。
        :param include_activity: 是否带上活动配置指纹（信封字段 16）。
            **实测 1001 与 1003 都要带**（330 字节 / 178 字节两条抓包都有它）；
            曾经流传过"1003 不能带"，那是把 HAR 里 85 字节的握手包误当成了 1003。
            这个开关只用于排查问题，正常流程保持默认。
        """
        body = self.envelope.encode_with(
            sub_packet,
            include_defaults=include_defaults,
            include_activity=include_activity,
        )
        prepared = self.session.prepare_request(
            "POST",
            "",
            data=body,
            content_type=config.GAME_CONTENT_TYPE,
            headers=UNITY_HEADERS,
            use_token=False,
        )
        return OutgoingRequest(
            method="POST",
            url=prepared["url"],
            headers=prepared["headers"],
            body=body,
        )

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------
    def post_raw(self, body: bytes) -> requests.Response:
        """POST 原始字节，返回原始响应（**不判断业务结果**）。

        .. note::
           这里**不**调用 ``raise_for_status``：网关的业务错误藏在响应体的
           protobuf 消息里（``errorCode`` 字段），HTTP 状态码只说明"网络层通不通"。
           把两者混在一起判断，会让你在「业务失败」时跑去查网络。
        """
        prepared = self.session.prepare_request(
            "POST",
            "",
            data=body,
            content_type=config.GAME_CONTENT_TYPE,
            headers=UNITY_HEADERS,
            use_token=False,
        )
        self.logger.debug("→ POST %s（%d 字节）", prepared["url"], len(body))
        response = self.session.send(prepared)
        self.logger.debug(
            "← HTTP %s（%d 字节）", response.status_code, len(response.content)
        )
        return response

    def send_raw(self, body: bytes) -> EnvelopeView:
        """发送原始字节并解析成信封视图（想手工构造字节时走它）。

        解析成功后会**顺手把响应里的令牌与配置指纹回填**到会话状态，
        免得每个调用点都要记得做这件事（忘了就会出现"下一个请求又变回新客户端"）。

        :raises client.exceptions.AbnormalPacketError: 服务端回了 99004 异常包时。
        """
        response = self.post_raw(body)
        try:
            view = parse_envelope(response.content)
        except EnvelopeError as exc:
            # 【为什么要落盘】"响应不是合法 protobuf" 这类失败光看错误信息无从下手：
            # 到底是服务端回了错误页、信封结构不同、还是响应被截断？
            # 把原始字节留下来，一次复现就能定性（目录在 tmp/，已进忽略清单）。
            dump_path = self._dump_failed_response(body, response)
            raise EnvelopeError(
                f"{exc}\n  ⚠ 已把这段原始响应写入：{dump_path}"
                f"（{len(response.content)} 字节，HTTP {response.status_code}）"
            ) from exc
        self.logger.debug("← %s", view.describe())
        # 顺序很重要：**先判异常包，再回填状态**。
        # 异常包的含义是"这次请求被拒"，把它当业务响应去回填，
        # 只会让下一个请求带着半截状态继续发错。
        self._raise_if_server_exception(
            view, request_packet_id=self._packet_id_of(body), request_size=len(body)
        )
        self.envelope.update_from(view)
        return view

    def _dump_failed_response(
        self, body: bytes, response: requests.Response
    ) -> Path:
        """把**解析失败**的响应原始字节落盘到 ``tmp/``（纯诊断，不参与业务）。

        :param body: 本次请求体（用来在文件名里带上"这是哪个请求的响应"）。
        :param response: 已收到的 HTTP 响应。
        :return: 落盘路径（错误信息里会带上它，方便直接打开看）。

        目录刻意选 ``tmp/``：它在 ``.clineignore`` 与 ``.gitignore`` 里 ——
        既不会被提交，也不会把几十 KB 的二进制灌进 AI 上下文。
        """
        directory = config.PROJECT_ROOT / "tmp"
        directory.mkdir(exist_ok=True)
        request_id = self._packet_id_of(body)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = directory / f"failed-response_{request_id}_{stamp}.bin"
        path.write_bytes(response.content)
        # 头部信息同样重要：它能区分"这是游戏服答的"还是"中间某层代理答的"
        # （实测踩过：22 字节的 {"response_code":"OK"} 根本不是游戏协议 —— 见
        #  notes/recon_findings.md 里 39007 的排查记录）。
        meta_path = path.with_suffix(".headers.txt")
        meta_path.write_text(
            f"HTTP {response.status_code}\n"
            + "\n".join(f"{name}: {value}" for name, value in response.headers.items()),
            encoding="utf-8",
        )
        self.logger.warning(
            "响应解析失败：HTTP %s，%d 字节，首 24 字节 %s —— 原始字节已存到 %s（响应头见 %s）",
            response.status_code,
            len(response.content),
            response.content[:24].hex(),
            path,
            meta_path.name,
        )
        return path

    # ------------------------------------------------------------------
    # 响应体检
    # ------------------------------------------------------------------
    def _raise_if_server_exception(
        self, view: EnvelopeView, *, request_packet_id: int | None, request_size: int
    ) -> None:
        """响应是服务端异常包（99004）时，抛一个**说得清原因**的异常。

        :param view: 解析后的响应视图。
        :param request_packet_id: 我们发出去的消息号（解不出时为 ``None``）。
        :param request_size: 请求体字节数（写进报错，便于与抓包对照）。
        :raises client.exceptions.AbnormalPacketError: 命中 99004 时。

        【为什么放在这里，而不是让每个任务自己判】
        "包异常"会出现在**每一个**业务请求上。靠各任务自己判断，漏掉一处就等于
        那次失败只能看到 `errorCode != 0` 这类含糊信息 —— 而"vCode 算错、
        少带一个字段"这种问题恰恰最需要线索。放在唯一出口处判断，漏判的可能性归零。
        """
        numbers = [view.packet_id]
        numbers.extend(number for number, _name, _payload in view.extra_packets)
        if GAME_ERROR_PACKET_ID not in numbers:
            return

        payload = view.packet_bytes(GAME_ERROR_CLASS_NAME)
        if payload is None and view.packet_id == GAME_ERROR_PACKET_ID:
            payload = view.sub_packet_bytes

        code = 0
        detail = ""
        if payload:
            try:
                error = ServerExceptionRes.decode(payload)
            except (ProtobufWireError, ValueError) as exc:
                detail = f"（异常包解析失败：{exc}）"
            else:
                code = error.errorCode
                detail = error.CustomErrorMsg

        exception_kwargs = dict(
            packet_id=request_packet_id if request_packet_id is not None else -1,
            code=code,
            detail=detail,
            context=f"本次请求体 {request_size} 字节",
        )
        if is_session_kicked(code, detail):
            # 会话被其他设备登录顶掉：与"包结构错"语义相反，可自动恢复。
            # runner 层捕获后免密重登并重试当前任务（见 SessionKickedError）。
            raise SessionKickedError(**exception_kwargs)
        raise AbnormalPacketError(**exception_kwargs)

    @staticmethod
    def _packet_id_of(body: bytes) -> int | None:
        """从**请求体**里解出信封字段 1（``packetID``），只用于报错信息。

        :param body: 完整的请求体字节。
        :return: 消息号；解不出时返回 ``None``。

        报错里带上"发出去的是哪个包"很关键：同一个错误码下，
        1001 的排查方向（账号字段）与 1003 的（区服、hamiSubNoList）完全不同。
        """
        try:
            field = fields_by_number(body).get(1, [])
        except ProtobufWireError:
            return None
        return field[0].as_int32() if field else None

    def send(
        self,
        sub_packet: ProtoMessage,
        *,
        expect: type[ProtoMessage] | None = None,
        include_defaults: bool = False,
        include_activity: bool = True,
    ) -> EnvelopeView:
        """发送业务子包并解析响应。

        :param sub_packet: 业务子包。
        :param expect: 期望的响应子包类型。给了就顺手解析校验一次 ——
            解析失败会立刻抛错，这比等业务逻辑出现"莫名其妙的现象"再回头查便宜得多。
        :param include_defaults: 子包是否连默认值一起发（见 :meth:`build`）。
        :param include_activity: 是否带上活动配置指纹（见 :meth:`build`）。
        """
        request = self.build(
            sub_packet,
            include_defaults=include_defaults,
            include_activity=include_activity,
        )
        self.logger.debug("→ %s", request.describe())
        view = self.send_raw(request.body)
        if expect is not None:
            view.decode_sub_packet(expect)
        return view

    # ------------------------------------------------------------------
    # 业务便捷方法
    # ------------------------------------------------------------------
    def handshake(self) -> VersionControlServerRes:
        """执行握手（99015 → 99016），返回服务端下发的配置。

        **不需要登录态** —— 实测该请求里没有任何鉴权字段，
        所以它是「在不碰账号数据的前提下验证整条链路」的最佳起点。

        :return: 服务端下发的配置（``serverIP`` / ``serverPort`` / ``jsonInfo`` / CDN / DNS）。
        """
        view = self.send(
            VersionControlServerPacket(
                channelPlatformType=config.CHANNEL_PLATFORM_TYPE_EL,
                clientVersion=config.GAME_CLIENT_VERSION,
                languageCode=config.GAME_LANGUAGE,
            ),
            expect=VersionControlServerRes,
        )
        return view.decode_sub_packet(VersionControlServerRes)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def close(self) -> None:
        """关闭底层会话（释放连接池）。"""
        self.session.close()

    def __enter__(self) -> "GameClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return (
            f"GameClient(url={config.GAME_GATEWAY_URL!r}, "
            f"playerId={self.envelope.player_id}, "
            f"token={'有' if self.envelope.token else '无'})"
        )
