"""邮件相关报文（消息号 13001 → 13002，再 13003 → 13004）。

【这个模块的目标只有一个：把邮件"领掉"】

本项目的邮件需求非常窄 —— **不读信、不显示标题、不展示附件**，
只要把「有哪些邮件」和「一键领取」这两件事做完：

===================================================  ==========================================
13001 ``MailGetListPacket``  （空报文）
   → 13002 ``MailGetListRes``  ``{mailList}``        列表；每封邮件里**只取它的 ID**
13003 ``MailOpenParcelPacket``  ``{mailIDList}``     把要领的邮件 ID 一次性发过去
   → 13004 ``MailOpenParcelRes``  ``{errorCode, addList}``   奖励清单
===================================================  ==========================================

【为什么邮件明细用 ``list[bytes]`` 而不是建模成一个类】

一封邮件在协议里是 ``MailClass``：

    #1 mailID (int)  #2 isRead (int)  #3 sendTime (string)  #4 message (MailMessageClass)

而 ``message`` 里还嵌着 ``from / subject / body / addList / conditions / glossary``
（``addList`` 又是 ``List<GetObjClass>``，``conditions`` 再往下还有一层）。
把这一整棵树都建模出来，代码量会翻好几倍，**而本任务一个字段都用不到** ——
奖励的权威来源是 13004 的 ``addList``，不是邮件正文。

所以这里采纳项目对待"未建模结构"的一贯做法（见 ``models/daily.py`` 的
``GetObjClass``）：**保留原始字节**，需要 ID 时用 :func:`mail_id_of` 从字节里取
``mailID``（字段 1）。好处是：

- 报文照常收发，不丢数据（线格式就是原样字节，见 ``ProtoMessage._encode_value``）；
- 将来真要显示正文，只需新增一个模型去解析这些字节，**不必重新抓包**。

【mailID 的字段编号为什么是 1】

``models/proto_fields.py`` 收录了本表全部 4 个报文类，但 ``MailClass`` 这种
数据类不在其中（它是消息体的元素，不是报文）。它的字段顺序由
``tools/extract_proto_fields.py --show MailClass`` 读出：
``mailID / isRead / sendTime / message`` —— 编号即声明顺序，故 ``mailID`` = 1。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from pydantic import Field

from models.daily import SUCCESS_ERROR_CODE, GetObjClass
from models.game_packet import ProtoMessage
from models.protobuf_wire import WIRE_VARINT, iter_fields

#: ``mailID`` 在 ``MailClass`` 里的字段编号（= 声明顺序第 1 位，见模块文档）。
MAIL_ID_FIELD: Final[int] = 1

#: ``isRead`` 在 ``MailClass`` 里的字段编号（= 声明顺序第 2 位）。
#: 取值 ``0`` = 未读、``1`` = 已读（服务端省略该字段时按默认值 0 处理）。
MAIL_IS_READ_FIELD: Final[int] = 2


def _varint_field_of(raw: bytes, number: int) -> int | None:
    """从一段消息字节里取某个 **varint** 字段的值。

    :param raw: protobuf 消息字节（``MailClass`` 或别的 data class）。
    :param number: 字段编号。
    :return: 字段值；该编号不存在（或线类型不是 varint）时返回 ``None``。

    【为什么要按字段号找，而不是"读第一个字节"】
    字段是**可以乱序、也可以缺失**的（服务端省略默认值是常态），
    所以必须用编号去定位，而不是靠位置猜。
    抽成私有函数只为一件事：``mailID`` 与 ``isRead`` 的取法必须**完全一致** ——
    两处各写一遍循环，早晚有一处会忘记判线类型。
    """
    for field in iter_fields(raw):
        if field.number == number and field.wire_type == WIRE_VARINT:
            return field.as_int64()
    return None


def mail_id_of(raw: bytes) -> int | None:
    """从**一封邮件的原始字节**里取出 ``mailID``。

    :param raw: ``MailClass`` 的 protobuf 字节（来自 ``MailGetListRes.mailList``）。
    :return: 邮件 ID；字节里找不到字段 1 时返回 ``None``。

    【为什么返回 ``None`` 而不是抛异常】
    一封解析不出 ID 的邮件不该让整个邮箱拉取失败 ——
    调用方（:meth:`MailGetListRes.mail_ids`）会跳过它，
    其余邮件照样能领。真正的"整体结构不对"会在更外层暴露
    （``parse_envelope`` / ``decode`` 会报错）。
    """
    return _varint_field_of(raw, MAIL_ID_FIELD)


def mail_is_read_of(raw: bytes) -> int | None:
    """从**一封邮件的原始字节**里取出 ``isRead``（字段 2）。

    :param raw: ``MailClass`` 的 protobuf 字节。
    :return: ``1`` = 已读、``0`` = 未读、``None`` = 服务端没下发这个字段（等同未读）。

    【为什么这个字段很关键（真机实证 2026-09-22）】

    服务端对「批量领取」是**整包原子**的：只要 ID 列表里混进一封**已读**邮件，
    整个 13003 都回 ``errorCode=-2``，**一封都不会发**（详见
    :meth:`MailGetListRes.unread_mail_ids` 的实测记录）。
    所以"这封是不是已读"直接决定"这次领取会不会全军覆没"，
    不能像 ``sendTime`` 那样忽略不管。
    """
    return _varint_field_of(raw, MAIL_IS_READ_FIELD)


class MailGetListPacket(ProtoMessage):
    """拉取邮件列表（消息号 13001）。

    .. note::
       **这是一个空报文**：``dump.cs`` 里 ``MailGetListPacket`` 没有任何
       ``[ProtoMember]`` 属性（只有一个内部用的 ``extensionObject``），
       所以它的编码结果就是**零字节**。这不是省略了什么，而是协议本来如此 ——
       ``models/proto_fields.py`` 里它的字段元组也是空的。
    """

    proto_class_name = "MailGetListPacket"


class MailGetListRes(ProtoMessage):
    """邮件列表响应（消息号 13002）。

    :param mailList: 邮件列表。**保留原始字节**（原因见模块文档），
        用 :meth:`mail_ids` 取其中的 ID。
    """

    proto_class_name = "MailGetListRes"

    #: 邮件列表（每项是一封 ``MailClass`` 的原始字节）
    mailList: list[bytes] = Field(default_factory=list)

    def mail_ids(self) -> list[int]:
        """按服务端下发顺序提取**每封**邮件的 ``mailID``（含已读邮件）。

        :return: 邮件 ID 列表；解析不出 ID 的邮件会被跳过（见 :func:`mail_id_of`）。

        .. warning::
           这个方法是"邮箱里都有哪些邮件"的如实翻译，**不要**把它直接喂给
           13003 批量领取 —— 里面混进一封已读邮件就会让整包回 ``errorCode=-2``
           （真机实证见 :meth:`unread_mail_ids`）。领取请用后者。

        为什么要单独一个方法，而不是在任务里循环取？
        这样"如何从邮件字节里拿 ID"只写一遍 —— 将来 13002 的结构若变了，
        需要改的地方就这一处。
        """
        ids: list[int] = []
        for raw in self.mailList:
            mail_id = mail_id_of(raw)
            if mail_id is not None:
                ids.append(mail_id)
        return ids

    def unread_mail_ids(self) -> list[int]:
        """按服务端下发顺序提取**未读**邮件的 ``mailID``（可直接用于 13003）。

        :return: 未读邮件 ID（``isRead != 1``）列表；解析不出 ID 的会被跳过。

        【为什么领取前必须筛掉已读邮件（2026-09-22 真机实证）】

        账号 #113 的邮箱里当时有 5 封：

            mailID        isRead   逐封发 13003 的结果
            1251764650    0        ✔ errorCode=0
            1251643518    0        ✔ errorCode=0
            1251475754    0        ✔ errorCode=0
            1251322862    1        ✘ errorCode=-2
            1250393485    1        ✘ errorCode=-2

        而**批量**把这 5 个 ID 一起发时，服务端直接回 ``errorCode=-2``、
        **一封都不发**（0 封到账）。也就是说：

        * ``-2`` 的真实含义是"你给的清单里有东西不能领"（很可能是**已领取过**的
          邮件 —— 服务端保留在列表里、但把 ``isRead`` 置 1），
          而不是我们原先猜的"整体请求格式不对"；
        * 客户端界面上的"一键领取"只挑**未读/有红点**的那些，所以玩家从没踩到过；
        * 这也解释了"命令行能领、网页领不到"的错觉：命令行那次 40 封
          **全是未读**，一次成功；网页那次 5 封里混了 2 封旧的，于是整批被拒。

        因此本方法只返回未读邮件：既避免整包被拒，也不会去动"已经领过的"那些。
        ``isRead`` 字段缺失时按未读处理（服务端省略默认值 0，就是"未读"）。
        """
        ids: list[int] = []
        for raw in self.mailList:
            if mail_is_read_of(raw) == 1:
                continue
            mail_id = mail_id_of(raw)
            if mail_id is not None:
                ids.append(mail_id)
        return ids

    def read_mail_count(self) -> int:
        """已读邮件封数（日志里用来解释"为什么只领了 N 封"）。"""
        return sum(1 for raw in self.mailList if mail_is_read_of(raw) == 1)


class MailOpenParcelPacket(ProtoMessage):
    """一键领取邮件奖励（消息号 13003）。

    :param mailIDList: 要领取的邮件 ID 列表 —— 由
        :meth:`MailGetListRes.mail_ids` 得来（**必须是服务端下发的 ID**，
        自己编一个只会被拒）。

    【"一键领取"就是这一个请求】
    协议把"批量领"做成了**单次请求 + ID 列表**，所以不存在"循环调用 N 次"的写法。

    .. note::
       ``mailIDList`` 的类型是 ``List<int>``，protobuf-net 对整数列表
       默认发 **packed**（长度前缀 + 连续 varint），编码由
       ``ProtoMessage`` 自动处理；服务端侧的两种形态它都能收。
       若实测发现服务端收不到 ID（现象是**没有错误码却也没有奖励**），
       第一处该怀疑的就是这里 —— 见 ``models/protobuf_wire.encode_repeated_ints``。
    """

    proto_class_name = "MailOpenParcelPacket"

    #: 要领取的邮件 ID 列表
    mailIDList: list[int] = Field(default_factory=list)

    @classmethod
    def for_ids(cls, mail_id_list: Sequence[int]) -> "MailOpenParcelPacket":
        """按邮件 ID 列表构造请求。

        :param mail_id_list: 邮件 ID（来源必须是 13002 响应）。

        【为什么要一个工厂方法】
        它把「入参是 ID 列表」这件事写在名字里。``mailIDList`` 这个名字与
        ``MailGetListRes.mailList``（原始字节列表）长得很像，
        手写构造时传错对象是很容易发生的事 —— 而在类型检查上
        ``list[bytes]`` 与 ``list[int]`` 都能通过，只有运行时才会出问题。
        这里顺手把元素强制转成 ``int``，让上游即使传了
        ``numpy.int64`` 之类的"看起来像整数"的值也能正常发出。
        """
        return cls(mailIDList=[int(mail_id) for mail_id in mail_id_list])


class MailOpenParcelRes(ProtoMessage):
    """领取邮件奖励的响应（消息号 13004）。

    :param errorCode: 业务错误码（0 = 成功，见 :data:`models.daily.SUCCESS_ERROR_CODE`）。
    :param addList: 本次获得的东西 —— **奖励的权威来源**（不是邮件正文里的附件）。
    """

    proto_class_name = "MailOpenParcelRes"

    #: 业务错误码（0 = 成功）
    errorCode: int = 0
    #: 本次获得的东西
    addList: list[GetObjClass] = Field(default_factory=list)

    def is_success(self) -> bool:
        """是否业务成功（判断依据见 :data:`models.daily.SUCCESS_ERROR_CODE` 的说明）。"""
        return self.errorCode == SUCCESS_ERROR_CODE

    def reward_summary(self) -> str:
        """把 ``addList`` 汇总成一行（**仅开发者日志（DEBUG）用**）。

        :return: 形如 ``道具 200000001×60、200000016×2``；没有道具时返回 ``<无道具>``。

        .. warning::
           ``itemAmount`` 是**领取后的持有量**（Q-021），不是本次增量 ——
           所以禁止拼进用户可见文案（会把账户余额念成"获得"）。
           邮件任务的结果文案只报"领了几封"，见 ``tasks/mail.py`` 的
           「结果视图纪律」。
        """
        items: list[str] = []
        for got in self.addList:
            items.extend(f"{item.itemID}×{item.itemAmount}" for item in got.itemList)
        return "道具 " + "、".join(items) if items else "<无道具>"
