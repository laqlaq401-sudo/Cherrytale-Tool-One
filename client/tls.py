"""TLS 信任库配置 —— 一次真实踩坑的完整记录。

【问题现象（2026-09-20 实测）】
对 ``https://sadpki-portal-v2.ebuajk.com`` 发请求时：

- ``curl``（Windows schannel）→ 正常返回 HTTP 400
- ``openssl s_client`` → ``Verify return code: 0 (ok)``
- **Python (requests + certifi) → 稳定失败**，4/4 次全部报：

.. code-block:: text

    SSLError(SSLCertVerificationError(1,
      '[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
       unable to get local issuer certificate (_ssl.c:1081)'))

【排查过程】
先用 Python 打印「服务器实际下发的证书链」（``SSLSocket.get_unverified_chain()``）：

.. code-block:: text

    [0] CN=sadpki-portal-v2.ebuajk.com   ← 由 WE1 签发
    [1] CN=WE1 (Google Trust Services)   ← 由 GTS Root R4 签发
    [2] CN=GTS Root R4                   ← 由 **GlobalSign Root CA** 签发 ★问题在这

并且确认 ``certifi`` 的证书包里**确实有** ``GTS Root R4``。

结论：第 [2] 张是**交叉签名版**的 GTS Root R4（而非自签名版），
要建成完整链路就得靠 ``GlobalSign Root CA``。Python 3.14 自带的
**OpenSSL 3.0.18** 在这种「服务器提供交叉签名根」的场景下不会去系统库里补链，
于是直接判定失败；而浏览器 / curl / schannel 都能正确处理。

**这不是网络问题、不是中间人攻击、也不是我们的代码写错了** ——
它纯粹是「Python 的证书信任库与链构建策略」和浏览器不一致导致的。

【解决办法】
使用 pyca 生态的 ``truststore``：把证书校验交给**操作系统信任库**
（Windows = CryptoAPI / schannel）。官方文档明确列出它相比 certifi 的优势：

- 证书随系统自动更新（不用等 certifi 发新版）；
- **自动补齐缺失的中间证书**（正是本例需要的）；
- 支持 CRL 吊销检查。

【为什么不干脆 verify=False】
关掉证书校验等于允许任何人替换服务器证书；而我们随后就会在请求头里
带上 ``Authorization`` 令牌 —— 那等于把账号凭据直接交给攻击者。
**这是绝对不能做的妥协**，宁可多装一个 18 KB 的纯 Python 包。
"""

from __future__ import annotations

import logging
from typing import Final

import config

_LOGGER: Final[logging.Logger] = logging.getLogger("client.tls")

#: 是否已经注入过系统信任库。``truststore.inject_into_ssl()`` 会替换
#: ``ssl.SSLContext`` 等对象，重复调用没有必要，因此这里自己做一次幂等保护。
_INJECTED: bool = False


def use_system_trust_store() -> bool:
    """让 Python 的 TLS 校验改用操作系统信任库（幂等）。

    :return: ``True`` 表示当前已生效（本次注入成功，或之前已注入）；
        ``False`` 表示未启用（配置关闭）或 ``truststore`` 不可用。

    行为说明：

    - 配置 ``config.USE_SYSTEM_TRUST_STORE`` 为 ``False`` 时直接返回 ``False``，
      保持 Python 原生（certifi）行为；
    - ``truststore`` 未安装时**只记一条警告并返回 False**，不抛异常 ——
      因为对不涉及该证书链的请求，certifi 仍然够用，
      不该让整个程序起不来。

    .. note::
       ``truststore`` 官方文档提醒：``inject_into_ssl()`` 只应由
       **应用/脚本**调用，不应由库调用（否则会影响依赖方的行为）。
       本项目是应用，因此在这里调用是正确用法。
    """
    global _INJECTED

    if _INJECTED:
        return True

    if not config.USE_SYSTEM_TRUST_STORE:
        _LOGGER.debug("已按配置跳过系统信任库注入（CHERRYTALE_USE_SYSTEM_TRUST_STORE=0）")
        return False

    try:
        import truststore
    except ImportError:
        _LOGGER.warning(
            "未安装 truststore，继续使用 certifi 信任库。\n"
            "  若遇到 'unable to get local issuer certificate'，请执行：\n"
            "      python -m pip install -r requirements.txt"
        )
        return False

    truststore.inject_into_ssl()
    _INJECTED = True
    _LOGGER.debug("已启用操作系统证书信任库（truststore）")
    return True


def is_using_system_trust_store() -> bool:
    """当前进程是否已启用系统信任库（供自检与测试查询）。"""
    return _INJECTED
