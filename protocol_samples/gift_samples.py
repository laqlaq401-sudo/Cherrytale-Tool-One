"""每日免费钻石和扫荡券领取.saz 的真实抓包样本（**由 tools/export_saz_samples.py 自动生成，请勿手工编辑**）。

【数据来源】
``captures/每日免费钻石和扫荡券领取.saz``（用户实测导出；原始 SAZ 仍在 captures/ 里）。

【怎么用】
在你自己的测试里 import 下面这些常量（全部是**子包字节**）。

【导出命令（如需更新，原样重跑即可）】
    PYTHONIOENCODING=utf-8 python tools/export_saz_samples.py --match '每日免费钻石' --sessions raw/6,raw/8 --prefix GIFT --out protocol_samples/gift_samples.py

【为什么存子包字节】
``ProtoMessage.decode()`` 直接吃这段（已剥掉 ``RootPacket`` 信封），
这样测试不依赖信封解析，也不会被 HTTP 头干扰。
"""

from __future__ import annotations

from typing import Final

#: raw/6 request：GetFreeGiftPacket(37019)，子包 6 B
GIFT6_REQUEST_HEX: Final[str] = "08c1dfc8fb02"

#: raw/6 response：GetFreeGiftPacketRes(37020)，子包 20 B
GIFT6_RESPONSE_HEX: Final[str] = "080012100a0e08dcf1c078108184af5f18c9c107"

#: raw/8 request：GetFreeGiftPacket(37019)，子包 6 B
GIFT8_REQUEST_HEX: Final[str] = "08cacdc8fb02"

#: raw/8 response：GetFreeGiftPacketRes(37020)，子包 19 B
GIFT8_RESPONSE_HEX: Final[str] = "0800120f0a0d08d7e5cb78109084af5f188807"
