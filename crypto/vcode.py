"""``vCode`` 的生成算法（**逆向还原**，已用两个真实抓包样本双向验证）。

【它是什么】
信封 ``RootPacket`` 的字段 14 ``vCode``（32 位十六进制大写）。服务端会校验它，
算错就回 ``ServerExceptionRes + ABNORMAL_PACKET`` —— 而错误信息里**看不出任何线索**，
所以这一步曾经卡了很久（那一对战"每包不同、还必须配对"的值）。

【算法】
反汇编 ``GameAssembly.dll`` 得到的完整调用链（RVA 见本地 notes/vcode_algorithm.md）::

    NetWorkModule.GetRootPacketVCode(RootPacket)
        └─ GetGlobalVerifyData(object[] param)
              └─ GetGlobalVCode(string data, string key)
                    └─ GetMd5Hash(byte[] data)

翻译成一句话::

    source = f"{packetID}{token}{settingMd5}{timeStampClient}"   # 直接相连，无分隔符
    vCode  = UPPER( MD5( Base64(source) + KEY ) )

其中 ``KEY`` 取自 ``GetGlobalVCode`` 里的静态常量 ——
它是**游戏客户端写死的常量**（服务端要用同一个才能验证），不是账号/设备相关的值。
为保证公开仓库不携带该常量，它改由 ``config.GAME_V_CODE_KEY`` 从本机注入
（``config_local.py`` 或环境变量 ``CHERRYTALE_V_CODE_KEY``），详见 ``config.py`` 注释。

【验证样本】（``tests/test_vcode.py`` 会把这两条锁死；需先配置 KEY）
    make_v_code(1001, "", "-1", 1789905913885) == "92AEDFDDD86362AD5A1FB38C817EAC80"
    make_v_code(1003, "", "-1", 1789908064585) == "93ABA44B13800136EBED3CF097981137"
"""

from __future__ import annotations

import base64

import config
from crypto.hash import md5_hex

#: 密钥。放在 ``config`` 里而不是直接写字面量，是为了将来万一需要改动时
#: 只有一个修改点（也可以经环境变量覆盖，便于比对实验）。
V_CODE_KEY: str = config.GAME_V_CODE_KEY

_KEY_HINT = (
    "vCode 密钥未配置。请在 config_local.py 写入 GAME_V_CODE_KEY，"
    "或设置环境变量 CHERRYTALE_V_CODE_KEY。\n"
    "（该常量属逆向结论，不随公开仓库分发；推导见本地 notes/vcode_algorithm.md）"
)


def make_v_code(
    packet_id: int,
    token: str | None,
    setting_md5: str | None,
    time_stamp_client: int,
) -> str:
    """按游戏客户端的算法生成 ``vCode``。

    :param packet_id: 本次要发的业务消息号（信封字段 1），如 ``1001`` / ``1003``。
    :param token: 会话令牌（信封字段 2）。未登录时是空串或 ``None``。
    :param setting_md5: 配置指纹（信封字段 3）。没从服务端拿到真值前是 ``"-1"``。
    :param time_stamp_client: 客户端毫秒时间戳（信封字段 12）。
    :returns: 32 位十六进制大写字符串。
    :raises RuntimeError: 未配置 ``GAME_V_CODE_KEY`` 时 —— 宁可显式失败，
        也不要静默算出一个错的 vCode 让服务端回一句笼统的 ``ABNORMAL_PACKET``。

    .. warning::
       这四个参数里任何一个与实际发出去的报文不符，算出的 vCode 就是错的，
       而服务端只会回一句笼统的 ``ABNORMAL_PACKET``。
       所以调用点应当**直接从"即将被编码进信封的那个对象"上取值**，
       不要各处自己拼一遍 —— 这个函数的四个参数与
       :meth:`models.envelope.GameEnvelope.encode_with` 里的取值点是一一对应的。
    """
    if not V_CODE_KEY:
        raise RuntimeError(_KEY_HINT)
    source = f"{packet_id}{token or ''}{setting_md5 or ''}{time_stamp_client}"
    encoded = base64.b64encode(source.encode("utf-8")).decode("ascii")
    return md5_hex(encoded + V_CODE_KEY).upper()
