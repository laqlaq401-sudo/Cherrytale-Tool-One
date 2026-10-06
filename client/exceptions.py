"""client 包的异常体系。

【为什么要自己定义一整套异常？】

初学者常见做法是用一个 ``Exception`` 应付所有情况，结果是出问题时
你只能看到一句「出错了」，完全不知道该查哪里。而逆向游戏协议时，
**失败原因是分层的**，每层的处理方式完全不同：

- 网络层断了  → 该重试，或检查代理/DNS；
- 服务端回了业务错误 → 该看错误码，不要盲目重试；
- 令牌过期     → 该重新登录；
- 协议还没确认 → 该去分析 dump.cs，而不是改代码。

把它们分成不同异常类，程序就能「对症下药」，你排查时也一眼看清层次。

所有异常都继承自 :class:`CherrytaleError`，因此调用方可以先用
``except CherrytaleError`` 兜住全部自家异常。
"""

from __future__ import annotations


class CherrytaleError(Exception):
    """本项目所有自定义异常的基类。

    好处：``except CherrytaleError`` 可以一次性捕获「我们自己抛的」全部异常，
    而不会把 ``KeyboardInterrupt`` 这类系统异常也吞掉。
    """


class NetworkError(CherrytaleError):
    """网络层失败：连接超时、DNS 解析失败、TLS 握手失败、重试耗尽等。

    出现本异常时的正确反应：检查网络、代理设置、以及 ``config.CUSTOM_DNS_IP``
    （游戏使用自建 UDP DNS，见 ``Game.Http.DnsUdpClient``）。
    """


class ProtocolError(CherrytaleError):
    """响应内容不符合预期结构：不是合法 JSON、缺少必要字段、长度异常等。

    出现本异常通常意味着**我们的理解有偏差**（字段名写错、解压/解密步骤遗漏），
    此时应该回到 dump.cs 对照，而不是加 try/except 掩盖。
    """


class ApiError(CherrytaleError):
    """服务端返回了业务层的失败结果（HTTP 通了，但业务码不是成功值）。

    :param code: 服务端返回的业务错误码。
    :param message: 服务端返回的错误描述。
    """

    def __init__(self, code: int | str, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(f"业务错误 code={code}: {message or '<无描述>'}")


class AuthenticationError(ApiError):
    """需要登录、或令牌已失效（通常对应 HTTP 401 / 403，或特定业务码）。

    出现本异常时的正确反应：调用登录流程刷新令牌，然后重试原请求。
    """


class AbnormalPacketError(ProtocolError):
    """服务端判定"这个包不合理"（``ServerExceptionRes``，实测 ``code = -3``）。

    :param packet_id: 我们**发出去**的消息号（例如 1001 / 1003）。
    :param code: 服务端给的错误码（实测 ``-3``）。
    :param detail: 服务端附加描述（``CustomErrorMsg``，实测信息量很低）。
    :param context: 我们自己的补充说明（缺哪个字段、字节长度等），排查时最有用。

    【为什么继承 ProtocolError 而不是 ApiError】
    处理方式完全不同：``ApiError`` 是"服务端懂你的包，但业务上不答应"；
    本异常是"服务端根本没看懂你的包"。前者该看业务规则，后者**必须回到字节层面
    改代码** —— 重试只会反复发送同一个错包，还会增加风控暴露面。

    【为什么要把三条排查建议写进异常信息里】
    因为这个错误码**信息量为零**（见 ``models/game_error.py`` 的说明）：
    不写明下一步该做什么，下一次遇到它的人（很可能是几周后的你自己）
    还得从零开始猜。经验证有效的排查顺序是"每步只改一个变量"的对照实验。
    """

    def __init__(
        self,
        *,
        packet_id: int,
        code: int,
        detail: str = "",
        context: str = "",
    ) -> None:
        self.packet_id = packet_id
        self.code = code
        self.detail = detail
        self.context = context
        super().__init__(self._compose())

    def _compose(self) -> str:
        """拼出给人看的多行报错（含可执行的下一步）。"""
        lines = [
            f"服务端拒绝了这个包：99004 ServerExceptionRes code={self.code}",
            f"  发出的消息号：{self.packet_id}"
            + (f"；服务端描述：{self.detail}" if self.detail else ""),
        ]
        if self.context:
            lines.append(f"  本地补充：{self.context}")
        lines.extend(
            [
                "  这个码的语义是「包结构或校验算法不对」，**重试不会有任何改变** —— "
                "请改代码，而不是加重试。",
                "  按顺序排查（每步只改一个变量，别同时改两处）：",
                "    1. 用 --dry-run 打印字节，与 captures/ 里同一条抓包逐字节对比"
                "（长度差几个字节，通常就是「零值字段该不该发」的问题）；",
                "    2. 确认 vCode 与实际发出的 timeStampClient 配对"
                "（由 crypto/vcode.py 自动算，不要手工设 envelope.v_code）；",
                "    3. 确认信封 activity（字段 16）与子包的必需字段都在"
                "（例如 1003 的 hamiSubNoList 必须是平台 userId）。",
                "  复现：python main.py run <任务名> --dry-run",
            ]
        )
        return "\n".join(lines)


class SessionKickedError(AbnormalPacketError):
    """会话令牌已被**其他设备的登录**顶掉（99004 code=-5 "token is Invalid"）。

    与 ``ABNORMAL_PACKET`` 的语义相反：这不是包结构错误，重试前只需重新登录。
    "新设备挤掉旧设备"是服务端的既定行为（多端登录同一账号必然发生），
    因此 :mod:`services.runner` 会捕获它 —— 用保存的账号免密重登一次，
    再重试当前任务，对使用者全程透明。
    """

    def _compose(self) -> str:
        lines = [
            f"会话已被其他设备的登录顶掉：99004 code={self.code}",
            f"  发出的消息号：{self.packet_id}"
            + (f"；服务端描述：{self.detail}" if self.detail else ""),
        ]
        if self.context:
            lines.append(f"  本地补充：{self.context}")
        lines.append(
            "  这不是包结构错误：任务层会用保存的账号自动重新登录并重试当前任务。"
        )
        return "\n".join(lines)


class NotConfirmedError(CherrytaleError):
    """试图调用「协议细节尚未确认」的功能。

    这是本项目刻意设置的一道闸门：在 dump.cs 分析完成、字段与签名规则确认之前，
    涉及真实请求的功能一律抛本异常，避免发出错误请求污染账号数据。
    """


class SpendBlockedError(CherrytaleError):
    """被消费开关（``client.spending.SpendPolicy``）拦下的一笔花费。

    【与 NotConfirmedError 的区别】
    两者都表示"这次不发包"，但原因完全不同，处理方式也不同：

    - :class:`NotConfirmedError` → **我们还不确定协议**，该去分析 dump.cs；
    - :class:`SpendBlockedError` → 协议没问题，是**你（或默认配置）不允许花钱**，
      该去开启开关（``--allow-diamond`` / ``CHERRYTALE_ALLOW_DIAMOND_SPEND=1``）。

    把它单独成类，是为了让日志里一眼能区分"缺资料"与"被自己拦住" ——
    后者是完全正常的状态，不该被当成故障去修。

    本异常继承 :class:`CherrytaleError`，因此 ``BaseTask.run()`` 会把它转成
    失败的 :class:`tasks.base.TaskResult`，每日流程的其它子任务继续执行。
    """
