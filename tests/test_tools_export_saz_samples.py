"""``tools/export_saz_samples.py``（SAZ → 测试夹具常量）的单元测试。

运行方式：

    python -m pytest tests/test_tools_export_saz_samples.py -q
    python -m tests.test_tools_export_saz_samples

【为什么值得给"一个小工具"写测试】
这个工具的输出会被当成**真机证据**写进 ``protocol_samples/market_samples.py``，
并在 ``tests/test_market_capture.py`` 里被当作"官方客户端的字节"来断言。
它一旦悄悄出错（首版就踩过：把整段 HTTP body / 信封当成子包导出），
夹具看起来仍然"有一大段 hex"，很难被发现 —— 而错误的夹具会把错的结论固化。
所以这里守住三件事：

1. **剥信封**：导出的必须是 ``RootPacket`` 里的**业务子包**；
2. **空子包如实导出**（空字符串常量，而不是省略这一条）；
3. **缺会话 / 抓包不唯一都要报错**，不静默跳过、不随便取一份。
"""

from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

from models.envelope import parse_envelope
from protocol_samples.gateway_samples import VERSION_CONTROL_REQUEST_HEX
from tools.export_saz_samples import (
    load_sessions,
    main,
    render_fixture,
    sub_packet_of,
)

#: 只有 ``packetID``、没有业务子包的信封（``08 bb 6d`` → varint 14011）
EMPTY_ENVELOPE = bytes.fromhex("08bb6d")

#: SAZ 里每个会话对应的 HTTP 报文（工具只取 ``\r\n\r\n`` 之后的部分）
HTTP_HEAD = b"POST https://game-ct-labs.ecchi.xxx:1893/ HTTP/1.1\r\nHost: x\r\n\r\n"


def http_body(envelope: bytes) -> bytes:
    """把一段信封包成 SAZ 里的 HTTP 报文。"""
    return HTTP_HEAD + envelope


def make_saz(tmp_path: Path, sessions: dict[str, bytes]) -> Path:
    """造一个迷你 SAZ：``{"04": 信封字节}`` → ``raw/04_c.txt`` + ``raw/04_s.txt``。"""
    archive = tmp_path / "mini.saz"
    with zipfile.ZipFile(archive, "w") as handle:
        for index, envelope in sessions.items():
            handle.writestr(f"raw/{index}_c.txt", http_body(envelope))
            handle.writestr(f"raw/{index}_s.txt", http_body(envelope))
    return archive


def load_module(path: Path) -> ModuleType:
    """把生成的夹具文件当成模块加载（这样断言的是"真的能跑"的代码）。"""
    spec = importlib.util.spec_from_file_location("mini_fixture", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestSubPacketOf:
    def test_strips_the_envelope(self) -> None:
        """真实信封 → 取出的正是 ``parse_envelope`` 认定的子包。"""
        envelope = bytes.fromhex(VERSION_CONTROL_REQUEST_HEX)
        assert sub_packet_of(envelope) == parse_envelope(envelope).sub_packet_bytes

    def test_empty_body_returns_empty(self) -> None:
        assert sub_packet_of(b"") == b""

    def test_unparsable_body_returns_empty(self) -> None:
        """解析不了（残缺/非 protobuf）时返回空串，交给注释去解释原因。"""
        assert sub_packet_of(b"\xff\xff\xff\xff") == b""


class TestRenderFixture:
    def test_exports_sub_packet_bytes_not_the_envelope(self, tmp_path: Path) -> None:
        """核心回归：导出的必须是**子包**，不是整段信封。

        首版工具就是在这里出的错 —— 14011 明明是空子包，却导出了一整段含
        ``vCode`` / ``settingMd5`` 的信封字节。
        """
        envelope = bytes.fromhex(VERSION_CONTROL_REQUEST_HEX)
        archive = make_saz(tmp_path, {"01": envelope})
        session = load_sessions(archive, ["raw/01"])[0]

        text = render_fixture(archive, [session], "T", "cmd")
        path = tmp_path / "fixture.py"
        path.write_text(text, encoding="utf-8")
        module = load_module(path)

        expected = parse_envelope(envelope).sub_packet_bytes.hex()
        assert module.T01_REQUEST_HEX == expected
        assert module.T01_REQUEST_HEX != envelope.hex()

    def test_empty_sub_packet_is_exported_as_empty_string(self, tmp_path: Path) -> None:
        """空子包也要导出（空串常量）——"这条请求是空包"本身就是要断言的事实。"""
        archive = make_saz(tmp_path, {"01": EMPTY_ENVELOPE})
        session = load_sessions(archive, ["raw/01"])[0]

        text = render_fixture(archive, [session], "T", "cmd")
        assert 'T01_REQUEST_HEX: Final[str] = ""' in text

    def test_header_records_source_and_command(self, tmp_path: Path) -> None:
        """文件头必须写明数据来源与再生命令（否则夹具会变成"不知从哪来的 hex"）。"""
        archive = make_saz(tmp_path, {"01": EMPTY_ENVELOPE})
        session = load_sessions(archive, ["raw/01"])[0]

        text = render_fixture(archive, [session], "T", "python tools/x.py --sessions raw/01")
        assert "mini.saz" in text
        assert "python tools/x.py --sessions raw/01" in text
        assert "请勿手工编辑" in text


class TestMainCli:
    def test_writes_the_fixture_file(self, tmp_path: Path) -> None:
        """``--out`` 写盘后，常量名默认带会话目录名前缀（``RAW``）。"""
        make_saz(tmp_path, {"04": EMPTY_ENVELOPE})
        out = tmp_path / "out.py"

        code = main(["--captures", str(tmp_path), "--sessions", "raw/04", "--out", str(out)])

        assert code == 0
        assert "RAW04_REQUEST_HEX" in out.read_text(encoding="utf-8")

    def test_missing_session_exits(self, tmp_path: Path) -> None:
        """要的会话不在抓包里 → 报错退出（不静默生成半份夹具）。"""
        make_saz(tmp_path, {"04": EMPTY_ENVELOPE})
        with pytest.raises(SystemExit):
            main(["--captures", str(tmp_path), "--sessions", "raw/09"])

    def test_ambiguous_archive_exits(self, tmp_path: Path) -> None:
        """同名匹配到多份 SAZ → 报错退出（取错抓包 = 夹具来源与声明不符）。"""
        make_saz(tmp_path, {"04": EMPTY_ENVELOPE})
        (tmp_path / "second.saz").write_bytes(b"")
        with pytest.raises(SystemExit):
            main(["--captures", str(tmp_path), "--sessions", "raw/04"])
