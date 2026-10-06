"""``RootPacket`` 信封：把「元信息 + 业务子包」装成网关能收的字节。

【为什么不给 RootPacket 建一个 882 字段的模型】
``RootPacket`` 里每个协议类都占一个字段。若照搬建模，会得到一个近千行的类，
其中 99% 的字段永远是 ``None`` —— 既没人会读，每次 dump 更新还要重新生成。
而组包时真正要填的只有两样：**几个元信息 + 一个业务子包**。
所以这里只建「元信息 + 一个动态子包槽位」两层结构。

【字段编号从哪来（**实测结论**，见 notes/gateway_traffic.md）】

===========================  ==========================  ==========================================
类别                          编号规则                    实测证据
===========================  ==========================  ==========================================
前 16 个元信息字段            1 ~ 16（与声明顺序一致）     ``packetID``=1、``settingMd5``=3、``vCode``=14
业务子包槽位                  **= 该子包自己的消息号**      ``BA AC 30`` → 99015 → VersionControlServerPacket
                                                        ``82 01 44`` → 16    → SpecialActivityRefreshClass
===========================  ==========================  ==========================================

⚠️ 子包槽位这一条是**最容易踩的坑**：``models/proto_fields.py`` 里按声明顺序算出来的
``RootPacket`` 字段编号，对子包槽位是**错的**。本模块刻意只从
``models.packet_ids`` 取消息号，避免误用。

【实测的 88 字节首包（原样拆解）】::

    08 C7 85 06                字段1  packetID = 99015 (VersionControlServerPacket)
    1A 02 2D 31                字段3  settingMd5 = "-1"      ← 尚未拿到配置指纹时的占位
    50 FF×9 01                 字段10 timeStampToken = -1    ← 未使用
    60 89 E7 DD EE 8B 34 72    字段12 timeStampClient = 毫秒时间戳
    72 20 <32 字节 ASCII>      字段14 vCode = "BA32E78A..."  ← 每次不同，服务端不校验
    BA AC 30 18 ...            字段99015 业务子包（24 字节）
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass
from typing import Final, Mapping

import config
from crypto.vcode import make_v_code
from models.game_packet import ProtoMessage
from models.handshake import SpecialActivityRefreshClass
from models.packet_ids import packet_id, packet_name
from models.protobuf_wire import (
    WIRE_LENGTH_DELIMITED,
    WIRE_VARINT,
    Field,
    ProtobufWireError,
    encode_int,
    encode_message,
    encode_string,
    fields_by_number,
)

#: ``RootPacket`` 的元信息字段编号（**实测**：与声明顺序一致）。
#:
#: 只列出我们真正会用到的那些。完整清单见 ``models/proto_fields.py`` 的 ``RootPacket``。
META_TAGS: Final[dict[str, int]] = {
    "packetID": 1,
    "token": 2,
    "settingMd5": 3,
    "teachingCheckID": 4,
    "teachingActionID": 5,
    "isStreeTest": 6,
    "tmpPlayerId": 7,
    "playerId": 8,
    "isBrowser": 9,
    "timeStampToken": 10,
    "PlayerBasicDataClass": 11,
    "timeStampClient": 12,
    "reconnect": 13,
    "vCode": 14,
    "activeMissionSettingMd5": 15,
    "SpecialActivityRefreshClass": 16,
}

#: ``vCode`` 的形态：32 个十六进制字符（= 16 字节 MD5 的十六进制）。
#: ⚠️ **它不是随机值** —— 而是由客户端算出来的，算法见 :mod:`crypto.vcode`：
#: ``UPPER(MD5(Base64(f"{消息号}{token}{settingMd5}{时间戳}") + KEY))``。
#: 早期版本曾把它当随机值发，服务端一律回 ``ABNORMAL_PACKET``。
VCODE_HEX_LENGTH: Final[int] = 32

_LOGGER: Final[logging.Logger] = logging.getLogger("models.envelope")

#: 上一次用过的时间戳，用来保证**严格递增**。
#: 连着发多个请求很容易落在同一毫秒里，而 vCode 与时间戳是绑定的 ——
#: 两个包带上完全相同的时间戳，服务端会认为是"这对值被重复使用"。
_LAST_TIMESTAMP: int = 0


def next_client_timestamp() -> int:
    """取一个**严格递增**的毫秒时间戳。

    正常情况下就是当前时间；只有"距上次不足 1 毫秒"时才 +1 微调，
    所以它与真实时间的偏差不会累积（最多慢几毫秒）。
    """
    global _LAST_TIMESTAMP
    stamp = int(time.time() * 1000)
    if stamp <= _LAST_TIMESTAMP:
        stamp = _LAST_TIMESTAMP + 1
    _LAST_TIMESTAMP = stamp
    return stamp


class EnvelopeError(ValueError):
    """信封组装或解析失败。

    最典型的两种原因：子包类名没在消息号表里（拼错），或响应开头不是 ``RootPacket``
    （可能抓到了别的服务的响应）。两者都需要立刻报出来 ——
    静默跳过只会把问题推到更难定位的地方。
    """


def new_v_code() -> str:
    """**已废弃** —— 保留仅为兼容旧调用点，请勿在新代码里使用。

    早期版本以为 ``vCode`` 是客户端随机生成的，于是随机造一个发出去，
    结果服务端一律回 ``ABNORMAL_PACKET``。真实算法见 :mod:`crypto.vcode`：
    ``UPPER(MD5(Base64(f"{消息号}{token}{settingMd5}{时间戳}") + KEY))``，
    由 :meth:`GameEnvelope.encode_with` 自动计算，**不需要调用方操心**。

    留着这个函数（而不是直接删掉）是为了让"还在调它"的旧代码立刻暴露出来 ——
    它生成的随机值现在一定过不了服务端。
    """
    return secrets.token_hex(VCODE_HEX_LENGTH // 2)


def now_ms() -> int:
    """当前时间的毫秒时间戳（``timeStampClient`` 的实测单位）。"""
    return int(time.time() * 1000)


@dataclass
class GameEnvelope:
    """一次请求的信封状态（跨请求保持，模拟同一个客户端会话）。

    【哪些字段实测必需 / 可选】
    - ``token`` / ``player_id``：登录之后才有；握手包（99015）**不带**它们；
    - ``setting_md5`` / ``active_mission_setting_md5``：服务端在响应里下发，后续请求回传；
    - ``v_code``：实测每次不同且服务端不校验 → 留空即自动生成；
    - ``time_stamp_client``：实测带毫秒时间戳 → 留 ``None`` 即自动取当前时间。
    """

    #: 登录令牌（来自 ``AccountClientInfoLoginRes``）
    token: str = ""
    #: 玩家 ID
    player_id: int = 0
    #: 配置指纹。**默认 ``"-1"`` 是实测值**：客户端在还没从服务端拿到真指纹时
    #: 就先发这个占位值（抓包的 88 字节首包里就是它）。
    #: 收到 ``SpecialActivityRefreshClass`` 后会被 :meth:`update_from` 换成真值。
    setting_md5: str = "-1"
    #: 活动配置指纹（同上，另一段）
    active_mission_setting_md5: str = ""
    #: **信封字段 16 的全局状态包** —— 实测 1001 会带上它。
    #:
    #: 【为什么它是必需的】
    #: 客户端的做法是「握手时服务端下发指纹 → 后续每个请求回传」。缺失时
    #: 服务端可能直接拒绝（实测：不带它发 1001，回来的错误响应字段结构完全不同，
    #: 会让人误以为是字段编号猜错了）。
    #: 取法：握手响应里的 ``SpecialActivityRefreshClass``（见 ``GameClient.handshake``）。
    activity: SpecialActivityRefreshClass | None = None
    #: 令牌时间戳。**默认 ``-1`` 同样是实测值**（表示"尚未使用"）。
    time_stamp_token: int = -1
    #: 客户端毫秒时间戳；``None`` → 取当前时间，并由 :func:`next_client_timestamp`
    #: 保证**严格递增**。``vCode`` 会自动与它配对算出来 —— 所以除非你同时在
    #: :attr:`v_code` 里给出配套的值，否则**不要手工设它**。
    time_stamp_client: int | None = None
    #: **通常留空**，由 :meth:`encode_with` 按客户端算法现算（见 :mod:`crypto.vcode`）。
    #: 显式赋值只用于"复现某一次抓包字节"这类场景 ——
    #: 那种情况下必须与 :attr:`time_stamp_client` 用抓包里的同一对，否则服务端会拒。
    v_code: str = ""
    #: 重连标记（正常请求为 0，即不发送）
    reconnect: int = 0
    #: 浏览器标记（正常请求为 0，即不发送）
    is_browser: int = 0

    # ------------------------------------------------------------------
    # 组装
    # ------------------------------------------------------------------
    def encode_with(
        self,
        sub_packet: ProtoMessage,
        *,
        include_defaults: bool = False,
        include_activity: bool = True,
    ) -> bytes:
        """把业务子包装进信封，返回**完整请求体**。

        :param sub_packet: 业务子包，必须设置了 ``proto_class_name``。
        :param include_defaults: 子包是否**连默认值一起发**（0 / False / 空串）。
            默认 ``False``，但这个默认值**不是随便定的** —— 实测两种模式都存在：

            ==================  ==================  ==========================================
            报文                实测模式            证据
            ==================  ==================  ==========================================
            99015 握手          **省略**默认值       88 字节里没有任何零值字段
            1001 账号登录       **发送**默认值       330 字节里显式带了 ``70 00``（sid=0）、
                                                    ``78 00``（isRoot=0）、``1a 00``（空密码）
            ==================  ==================  ==========================================

            所以调用方要按报文选：拿不准时先 ``--dry-run`` 与抓包比长度 ——
            差几个字节通常就是"该不该发这些零值字段"。

        :param include_activity: 是否把活动配置指纹（字段 16）一起发出去。
            **默认 ``True``，正常流程不要改** —— 实测 1001（330 字节）与
            1003（178 字节）**都带着它**。这个开关只用于排查问题。

        :raises EnvelopeError: 子包没有类名，或类名不在消息号表里时
            （后者通常意味着名字拼错，或它根本不是协议类）。
        """
        class_name = type(sub_packet).proto_class_name
        if not class_name:
            raise EnvelopeError(
                f"{type(sub_packet).__name__} 没有设置 proto_class_name，无法确定子包槽位编号"
            )
        try:
            slot = packet_id(class_name)
        except KeyError as exc:
            raise EnvelopeError(
                f"子包 {class_name!r} 不在消息号表里 —— "
                "它可能不是协议类，或名字拼错了（可用 models.packet_ids.search() 模糊查找）"
            ) from exc

        chunks: list[bytes] = [encode_int(META_TAGS["packetID"], slot)]

        # 元信息按**编号升序**追加：protobuf 本身不要求顺序，但升序让报文
        # 与"按编号阅读"一致 —— 排查逐字节差异时这一点很省事。
        if self.token:
            chunks.append(encode_string(META_TAGS["token"], self.token))
        if self.setting_md5:
            chunks.append(encode_string(META_TAGS["settingMd5"], self.setting_md5))
        if self.player_id:
            chunks.append(encode_int(META_TAGS["playerId"], self.player_id))
        if self.time_stamp_token:
            chunks.append(encode_int(META_TAGS["timeStampToken"], self.time_stamp_token))

        client_stamp = (
            next_client_timestamp()
            if self.time_stamp_client is None
            else self.time_stamp_client
        )
        chunks.append(encode_int(META_TAGS["timeStampClient"], client_stamp))
        # vCode 是**算出来的**（既不是随机值，也不是"抓一个固定值凑合用"）：
        #   UPPER(MD5(Base64(f"{子包消息号}{token}{settingMd5}{时间戳}") + KEY))
        # 算法与推导见 crypto/vcode.py。这里刻意**紧贴着时间戳计算** ——
        # vCode 必须与"实际写进报文的那个时间戳"配对，差一个毫秒就对不上。
        chunks.append(
            encode_string(
                META_TAGS["vCode"],
                self.v_code
                or make_v_code(slot, self.token, self.setting_md5, client_stamp),
            )
        )

        if self.reconnect:
            chunks.append(encode_int(META_TAGS["reconnect"], self.reconnect))
        if self.is_browser:
            chunks.append(encode_int(META_TAGS["isBrowser"], self.is_browser))
        if self.active_mission_setting_md5:
            chunks.append(
                encode_string(
                    META_TAGS["activeMissionSettingMd5"], self.active_mission_setting_md5
                )
            )

        # 字段 16：活动配置指纹。实测 1001 会带上它（抓包里是 ``82 01 24`` + 36 字节）。
        #
        # ⚠️ 实测（2026-09-21，荣耀之巅实战）：真实客户端**总是**把 ``puzzleRoleGroup``
        # 发出来，**哪怕是空串** —— 抓包里字段 16 的字节就是
        # ``0A 20 <32 字节 activeMission> 12 00``（最后两字节 = 空的 puzzleRoleGroup）。
        #
        # 少了那个 ``12 00`` 时，服务端会把**战斗类请求**挡在门外：回 22 字节
        # ``{"response_code":"OK"}``（``content-type: text/html``，根本不是游戏协议），
        # 而读类请求（39001/39013/39019）照常通过 —— 现象正是"读得到状态、打不了架"。
        # 四组对照实验（各发一次 39011，见 notes/glory_summit_capture.md 第九节）：
        #
        #   A 真指纹 + 带空字段              → ✔ 成功
        #   B 指纹 = -1 + 带空字段           → ✔ 成功（**所以 settingMd5 无关**）
        #   C 真指纹 + **不带**空字段        → ✘ 被拦（**决定性条件**）
        #   D 真指纹 + 带空字段 + 带 playerId → ✔ 成功（playerId 也无关）
        #
        # 历史教训"两个字段都发会 ABNORMAL_PACKET"指的是发**非空**的 puzzleRoleGroup
        # （曾经把服务端下发的真值也回传了）。两者不矛盾：**只发空串**即可。
        if self.activity is not None and include_activity:
            chunks.append(
                encode_message(
                    META_TAGS["SpecialActivityRefreshClass"],
                    SpecialActivityRefreshClass(
                        activeMission=self.activity.activeMission,
                        puzzleRoleGroup="",
                    ).encode(include_defaults=True),
                )
            )

        # 子包槽位：编号 = 消息号（**实测**，不是声明顺序）
        chunks.append(
            encode_message(slot, sub_packet.encode(include_defaults=include_defaults))
        )
        return b"".join(chunks)

    # ------------------------------------------------------------------
    # 状态回填
    # ------------------------------------------------------------------
    def update_from(self, view: "EnvelopeView") -> None:
        """用响应里的元信息回填会话状态。

        【为什么必须做】
        令牌与两段配置指纹**只在响应里出现**，后续请求要回传它们。
        不回填的话，每个请求都会被服务端当成"一个刚上线的新客户端"，
        轻则被要求重新握手，重则直接拒绝。
        """
        token = view.text("token")
        if token is not None:
            self.token = token

        setting_md5 = view.text("settingMd5")
        if setting_md5 is not None:
            self.setting_md5 = setting_md5

        active_md5 = view.text("activeMissionSettingMd5")
        if active_md5 is not None:
            self.active_mission_setting_md5 = active_md5

        player_id = view.integer("playerId")
        if player_id is not None:
            self.player_id = player_id

        # `timeStampToken`（字段 10）：**进区登录（1004）响应**里由服务端下发，
        # 之后每个业务请求都要回传（实测抓包里所有业务请求都带同一个值）。
        # ⚠️ 实测教训：不带它时读类请求仍能通过，但**战斗类请求会被前端拦下**，
        # 返回 22 字节的 `{"response_code":"OK"}`（`content-type: text/html`）——
        # 那不是游戏协议，客户端也解析不了（"response_code" 在客户端字符串池里 0 命中）。
        stamp_token = view.integer("timeStampToken")
        if stamp_token is not None:
            self.time_stamp_token = stamp_token

        # 活动指纹：服务端在（几乎）每个响应里都会下发，客户端则要把它回传给后续请求。
        # 解不出来时**不中断** —— 它只是配置指纹，缺了顶多被要求重新同步一次，
        # 而为此让整条链路失败显然不划算。
        fingerprint = view.packet_bytes("SpecialActivityRefreshClass")
        if fingerprint:
            try:
                self.activity = SpecialActivityRefreshClass.decode(fingerprint)
            except (ProtobufWireError, ValueError) as exc:
                _LOGGER.debug("活动指纹解析失败，已忽略：%s", exc)

        # 把"这个响应带了哪些元信息字段"打到 DEBUG：排查"服务端下发的某个 meta 我们没接住"
        # 这类问题时，一行日志能省掉一次抓包（例如 `timeStampToken` 至今未接）。
        _LOGGER.debug("响应元信息字段：%s", sorted(view.meta))

    def clone(self) -> "GameEnvelope":
        """复制一份信封状态（用于"先试发一次，失败不影响主会话"的排查场景）。"""
        return GameEnvelope(
            token=self.token,
            player_id=self.player_id,
            setting_md5=self.setting_md5,
            active_mission_setting_md5=self.active_mission_setting_md5,
            time_stamp_token=self.time_stamp_token,
            time_stamp_client=self.time_stamp_client,
            v_code=self.v_code,
            reconnect=self.reconnect,
            is_browser=self.is_browser,
            activity=self.activity,
        )




# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EnvelopeView:
    """解析后的信封视图（响应侧）。

    :param packet_id: 消息号（``RootPacket`` 字段 1）。
    :param class_name: 消息号对应的协议类名；表里没收录时为 ``None``。
    :param sub_packet_bytes: 业务子包的原始字节（空表示该响应不含子包）。
    :param meta: 元信息字段（名字 → :class:`~models.protobuf_wire.Field`）。
    :param unknown_numbers: 本表不认识的字段编号 —— 服务端比客户端新时会出现，
        只记不报错（详见 :func:`parse_envelope` 的说明）。
    """

    packet_id: int
    class_name: str | None
    sub_packet_bytes: bytes
    meta: Mapping[str, Field]
    #: **服务端主动下发的额外子包**：``((消息号, 类名, 字节), ...)``。
    #:
    #: 实测一个响应里可以同时带好几个包。例如那 650 字节的握手响应里就有三个：
    #: ``VersionControlServerRes``（主响应）+ ``SpecialActivityRefreshClass``（配置指纹）
    #: + ``CheckFunctionStateRes``（功能开关，就是载荷里那段明文 JSON 字典的来源）。
    #: 丢掉它们会让"服务端明明发了配置，我们却看不到"。
    extra_packets: tuple[tuple[int, str, bytes], ...] = ()
    unknown_numbers: tuple[int, ...] = ()
    #: 本视图是否来自「容忍截断」的解析
    #: （即 ``parse_envelope(data, tolerate_truncation=True)``）。
    #:
    #: 为 ``True`` 时 :meth:`decode_sub_packet` 也默认宽容：否则会出现
    #: 「外层允许截断、内层却严格要求完整」这种自相矛盾，报错点还会跑偏。
    truncation_tolerant: bool = False

    # ---- 便捷取值 --------------------------------------------------------
    def text(self, name: str) -> str | None:
        """取字符串型元信息；字段不存在或类型不符时返回 ``None``。"""
        entry = self.meta.get(name)
        if entry is None or entry.wire_type != WIRE_LENGTH_DELIMITED:
            return None
        return entry.as_string()

    def integer(self, name: str) -> int | None:
        """取整型元信息；字段不存在或类型不符时返回 ``None``。"""
        entry = self.meta.get(name)
        if entry is None or entry.wire_type != WIRE_VARINT:
            return None
        return entry.as_int32()

    def packet_bytes(self, class_name: str) -> bytes | None:
        """按协议类名取额外子包的字节（找不到返回 ``None``）。

        用途举例：握手响应里的功能开关在 ``CheckFunctionStateRes`` 中，
        取出来丢给自己的模型解析即可。
        """
        for _number, name, payload in self.extra_packets:
            if name == class_name:
                return payload
        return None

    def decode_sub_packet(
        self,
        model: type[ProtoMessage],
        *,
        tolerate_truncation: bool | None = None,
    ) -> ProtoMessage:
        """把业务子包按指定模型解析。

        :param model: 子包对应的模型类（例如 ``VersionControlServerRes``）。
        :param tolerate_truncation: 是否容忍数据被截断。``None``（默认）表示
            跟随本视图的 :attr:`truncation_tolerant`（来自 ``parse_envelope`` 的入参）。
        :raises EnvelopeError: 该响应不含子包，或子包字节不符合模型时。
        """
        if not self.sub_packet_bytes:
            raise EnvelopeError(
                f"{self.class_name or self.packet_id} 的响应不含业务子包 —— "
                "有些接口只用元信息应答（例如心跳/同步），此时不该强行解析"
            )
        relaxed = self.truncation_tolerant if tolerate_truncation is None else tolerate_truncation
        try:
            return model.decode(self.sub_packet_bytes, tolerate_truncation=relaxed)
        except (ProtobufWireError, ValueError) as exc:
            raise EnvelopeError(f"子包无法按 {model.__name__} 解析：{exc}") from exc

    def describe(self) -> str:
        """一行摘要（日志用）。"""
        names = [self.class_name or f"<未收录 {self.packet_id}>"]
        names.extend(name for _number, name, _payload in self.extra_packets)
        meta_names = "、".join(sorted(self.meta)) or "<无>"
        suffix = f" 未知字段={list(self.unknown_numbers)}" if self.unknown_numbers else ""
        return (
            f"包: {' + '.join(names)} | 主响应子包 {len(self.sub_packet_bytes)}B | "
            f"元信息: {meta_names}{suffix}"
        )


def parse_envelope(data: bytes, *, tolerate_truncation: bool = False) -> EnvelopeView:
    """把一段响应字节解析成 :class:`EnvelopeView`。

    :param tolerate_truncation: 数据被截断时是否宽容处理（默认严格）。
        置 ``True`` 时「能读多少读多少」，并且容许子包**比声明的短**。

        【什么时候需要打开它】
        ``protocol_samples/gateway_samples.py`` 里的大响应用的是**前 256 字节预览**
        （抓包导出脚本为控制体积刻意截断，见 ``tools/extract_har.py``）。
        例如那条 8872 字节的登录响应：区服列表包声明 8781 字节，
        预览里只有开头 —— 但**前 3 个区服是完整的**，它们就在预览里。
        严格模式下这会被判成"非法 protobuf"，白丢掉能用的信息。

        .. warning::
           解析**真实完整**响应时不要打开它：
           "字段对齐错了" 的典型症状就是"读到一半失败"，
           宽容模式会让这种错误看起来一切正常。

    :raises EnvelopeError: 响应为空、不是合法 protobuf、或开头不是 ``RootPacket``
        （例如误把别的服务的响应交给它）。

    【未知字段为什么只记不报错】
        服务端版本比客户端新是常态。因为多了一个不认识的字段就让整包解析失败，
        属于自找麻烦 —— 记在 ``unknown_numbers`` 里，需要时一眼就能看到。
    """
    if not data:
        raise EnvelopeError("响应体为空，无法解析")

    try:
        grouped = fields_by_number(data, stop_at_truncation=tolerate_truncation)
    except ProtobufWireError as exc:
        raise EnvelopeError(f"响应不是合法 protobuf：{exc}") from exc

    id_fields = grouped.get(META_TAGS["packetID"], [])
    if not id_fields or id_fields[0].wire_type != WIRE_VARINT:
        raise EnvelopeError(
            "响应开头不是 packetID（RootPacket 的字段 1，应为 `08 <varint>`）—— "
            "可能不是本网关的响应，或外层结构与预期不同"
        )
    number = id_fields[0].as_int32()

    sub_packet_bytes = b""
    for candidate in grouped.get(number, []):
        if candidate.wire_type == WIRE_LENGTH_DELIMITED:
            sub_packet_bytes = candidate.as_bytes()
            break

    meta: dict[str, Field] = {}
    for name, tag in META_TAGS.items():
        if tag == number:
            continue
        entries = grouped.get(tag)
        if entries:
            meta[name] = entries[0]

    # 额外子包：响应里**其它已知消息号**的字段。
    # 服务端会一次下发多个包（实测握手响应里就有 3 个），漏掉它们会让人以为
    # "服务端没发配置"，从而去错误的地方找原因。
    extra: list[tuple[int, str, bytes]] = []
    for other_number in sorted(grouped):
        if other_number == number:
            continue
        other_name = packet_name(other_number)
        if other_name is None:
            continue
        for entry in grouped[other_number]:
            if entry.wire_type == WIRE_LENGTH_DELIMITED:
                extra.append((other_number, other_name, entry.as_bytes()))

    # 未知编号：只统计「既不是元信息、也不是已知消息号」的那些
    unknown = tuple(
        sorted(
            n
            for n in grouped
            if n != number and n not in META_TAGS.values() and packet_name(n) is None
        )
    )

    return EnvelopeView(
        packet_id=number,
        class_name=packet_name(number),
        sub_packet_bytes=sub_packet_bytes,
        meta=meta,
        extra_packets=tuple(extra),
        unknown_numbers=unknown,
        truncation_tolerant=tolerate_truncation,
    )
