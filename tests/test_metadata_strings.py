"""元数据字符串提取工具的单元测试。

运行方式：

    python -m tests.test_metadata_strings
    python -m pytest tests/test_metadata_strings.py -q

【为什么这些测试重要】
本工具的判定函数（域名 / IP / 密钥）最初写得过宽，实测分别刷出
294 条、66 条、1506 条噪声，把真正有用的几条彻底淹没。
下面每条 ``test_rejects_*`` 都是当时踩过的真实坑，写在这里防止回归。

真实文件那组用例还顺带验证了「精确切分」这件事：
字面量 ``432d18c9-9348-4b90-bfbf-9f2a10e1f15b`` 必须作为**独立条目**存在——
如果工具退化成 ``grep -a`` 式的连续字节搜索，它就会被粘进
``auer1342e595709065940eb77dc1fea3c87b432d18c9-...`` 这种长串里。
"""

from __future__ import annotations

import pytest

import config
from tools.extract_metadata_strings import (
    MetadataHeader,
    _display,
    classify,
    load_literals,
    looks_like_domain,
    looks_like_ip,
    looks_like_secret,
    read_header,
    search_literals,
)

#: 实测位置：与 dump.cs 同目录
METADATA_PATH = config.DUMP_CS_PATH.parent / "global-metadata.dat"

requires_metadata = pytest.mark.skipif(
    not METADATA_PATH.is_file(),
    reason="global-metadata.dat 不存在（可用环境变量 CHERRYTALE_MATERIAL_DIR 指定素材）",
)


class TestLooksLikeDomain:
    def test_accepts_real_domains(self) -> None:
        assert looks_like_domain("game-ct-labs.ecchi.xxx")
        assert looks_like_domain("api-ct-labs.ecchi.xxx")
        assert looks_like_domain("sadpki-portal-v2.ebuajk.com/login")
        assert looks_like_domain("api.example.com:8443")

    def test_ip_is_not_a_domain(self) -> None:
        """IP 应当由 :func:`looks_like_ip` 处理。

        否则 ``119.28.41.111`` 会同时出现在「域名」与「IP」两节里，
        你排查时还得自己判断哪个才是真正可连接的地址。
        """
        assert not looks_like_domain("127.0.0.1")
        assert not looks_like_domain("119.28.41.111:443")

    def test_rejects_file_extensions_and_identifiers(self) -> None:
        """实测这四类会刷出 290+ 条噪声，必须挡住。"""
        assert not looks_like_domain("Arial.ttf")
        assert not looks_like_domain("DataRelative.bytes")
        assert not looks_like_domain("DataSet.Namespace")
        assert not looks_like_domain("Alliance.eSortType")

    def test_rejects_strings_with_spaces(self) -> None:
        assert not looks_like_domain("hello world.com")


class TestLooksLikeIp:
    def test_accepts_ipv4(self) -> None:
        assert looks_like_ip("119.28.41.111")
        assert looks_like_ip("127.0.0.1")
        assert looks_like_ip("10.0.0.1:8080")

    def test_rejects_x509_oids(self) -> None:
        """X.509 OID 长得和 IP 一模一样（实测 66 条里 61 条是 OID）。"""
        assert not looks_like_ip("2.5.29.15")
        assert not looks_like_ip("1.3.6.1")
        assert not looks_like_ip("1.2.840")

    def test_rejects_out_of_range(self) -> None:
        assert not looks_like_ip("999.1.1.1")


class TestLooksLikeSecret:
    def test_accepts_hash_and_uuid(self) -> None:
        assert looks_like_secret("a" * 32)  # md5 长度
        assert looks_like_secret("a" * 64)  # sha256 长度
        assert looks_like_secret("432d18c9-9348-4b90-bfbf-9f2a10e1f15b")

    def test_accepts_keyword_identifiers(self) -> None:
        assert looks_like_secret("appSecret")
        assert looks_like_secret("sign_salt")

    def test_rejects_sentences(self) -> None:
        """带空格的几乎都是给人看的日志文案，不是参与哈希计算的盐。"""
        assert not looks_like_secret("Public Key: ")
        assert not looks_like_secret("Salt is not at least eight bytes.")
        assert not looks_like_secret("invalid salt")


class TestClassify:
    def test_splits_into_expected_groups(self) -> None:
        literals = [
            "https://api.example.com/api/x",
            "game.example.com",
            "119.28.41.111",
            "b" * 64,
            "432d18c9-9348-4b90-bfbf-9f2a10e1f15b",
            "appSecret",
            "1.0.0",
            "随便一条文案",
        ]
        groups = classify(literals)
        assert "https://api.example.com/api/x" in dict(groups["URL（完整地址，可直接使用）"])
        # URL 里的主机名会被单独收进域名组，便于只看域名清单
        assert "api.example.com" in dict(groups["域名 / 主机名"])
        assert "game.example.com" in dict(groups["域名 / 主机名"])
        assert "119.28.41.111" in dict(groups["IP 地址（可能是旧版硬编码服务器）"])
        assert "b" * 64 in dict(groups["疑似密钥：摘要 / UUID"])
        assert "appSecret" in dict(groups["疑似密钥：含具体关键词的标识符"])
        assert "1.0.0" in dict(groups["版本号"])

    def test_plain_text_lands_in_no_group(self) -> None:
        groups = classify(["随便一条文案", "another plain sentence"])
        assert all(not items for items in groups.values())


class TestDisplay:
    def test_escapes_control_characters(self) -> None:
        """不转义就会让「一条一行」的格式崩掉，grep 也会把报告当二进制。"""
        assert _display("a\nb") == "a\\nb"
        assert _display("a\tb") == "a\\tb"
        assert _display("a\\b") == "a\\\\b"

    def test_truncates_long_text(self) -> None:
        assert len(_display("x" * 300, limit=10)) == 11  # 10 个字符 + 省略号
        assert _display("x" * 300, limit=10).endswith("…")


@requires_metadata
class TestRealMetadata:
    """真实文件校验（数字来自实测，游戏更新后需要同步修改）。"""

    #: 实测：元数据版本与字面量总数
    EXPECTED_VERSION = 27
    EXPECTED_LITERAL_COUNT = 27884

    def test_header(self) -> None:
        header = read_header(METADATA_PATH)
        assert isinstance(header, MetadataHeader)
        assert header.version == self.EXPECTED_VERSION
        assert header.literal_count == self.EXPECTED_LITERAL_COUNT

    def test_known_literals_present(self) -> None:
        """已知的游戏服域名与登录地址必须能提取到（这是缺口 3 的全部依据）。"""
        literals = set(load_literals(METADATA_PATH))
        assert "game-ct-labs.ecchi.xxx" in literals
        assert "https://login-cq.ecchi.xxx/" in literals
        assert "https://api-ct-labs.ecchi.xxx/api/getServerState.jsp?version=" in literals

    def test_uuid_is_not_glued_to_neighbours(self) -> None:
        """验证「精确切分」：这条 UUID 必须独立存在。

        用 ``grep -a`` 搜二进制时它会被粘进
        ``auer1342e595709065940eb77dc1fea3c87b432d18c9-9348-4b90-bfbf-9f2a10e1f15b43743_``，
        于是根本无法判断哪一段才是真正的密钥材料。
        """
        literals = set(load_literals(METADATA_PATH))
        assert "432d18c9-9348-4b90-bfbf-9f2a10e1f15b" in literals
        assert "119.28.41.111" in literals

    def test_search_is_case_insensitive(self) -> None:
        hits = dict(search_literals(load_literals(METADATA_PATH), "ECCHI", limit=50))
        assert "game-ct-labs.ecchi.xxx" in hits


if __name__ == "__main__":  # 支持 `python -m tests.test_metadata_strings`
    raise SystemExit(pytest.main([__file__, "-v", "--no-header"]))
