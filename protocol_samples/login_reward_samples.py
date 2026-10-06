"""登录后自动签到（每日+活动）.saz 的真实抓包样本（**由 tools/export_saz_samples.py 自动生成，请勿手工编辑**）。

【数据来源】
``captures/登录后自动签到（每日+活动）.saz``（用户实测导出；原始 SAZ 仍在 captures/ 里）。

【怎么用】
在你自己的测试里 import 下面这些常量（全部是**子包字节**）。

【导出命令（如需更新，原样重跑即可）】
    PYTHONIOENCODING=utf-8 python tools/export_saz_samples.py --match '登录后自动签到' --sessions raw/17 --prefix LOGIN_REWARD --out protocol_samples/login_reward_samples.py

【为什么存子包字节】
``ProtoMessage.decode()`` 直接吃这段（已剥掉 ``RootPacket`` 信封），
这样测试不依赖信封解析，也不会被 HTTP 头干扰。
"""

from __future__ import annotations

from typing import Final

#: raw/17 request：GetActivityLoginRewards(33055)，子包 0 B
LOGIN_REWARD17_REQUEST_HEX: Final[str] = ""

#: raw/17 response：GetActivityLoginRewardsRes(33056)，子包 764 B
LOGIN_REWARD17_RESPONSE_HEX: Final[str] = (
    "0af8010893acb8a10210809de9bd8a341880bdb1bf9334221be99693e5aeaee9"
    "babbe79086e7b9aae799bbe585a5e78d8ee58bb52a1be99693e5aeaee9babbe7"
    "9086e7b9aae799bbe585a5e78d8ee58bb5321ee4b883e5a4a9e799bbe585a5e5"
    "8fafe9a098e58f96e78d8ee58bb5e380823a124c6f67696e5f5265776172645f"
    "42475f30314209080110adc8cd5f18014209080210c3d3d35f180a420a080310"
    "8184af5f18ac02420b080410b284af5f18c0843d420a080510ef84af5f18f403"
    "42090806108a84af5f180a4209080710a984af5f1801480150025a0e63743038"
    "375f43675f5370696e65620f0a0d0892fda1b30110c3d3d35f180d0af8010894"
    "acb8a1021080adcdfe8e341880bdb1bf9334221be99693e5aeaee9babbe79086"
    "e88fafe799bbe585a5e78d8ee58bb52a1be99693e5aeaee9babbe79086e88faf"
    "e799bbe585a5e78d8ee58bb5321ee4b883e5a4a9e799bbe585a5e58fafe9a098"
    "e58f96e78d8ee58bb5e380823a124c6f67696e5f5265776172645f42475f3031"
    "4209080110aec8cd5f18014209080210c3d3d35f180a420a0803108184af5f18"
    "ac02420b080410b284af5f18c0843d420a080510ef84af5f18f4034209080610"
    "8a84af5f180a4209080710a984af5f1801480150025a0e63743038385f43675f"
    "5370696e65620f0a0d0892fda1b30110c3d3d35f18170af2010895acb8a10210"
    "80a59bde8c341880b5ff9e91342218e69c88e4b88be79b9be5aeb4e799bbe585"
    "a5e78d8ee58bb52a18e69c88e4b88be79b9be5aeb4e799bbe585a5e78d8ee58b"
    "b5321ee4b883e5a4a9e799bbe585a5e58fafe9a098e58f96e78d8ee58bb5e380"
    "823a124c6f67696e5f5265776172645f42475f30314209080110afc8cd5f1801"
    "4209080210c3d3d35f180a420a0803108184af5f18ac02420b080410b284af5f"
    "18c0843d420a080510ef84af5f18f40342090806108a84af5f180a4209080710"
    "a984af5f1801480150025a0e63743030395f43675f5370696e65620f0a0d0892"
    "fda1b30110c3d3d35f1821120f0a0d088bd58ebf0210ba97da5f1802"
)
