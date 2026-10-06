"""会话级真实样本（**手工维护**，与自动生成的 ``gateway_samples.py`` 分工不同）。

【为什么要单独一个文件】
``gateway_samples.py`` 由 ``tools/extract_har.py`` 自动生成，手工往里加内容
会在下次重新生成时被覆盖。而下面这些数据有个共同特点：**它们来自 HAR 之外**——
Fiddler 导出的请求体是**有损文本**（二进制字节被替换成了替换字符），
所以只能由人在 HexView 里复制真实字节。这类"人工搬进来"的样本集中放在这里，
既不会被覆盖，也一眼能看出"这批数据不是自动来的"。

【数据来源】
1001 账号登录请求：用户从 Fiddler 的 HexView 复制的完整报文
（含 HTTP 头，此处只保留 body）。抓包时间 2026-09，游戏版本 2.2.0-el-h-win。

【身份字段的脱敏约定】
本文件原先含**真实**的 ``deviceID`` 与平台 ``userId``。这些属于账号凭据
（``socialAccount`` + ``socialOpenId`` 就是登录凭据本身），因此已替换为
**等长占位符**：

    deviceID      40 位 hex   → ``"0" * 40``
    socialAccount 38 字符 UUID → ``ER00000000-0000-0000-0000-000000000000``

等长是关键 —— 长度不变，所以 ``LOGIN_REQUEST_LENGTH`` / ``LOGIN_REQUEST_SUBPACKET_LENGTH``
那几条「与抓包逐字节等长」的断言**依然成立**。
字段编号与结构才是本文件要锁定的东西，具体取值不影响协议正确性。

【它解决了什么问题】
在拿到这份字节之前，``models/server.py`` 里 1001 的 15 个字段只能靠声明顺序推断
（编号可信、**取值未知**）。现在取值全部有了实测依据，其中最关键的一条纠正：

    ❌ 原以为要填 ``auerWebLogin_sid`` / ``auerWebLogin_token``（网页登录凭据）
    ✅ 实际 ``auerWebLogin_sid`` = 0，且 15 个字段里**根本没有** token 字段；
       真正的凭据是 ``socialAccount`` + ``socialOpenId`` = **平台 userId**
"""

from __future__ import annotations

from typing import Any, Final

#: 完整请求体长度（与抓包的 ``Content-Length: 330`` 一致）
LOGIN_REQUEST_LENGTH: Final[int] = 330

#: 请求开头 17 字节（信封元信息），逐段标注::
#:
#:     08 E9 07        packetID = 1001
#:     1A 02 2D 31     settingMd5 = "-1"
#:     50 FF×9 01      timeStampToken = -1（负数按 64 位补码，占 10 字节）
LOGIN_REQUEST_PREFIX_HEX: Final[str] = (
    "08e907"
    "1a022d31"
    "50ffffffffffffffffff01"
)

#: 实测的 ``vCode``：32 位十六进制（每次请求都会变，这里只用于断言"形态正确"）
LOGIN_REQUEST_VCODE: Final[str] = "92AEDFDDD86362AD5A1FB38C817EAC80"

#: 子包槽位的「标签 + 长度」 = ``CA 3E E4 01``::
#:
#:     标签 8010 = (1001 << 3) | 2  → 字段编号 1001（再次印证「子包槽位 = 消息号」）
#:     长度 228                      → 下面 15 个字段的总长
LOGIN_REQUEST_SUBPACKET_HEADER_HEX: Final[str] = "ca3ee401"
LOGIN_REQUEST_SUBPACKET_LENGTH: Final[int] = 228

#: 1001 子包里 15 个字段的**实测取值**（从上面那 228 字节逐字段解出）。
#:
#: 【身份字段已脱敏】
#: ``deviceID`` / ``socialAccount`` / ``socialOpenId`` 原为真实值，现已替换为
#: **等长占位符** —— 这样 ``LOGIN_REQUEST_LENGTH``（330）与子包 228 字节的
#: 「逐字节等长」断言依然成立，脱敏不会破坏任何长度校验。
#:
#: 需要真实值时请从 ``captures/`` 重新导出，勿把真实值写回本文件。
LOGIN_REQUEST_FIELDS: Final[dict[str, Any]] = {
    # 设备唯一标识 —— 与 config.DEVICE_ID 一字不差（两个体系共用同一个值）
    # 40 位 hex（原值已脱敏，长度保持不变）
    "deviceID": "0" * 40,
    # 9 = Erolabs 渠道
    "socialAccountType": 9,
    # **空** —— 登录不走密码
    "passWord": "",
    # 渠道标识（不是玩家账号）
    "customAccount": "ecchigame",
    # 平台 userId —— 真正的登录凭据（38 字符 UUID，原值已脱敏）
    "socialAccount": "ER00000000-0000-0000-0000-000000000000",
    "deviceModel": "Dell G15 5520 (Dell Inc.)",
    "osVersion": "Windows 11  (10.0.26200) 64bit",
    "clientVersion": "2.2.0-el-h-win",
    "channelPlatformType": 2,
    "advertisementID": "",
    "createAccountType": 0,
    # 与 socialAccount 同值
    "socialOpenId": "ER00000000-0000-0000-0000-000000000000",
    "languageCode": "zhcn",
    # 实测为 0 —— *不是*登录凭据（原先的推测在这里被推翻）
    "auerWebLogin_sid": 0,
    # 0 = 未 root
    "isRoot": 0,
}

#: 请求里**同时携带**的全局状态包（信封字段 16）：
#: ``SpecialActivityRefreshClass{activeMission: <指纹>, puzzleRoleGroup: ""}``
LOGIN_REQUEST_ACTIVE_MISSION_MD5: Final[str] = "2DB9B982CA690276061B90E63F3C3D38"

#: 上面那条 ``activeMission`` 指纹**与握手响应里下发的一致** ——
#: 说明客户端是"从服务端拿到指纹 → 后续请求回传"，而不是自己算的。

#: 抓包里的 ``timeStampClient``（毫秒）。有了它，我们编出来的包才能与抓包
#: **逐字节等长** —— 否则时间戳长度不同，长度对比就失去意义了。
LOGIN_REQUEST_TIMESTAMP_CLIENT: Final[int] = 1789905913885

#: 抓包里比「元信息 + 业务子包」多出来的一段字节数（``82 01 24`` + 36 = 39）。
#:
#: 它就是信封字段 16 的 ``SpecialActivityRefreshClass``（活动指纹）。
#: 单独记下这个数字有两个用处：
#:
#: 1. 长度断言可以精确到字节（``330 - 39 = 291``）；
#: 2. 提醒一件事：**信封里除了业务子包，还可能搭着全局状态包** ——
#:    当年正是靠它才发现"一次请求/响应可以带多个包"。
LOGIN_REQUEST_EXTRA_STATE_LENGTH: Final[int] = 39
