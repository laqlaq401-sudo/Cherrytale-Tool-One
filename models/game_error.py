"""服务端异常包（``ServerExceptionRes``, 消息号 99004）与错误码分类。

【这个文件解决什么问题：把"含糊的一串数字"变成"该做什么"】

实测（``notes/handoff_login_module.md`` 第七节）服务端拒绝请求时只会回一个：

    ServerExceptionRes{ errorCode = -3, CustomErrorMsg = "ABNORMAL_PACKET" }

**信息量为零** —— 它不告诉你是字段编号错了、vCode 算错了、还是少带了某个字段。
更要命的是它长得像"业务错误"，如果按"业务错误"处理（例如重试），
你会反复发同一个错包，既浪费时间又增加风控暴露面。

所以本模块把错误码**分类**，让上层按类决策（见 ``client/exceptions.py``）：

====================================  =========================  ===========================
分类                                   含义                        正确处理
====================================  =========================  ===========================
``GameErrorClass.ABNORMAL_PACKET``    包结构 / 校验算法不对        **不重试**，改代码，逐字节对照抓包
``GameErrorClass.BUSINESS``           其它业务码（语义未知）       不重试，记下来（可能是"已领取"这类正常结果）
====================================  =========================  ===========================

.. warning::
   **令牌失效时服务端回什么码，目前还没有实测**（已登记为 ``notes/open_questions.md``
   的 Q-010）。在查清之前，本模块**不会**把任何码猜成"需要重新登录" ——
   猜错的代价是：为了一个"字段少带了一个"的问题去重新登录，反而触发风控。

【字段编号依据】
``tools/proto_dossier.py ServerExceptionRes`` 实测输出：消息号 ``99004``，
字段 ``#1 errorCode``、``#2 CustomErrorMsg``（该类尚未进生成表
``models/proto_fields.py``，本模块的声明与它一致）。
"""

from __future__ import annotations

import enum
from typing import Final

from models.game_packet import ProtoMessage

#: 服务端异常包的消息号（``RootPacket`` 的子包槽位就用它）。
GAME_ERROR_PACKET_ID: Final[int] = 99004

#: 协议类名（信封解析时用它识别"这不是业务响应，而是异常包"）。
GAME_ERROR_CLASS_NAME: Final[str] = "ServerExceptionRes"

#: 最要命的错误码：包异常。
#:
#: 含义 = **我们发出去的包结构或校验算法不对**，重试一万次也是同一个结果。
#: 实测成因只有两个（登录阶段）：vCode 算法不对、1003 少带 ``hamiSubNoList``。
ABNORMAL_PACKET_CODE: Final[int] = -3

#: 会话被其他设备的登录顶掉（2026-10-02 实测：code=-5 且描述为 "token is Invalid"）。
SESSION_KICKED_CODE: Final[int] = -5


class ServerExceptionRes(ProtoMessage):
    """服务端异常响应（99004）。

    字段顺序即编号（``#1 errorCode``、``#2 CustomErrorMsg``）。
    """

    proto_class_name = GAME_ERROR_CLASS_NAME

    #: 错误码。``-3`` = 包异常（见 :data:`ABNORMAL_PACKET_CODE`）。
    errorCode: int = 0
    #: 服务端给的描述。**不要指望它有信息量**：实测就是 ``ABNORMAL_PACKET`` 这类短语。
    CustomErrorMsg: str = ""

    def describe(self) -> str:
        """一行摘要（排查日志用）。"""
        return f"ServerExceptionRes(code={self.errorCode}, msg={self.CustomErrorMsg or '<无>'})"


class GameErrorClass(enum.Enum):
    """错误码的分类 —— 决定"该重试、该改代码、还是该忽略"。"""

    #: 包结构 / 算法错：**不可重试**，必须改代码。
    ABNORMAL_PACKET = "abnormal_packet"
    #: 业务码（语义未实测）：不重试，记录后交给上层汇总。
    BUSINESS = "business"


def classify_error_code(code: int) -> GameErrorClass:
    """把服务端错误码分类。

    :param code: ``ServerExceptionRes.errorCode``。
    :return: :class:`GameErrorClass`。

    .. note::
       目前只有 ``-3`` 是**实测确认**的。其余码一律归入 ``BUSINESS`` ——
       这是保守选择：``BUSINESS`` 只是"不重试 + 记录"，
       而误判成"要重新登录"会带来账号风险（见模块文档的警告）。
    """
    if code == ABNORMAL_PACKET_CODE:
        return GameErrorClass.ABNORMAL_PACKET
    return GameErrorClass.BUSINESS


def is_abnormal_packet(code: int) -> bool:
    """``code`` 是否属于"包异常"（= 不该重试，该改代码）。"""
    return classify_error_code(code) is GameErrorClass.ABNORMAL_PACKET


def is_session_kicked(code: int, detail: str = "") -> bool:
    """这个 99004 是否表达"会话被其他设备的登录顶掉"。

    【为什么要求 detail 里含 token】``-5`` 这个码只在被踢场景实测过一次，
    加上服务端描述双保险，避免把未知语义的 ``-5`` 误判成可自动恢复
    （误判的代价是"对着真错误盲目重登"，掩盖问题）。
    """
    return code == SESSION_KICKED_CODE and "token" in (detail or "").lower()
