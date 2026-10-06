"""网关握手任务（99015 → 99016）—— 整条链路的「第一跳」。

【为什么先做这个任务】
它是**唯一不需要账号凭据**的网关请求：实测请求头里没有任何鉴权字段。
所以整个项目的推进顺序是::

    握手（本任务）→ 登录（需要凭据）→ 日常任务（需要登录态）

只要本任务能跑通并解出服务端配置，就同时证明了：
**地址对、协议对、字节格式对、TLS 与代理设置也对**。
后面任何问题都能顺着这个基线往下查 —— 这是"先建立可信基线"的典型做法。

【它做什么】
1. 组一个 ``VersionControlServerPacket``（渠道 / 版本 / 语言三个字段，取值全部来自实测）；
2. 发给网关，收 ``VersionControlServerRes``；
3. 解出服务端下发的配置：服务器地址与端口、明文 JSON 配置、CDN / DNS 列表。

【怎么用】

    python main.py run handshake --dry-run   # 只打印将要发送的字节（不联网）
    python main.py run handshake             # 真发（收到 99016 即链路打通）
"""

from __future__ import annotations

import config
from client.game_client import GameClient
from models.handshake import VersionControlServerPacket
from tasks.base import BaseTask, TaskResult, register_task


@register_task
class HandshakeTask(BaseTask):
    """握手：向网关索取服务端配置（99015 → 99016）。"""

    name = "handshake"

    #: **不需要登录态** —— 这是它能当"链路试金石"的前提
    requires_auth = False

    def execute(self) -> TaskResult:
        """组装并（按需）发送握手包。

        :raises client.exceptions.NetworkError: 网络失败时（由基类转成失败结果）。
        """
        packet = VersionControlServerPacket(
            channelPlatformType=config.CHANNEL_PLATFORM_TYPE_EL,
            clientVersion=config.GAME_CLIENT_VERSION,
            languageCode=config.GAME_LANGUAGE,
        )

        # 用 with 确保连接池会被释放：调试时这个任务可能被反复调用
        with GameClient(logger=self.logger) as client:
            request = client.build(packet)
            self.logger.info("目标 %s（请求体 %d 字节）", request.url, len(request.body))

            if self.dry_run:
                # 演练模式：把「将要发出的东西」完整打出来，**一个字节都不发**
                print(request.describe())
                print()
                print("—— 以上为演练输出，未发送任何请求 ——")
                return TaskResult(
                    task=self.name,
                    ok=True,
                    message=f"演练完成：请求体 {len(request.body)} 字节，未发送",
                    data={
                        "url": request.url,
                        "body_size": len(request.body),
                        "body_prefix": request.preview_bytes(16),
                    },
                )

            server_config = client.handshake()

        summary = [
            f"网关确认 {server_config.serverIP}:{server_config.serverPort}"
            f"（{'SSL' if server_config.isUseSSL else '明文'}）",
            f"errorCode={server_config.errorCode}",
        ]
        if server_config.has_announcement():
            summary.append(f"服务端提示：{server_config.customMsg}")
        if server_config.cdnList:
            summary.append(f"CDN {len(server_config.cdnList)} 条")
        if server_config.dnsList:
            summary.append(f"DNS {server_config.dnsList}")

        settings = server_config.json_settings()
        if settings:
            picked = "、".join(sorted(settings)[:6])
            summary.append(f"配置项 {len(settings)} 个（{picked}…）")

        return TaskResult(
            task=self.name,
            ok=True,
            message="；".join(summary),
            data={
                "server_ip": server_config.serverIP,
                "server_port": server_config.serverPort,
                "settings": settings,
            },
        )
