"""免费+付费宴席.saz 的真实抓包样本（**由 tools/export_saz_samples.py 自动生成，请勿手工编辑**）。

【数据来源】
``captures/免费+付费宴席.saz``（用户实测导出；原始 SAZ 仍在 captures/ 里）。

【怎么用】
在你自己的测试里 import 下面这些常量（全部是**子包字节**）。

【导出命令（如需更新，原样重跑即可）】
    PYTHONIOENCODING=utf-8 python tools/export_saz_samples.py --match '免费+付费' --sessions raw/09,raw/11 --prefix BBQ --out protocol_samples/bbq_samples.py

【为什么存子包字节】
``ProtoMessage.decode()`` 直接吃这段（已剥掉 ``RootPacket`` 信封），
这样测试不依赖信封解析，也不会被 HTTP 头干扰。
"""

from __future__ import annotations

from typing import Final

#: raw/09 request：BBQFriendsPacket(15003)，子包 26 B
BBQ09_REQUEST_HEX: Final[str] = "08c5cef2ee0110a6ecf902108ab7810310b1bdfa0210d4c3f602"

#: raw/09 response：BBQFriendsRes(15004)，子包 37 B
BBQ09_RESPONSE_HEX: Final[str] = (
    "0800120f0a0d08ddf1c078108284af5f18ac011a100a0e08dcf1c078108184af"
    "5f18bfc107"
)

#: raw/11 request：BBQFriendsPacket(15003)，子包 6 B
BBQ11_REQUEST_HEX: Final[str] = "08c4cef2ee01"

#: raw/11 response：BBQFriendsRes(15004)，子包 21 B
BBQ11_RESPONSE_HEX: Final[str] = "0800120f0a0d08ddf1c078108284af5f18a4021a00"
