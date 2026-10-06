"""网关握手与配置下发报文（消息号 99015 / 99016 / 99010 / 字段16）。

【这一组报文解决什么问题】
它们是整条链路的**第一跳**：客户端先发一个「版本控制」探测包，服务端回一个
「你该连哪里、配置是什么」的说明包。也就是说 ——
**服务器地址不是硬编码在客户端的，而是握手时下发的**
（这也解释了为什么之前翻遍 dump.cs 的字符串也找不到服务器 IP）。

【实测的完整握手：88 字节请求 / 650 字节响应（见 captures/）】::

    POST https://game-ct-labs.ecchi.xxx:1893/

    ── 请求 ──
    08 C7 85 06           packetID = 99015 (VersionControlServerPacket)
    1A 02 2D 31           settingMd5 = "-1"
    50 FF×9 01            timeStampToken = -1
    60 89 E7 ...          timeStampClient = 毫秒时间戳
    72 20 <32 hex>        vCode = 32 位随机 hex（服务端不校验）
    BA AC 30 ...          子包 {channelPlatformType: 2,
                                clientVersion: "2.2.0-el-h-win",
                                languageCode: "zhcn"}

    ── 响应（一包里含 3 个子包）──
    08 C8 85 06           packetID = 99016 (VersionControlServerRes)
    82 01 44              SpecialActivityRefreshClass {activeMission, puzzleRoleGroup}
    ...                   VersionControlServerRes 子包（serverIP / serverPort / jsonInfo …）
    ...                   CheckFunctionStateRes（功能开关）

【字段编号依据】
与本项目其它报文一致：**编号 = 声明顺序**。这一条正是由本组报文实测证实的
（见 ``models/proto_fields.py`` 开头的说明）。
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import Field

from models.game_packet import ProtoMessage


class VersionControlServerPacket(ProtoMessage):
    """版本控制探测包（消息号 99015）—— 整条链路的第一个请求。

    .. note::
       实测它**不需要登录态**（请求头里没有任何鉴权字段），
       因此它是「在完全不碰账号数据的前提下验证整条链路」的最佳起点。
    """

    proto_class_name = "VersionControlServerPacket"

    #: 渠道/平台类型（实测值 2）
    channelPlatformType: int = 0
    #: 客户端版本号（实测 ``2.2.0-el-h-win``）
    clientVersion: str = ""
    #: 语言代码（实测 ``zhcn``）
    languageCode: str = ""


class VersionControlServerRes(ProtoMessage):
    """版本控制响应（消息号 99016）—— 服务端告诉你该连哪里、配置是什么。

    【为什么这个包很关键】
    它是「服务器地址 / 配置来源 / CDN 分组」的**唯一权威来源**。
    实测 ``serverIP`` 与 ``serverPort`` 填的就是网关自身（``game-ct-labs.ecchi.xxx:1893``），
    而 ``jsonInfo`` 里是那段明文 JSON 配置字典。
    """

    proto_class_name = "VersionControlServerRes"

    #: 业务错误码
    errorCode: int = 0
    #: 服务端 IP（实测就是网关域名）
    serverIP: str = ""
    #: 服务端端口（实测 1893）
    serverPort: int = 0
    #: 自定义提示语（维护公告等；非空通常意味着服务端在提示什么）
    customMsg: str = ""
    #: 是否使用 SSL（实测 ``true``）
    isUseSSL: bool = False
    #: **明文 JSON 配置字典**（见 :meth:`json_settings`）
    jsonInfo: str = ""
    #: 配置节点 ID（配合 ``api/getMd5ByNode_V2.jsp`` 使用）
    settingsNodeId: str = ""
    #: HTTP 服务 IP / 端口（备用通道）
    httpIp: str = ""
    httpPort: int = 0
    #: CDN 域名列表
    cdnList: list[str] = Field(default_factory=list)
    #: DNS 服务器列表（客户端可能绕开系统 DNS）
    dnsList: list[str] = Field(default_factory=list)
    #: 聊天服 IP / 端口（本项目的自动化用不到）
    chatIp: str = ""
    chatPort: int = 0

    # ------------------------------------------------------------------
    # 便捷解析
    # ------------------------------------------------------------------
    def json_settings(self) -> dict[str, Any]:
        """把 ``jsonInfo`` 解析成字典。

        :return: 解析结果；字段为空或不是合法 JSON 时返回**空字典**。

        【为什么解析失败不抛异常】
        这段配置只影响可选功能（战斗校验、推送、CDN 分组…）。
        因为它格式异常就让整条链路失败，属于"次要问题阻塞主线"。

        实测内容形如::

            {"UseHttpFactory":"0", "PVEVerify":"1", "WuDouVP":"1",
             "download": "https://l.hyenadata.com/s/270Iul", ...}

        .. warning::
           值都是**字符串**（``"0"`` 而不是 ``0``）。写成
           ``if settings["UseHttpFactory"]:`` 会因为非空字符串恒为真而**永远成立** ——
           要比较就写 ``== "1"``。
        """
        if not self.jsonInfo:
            return {}
        try:
            data = json.loads(self.jsonInfo)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def has_announcement(self) -> bool:
        """服务端是否随握手带了提示/公告（``customMsg`` 非空）。"""
        return bool(self.customMsg.strip())

    def summary(self) -> str:
        """一行摘要（日志用）。"""
        ssl = "SSL" if self.isUseSSL else "明文"
        return (
            f"{self.serverIP}:{self.serverPort}（{ssl}）"
            f" errorCode={self.errorCode}"
            f" CDN={len(self.cdnList)} DNS={len(self.dnsList)}"
            f" 配置项={len(self.json_settings())}"
        )


class SpecialActivityRefreshClass(ProtoMessage):
    """活动 / 拼图配置指纹（作为 ``RootPacket`` 的元信息字段下发，编号 **16**）。

    两个字段都是 **32 字符的十六进制串**（配置文件的 MD5）。客户端在后续请求里
    回传它们，服务端据此判断「你手上的配置是不是最新的」；不匹配时通常会要求
    客户端重新下载配置。

    .. note::
       这个类很容易被误解：它不是"某个接口的响应"，而是**搭在信封上的全局状态**，
       所以每个响应里几乎都能看到它。抓包里的那两段 32 字符 MD5 就是它的两个字段。
    """

    proto_class_name = "SpecialActivityRefreshClass"

    #: 活动任务配置的指纹
    activeMission: str = ""
    #: 拼图角色组配置的指纹
    puzzleRoleGroup: str = ""


class CheckFunctionStateRes(ProtoMessage):
    """功能开关列表（消息号 99010）。

    实测它跟着握手响应一起下发（同一次响应里含 3 个包）。
    载荷里是一串整数开关编码，目前看不出各项含义 ——
    先原样保留，等有需要时再对照游戏行为逐位确认。
    """

    proto_class_name = "CheckFunctionStateRes"

    #: 功能开关编码列表（整数值；含义待确认）
    functionStateList: list[int] = Field(default_factory=list)

