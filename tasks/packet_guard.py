"""只读任务的公共发包守卫：**白名单 + 禁发清单 + 消息号核对**。

【为什么抽出来】
竞技板块有四个模式，其中三个只做只读（竞技场 / 天命对决 / 失控炼成阵）。
如果每个任务各写一遍"能不能发这个包"的判断，早晚会出现三份**各不相同**的守卫 ——
而这类守卫漏一处的后果是"悄悄把次数买了/把战斗打了"。
所以把它收敛成一个函数：所有任务共用同一套判断，改动只有一处。

【三道检查，缺一不可】

1. **禁发清单**（``forbidden``）：命中即拒绝。写的是"消耗次数 / 花钻石 / 进战"这类包，
   例如竞技场的 ``26015 BuyChallengePacket``（花钻石买次数）；
2. **白名单**（``allowed``）：不在名单里就拒绝 —— 白名单是"默认禁止"，
   比黑名单安全得多（新增包默认发不出去）；
3. **消息号核对**：把"白名单里写的号"与 ``models/packet_ids.py``（由 dump.cs 生成）
   对一遍。两者不一致说明 dump 更新过而白名单没跟上，此时**宁可报错也不要发**。

另外统一做两件与实测有关的事：

- ``include_defaults=True``：真实客户端的请求会显式带上"值为 0 的字段"
  （缺了会被服务端当非客户端流量挡下 —— 详见 ``notes/glory_summit_capture.md`` 第九节）；
- 把响应不是 protobuf 的情况翻译成 :class:`client.exceptions.ProtocolError`
  （堆栈对使用者毫无意义，可读提示才有用）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping, MutableSequence
from typing import Final

import config
from client.exceptions import ProtocolError
from client.game_client import GameClient
from models.envelope import EnvelopeError, EnvelopeView
from models.game_packet import ProtoMessage
from models.packet_ids import packet_id

_LOGGER: Final[logging.Logger] = logging.getLogger("tasks.packet_guard")


def guarded_send(
    game: GameClient,
    packet: ProtoMessage,
    expect: type[ProtoMessage] | None,
    *,
    allowed: Mapping[str, int],
    forbidden: Mapping[str, int],
    record: MutableSequence[int] | None = None,
    purpose: Mapping[int, str] | None = None,
    logger: logging.Logger | None = None,
    include_defaults: bool = False,
) -> EnvelopeView:
    """发送一个**白名单内**的包；越界、禁发、消息号不符都直接抛错。

    :param game: 已带会话状态的游戏客户端。
    :param packet: 要发的业务子包。
    :param expect: 期望的响应类型（顺手解一次，错了立刻暴露）；
        **服务端可能只回元信息（子包为空）的接口传 ``None``** ——
        此时由调用方拿到 :class:`EnvelopeView` 后按需解码
        （空子包不该强行解析，见 :meth:`models.envelope.EnvelopeView.decode_sub_packet`；
        用例：登录签到的 33056，``tasks/login_signin.py``）。
    :param allowed: 本任务允许发出的 ``{类名: 消息号}``。
    :param forbidden: 明确禁发的 ``{类名: 消息号}``（消耗性/花钱/进战的包）。
    :param record: 可选，把实际发出的消息号追加进去（任务与测试用它做断言）。
    :param purpose: 可选，``{消息号: 说明}``（写进 DEBUG 日志，便于一眼看懂）。
    :param logger: 可选日志器（默认用本模块的）。
    :param include_defaults: 是否把"值为默认值"的字段也发出去。**默认关闭**，
        因为实测客户端的规则是"字段带 ``[DefaultValue]`` 就省略"（例如 ``26033`` 的
        ``openTest=""`` 不发、子包为空）；只有**确认客户端会发**的包才置 ``True``
        （例如荣耀之巅的 ``39011`` 会显式发 ``actionType=0``，见 ``tasks/top_pvp.py``）。
    :raises ValueError: 命中禁发清单、不在白名单、或消息号与生成表不一致时。
    :raises client.exceptions.ProtocolError: 响应不是合法 protobuf 时。
    """
    log = logger or _LOGGER
    packet_name = type(packet).__name__

    if packet_name in forbidden:
        raise ValueError(
            f"〔内部错误〕尝试发送被禁止的报文 {packet_name}（{forbidden[packet_name]}）"
            " —— 该包会消耗次数/花钻石/进战，本项目不允许"
        )

    expected = allowed.get(packet_name)
    if expected is None:
        raise ValueError(
            f"〔内部错误〕{packet_name} 不在本任务的白名单里"
            f"（允许：{sorted(allowed.values())}）—— 拒绝发送"
        )

    actual = packet_id(packet_name)
    if actual != expected:
        raise ValueError(
            f"〔内部错误〕{packet_name} 的消息号对不上：白名单写 {expected}，"
            f"packet_ids 表是 {actual} —— dump.cs 可能更新过，请先核对再运行"
        )

    if record is not None:
        record.append(actual)
    log.debug(
        "→ 发送 %s（%d）：%s",
        packet_name,
        actual,
        (purpose or {}).get(actual, ""),
    )

    try:
        return game.send(packet, expect=expect, include_defaults=include_defaults)
    except EnvelopeError as exc:
        # ★ 把**请求字节**也存一份：排查"blob 拦截"时必须拿我方请求与真实抓包
        # 逐字节对照（2026-10-04 的 21047 事故只剩响应没有请求，多绕了一圈）。
        try:
            # ★ 必须按**实际发送**的口径转储：``ProtoMessage.encode()`` 的默认是
            # ``include_defaults=True``，而真正上网用的是本函数的 ``include_defaults``
            # 参数。2026-10-04 素材扫荡事故里，转储显示 9 B（默认口径）、实际发出 7 B
            # （漏了 ``18 00``），靠转储对照反而把排查带偏了一圈 —— 这里改成一致。
            request_hex = packet.encode(include_defaults=include_defaults).hex()
            dump_dir = config.PROJECT_ROOT / "tmp"
            dump_dir.mkdir(exist_ok=True)
            dump_path = dump_dir / (
                f"failed-request_{actual}_{time.strftime('%Y%m%d-%H%M%S')}.hex"
            )
            dump_path.write_text(request_hex, encoding="utf-8")
            request_note = (
                f"我方请求字节（{len(request_hex) // 2} B，"
                f"include_defaults={include_defaults}）已存到 {dump_path}"
            )
        except OSError:
            request_note = "（请求字节转储失败）"
        raise ProtocolError(
            f"{packet_name}（{actual}）的响应不是游戏协议：{exc}\n"
            '  已知情形：服务端对"字节形状/内容不合预期"的请求回 22 字节'
            ' {"response_code":"OK"}（content-type: text/html，非 protobuf）——'
            "实测两种触发：① 请求里少了「值为 0 的字段」"
            "（见 notes/glory_summit_capture.md 第九节；"
            "2026-10-04 素材扫荡 11009 就是这个原因：漏了 useSweepTicket=0，"
            "实发 7 B 而抓包是 9 B，修法是发送点显式 include_defaults=True）；"
            "② 21047 一键开矿提交了服务端不认的角色编队（Q-044：元素/职业/资质"
            "条件未建模，2026-10-04 实测，请求结构已与抓包逐字节核对一致）。\n"
            f"  {request_note}；原始响应字节与响应头已存放在 tmp/（见 warning 日志）。"
        ) from exc
