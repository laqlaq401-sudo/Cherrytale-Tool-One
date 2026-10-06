"""哈希与消息摘要工具。

【为什么需要这个模块】
手游协议最常见的鉴权方式是：把请求参数按固定规则拼成一个字符串，
末尾接上一段「盐(salt)」，整体做一次 MD5 / SHA1，得到 ``sign`` 字段随包发送，
服务端用同样算法重算一遍做校验。所以哈希是签名机制最底层的零件。

本模块只做**纯计算**：输入字符串或字节，输出摘要。
任何与游戏有关的业务规则（参数怎么排序、盐是什么）都不放在这里，
它们属于 crypto/sign.py。

【两条重要约定】
1. 所有返回十六进制字符串的函数，**一律返回小写**。
   很多服务端对大小写敏感，统一小写能避免「本地算对了、服务端却不认」的排查地狱。
2. 输入统一接受 ``str`` 或字节类，内部自动按 UTF-8 编码，
   避免初学者常踩的 ``TypeError: a bytes-like object is required``。
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
from pathlib import Path
from typing import Final, Literal

#: 允许作为输入的几种数据类型（字符串或任意字节容器）
BytesLike = str | bytes | bytearray | memoryview

#: 本模块支持的哈希算法名
HashAlgorithm = Literal["md5", "sha1", "sha256", "sha512"]

#: 字符串转字节时使用的编码。协议里几乎都用 UTF-8。
DEFAULT_ENCODING: Final[str] = "utf-8"

#: 读取大文件算哈希时，每次读多少字节（1 MB）。避免一次性把大文件塞进内存。
_FILE_CHUNK_SIZE: Final[int] = 1024 * 1024


def to_bytes(data: BytesLike, encoding: str = DEFAULT_ENCODING) -> bytes:
    """把输入统一转换成 ``bytes``。

    :param data: 字符串或任意字节容器。
    :param encoding: ``data`` 为字符串时使用的编码，默认 UTF-8。
    :return: 字节串。
    :raises TypeError: 当输入类型不受支持时（会明确告诉你收到了什么类型）。
    """
    if isinstance(data, str):
        return data.encode(encoding)
    if isinstance(data, (bytes, bytearray, memoryview)):
        return bytes(data)
    raise TypeError(
        f"不支持的数据类型：{type(data).__name__}；"
        "只接受 str / bytes / bytearray / memoryview"
    )


def _new_digest(algorithm: HashAlgorithm) -> "hashlib._Hash":
    """按算法名创建一个哈希对象。

    ``usedforsecurity=False`` 的含义：这些哈希只用于协议校验，不是安全用途，
    显式声明可以让程序在 FIPS 严格模式下也照常工作（否则 md5 可能被系统拒绝）。
    """
    try:
        return hashlib.new(algorithm, usedforsecurity=False)
    except ValueError as exc:  # 传入了不认识的算法名
        raise ValueError(
            f"不支持的哈希算法：{algorithm!r}；可选：md5 / sha1 / sha256 / sha512"
        ) from exc


def hash_digest(data: BytesLike, algorithm: HashAlgorithm = "md5") -> bytes:
    """计算摘要的**原始字节**形式（md5 为 16 字节，sha1 为 20 字节）。

    什么时候需要原始字节？当协议要把摘要直接嵌进二进制报文时
    （例如放进 protobuf 的 ``bytes`` 字段，或再套一层异或 / Base64）。
    """
    digest = _new_digest(algorithm)
    digest.update(to_bytes(data))
    return digest.digest()


def hash_hex(data: BytesLike, algorithm: HashAlgorithm = "md5") -> str:
    """计算摘要并返回**小写十六进制**字符串。

    :param algorithm: 算法名，默认 ``"md5"``（协议签名的常客）。
    """
    return hash_digest(data, algorithm).hex()


def md5_hex(data: BytesLike) -> str:
    """MD5 摘要，返回 32 位小写十六进制字符串。"""
    return hash_hex(data, "md5")


def md5_digest(data: BytesLike) -> bytes:
    """MD5 摘要，返回 16 字节原始数据。"""
    return hash_digest(data, "md5")


def sha1_hex(data: BytesLike) -> str:
    """SHA-1 摘要，返回 40 位小写十六进制字符串。"""
    return hash_hex(data, "sha1")


def sha256_hex(data: BytesLike) -> str:
    """SHA-256 摘要，返回 64 位小写十六进制字符串。"""
    return hash_hex(data, "sha256")


def sha512_hex(data: BytesLike) -> str:
    """SHA-512 摘要，返回 128 位小写十六进制字符串。"""
    return hash_hex(data, "sha512")


def hmac_digest(
    key: BytesLike,
    data: BytesLike,
    algorithm: HashAlgorithm = "sha256",
) -> bytes:
    """计算 HMAC 的原始字节形式。

    HMAC = 带密钥的哈希。相比「拼接盐再做 MD5」，它更抗长度扩展攻击，
    较新的服务端常用它校验请求完整性。
    """
    return _hmac.new(to_bytes(key), to_bytes(data), algorithm).digest()


def hmac_hex(
    key: BytesLike,
    data: BytesLike,
    algorithm: HashAlgorithm = "sha256",
) -> str:
    """计算 HMAC，返回小写十六进制字符串。"""
    return hmac_digest(key, data, algorithm).hex()


def file_hash_hex(
    path: str | Path,
    algorithm: HashAlgorithm = "md5",
    chunk_size: int = _FILE_CHUNK_SIZE,
) -> str:
    """计算**文件**的哈希值（分块读取，不会把大文件整个读进内存）。

    典型用途：校验下载下来的 AssetBundle / 配置表是否与期望一致。
    例如 ``dump.cs`` 有 34.8 MB，用本函数不会造成内存压力。

    :raises FileNotFoundError: 文件不存在时（附带绝对路径，方便定位）。
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"文件不存在：{file_path}")

    digest = _new_digest(algorithm)
    with file_path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def constant_time_compare(left: BytesLike, right: BytesLike) -> bool:
    """以**恒定时间**比较两段数据是否相等。

    为什么不用 ``==``？普通字符串比较在发现第一个不同字符时就返回，
    比较耗时与内容相关，理论上可被计时攻击逐步猜出正确值。
    校验服务端签名时用本函数更稳妥。

    :return: 相等返回 ``True``；类型不匹配或长度不同返回 ``False``（不抛异常）。
    """
    try:
        return _hmac.compare_digest(to_bytes(left), to_bytes(right))
    except TypeError:
        return False
