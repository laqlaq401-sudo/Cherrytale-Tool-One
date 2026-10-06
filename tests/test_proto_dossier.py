"""``tools/proto_dossier.py`` 的单元测试。

【为什么用「自造的小 dump.cs」而不是真文件】
    真 ``dump.cs`` 有 34 MB。测试必须**快**、而且要在没放素材的机器上也能跑。
    所以这里构造一份十几行的迷你 dump.cs，只保留本工具依赖的书写形式
    （``public class X`` / ``[ProtoMemberAttribute]`` + 属性 / ``) { }`` 方法体）。
    这样断言的是**解析与分类规则**，而不是素材内容 —— 素材变了测试也不会假报警。

【这些用例守住了什么】
    1. 词干提取（决定了「能不能抓到 OnXxx 回调」）；
    2. 引用分类与过滤（决定了输出是几十行还是几千行 = 省不省 Token）；
    3. 「表未收录的类」能现场从 dump.cs 抽取字段（不必手工往生成表里加）；
    4. 输出行数上限真的生效（省 Token 的硬约束不能被悄悄破坏）；
    5. 待确认清单的解析（``notes/open_questions.md`` 改了格式要立刻发现）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.proto_dossier import (
    DossierEntry,
    Question,
    REF_TEXT_WIDTH,
    Reference,
    build_entries,
    classify_reference,
    config_keywords,
    find_config_candidates,
    main,
    match_questions,
    normalize_targets,
    parse_open_questions,
    refine_kind,
    reference_sort_key,
    render_entries,
    scan_references,
    stem_of,
    trim_line,
)

# ---------------------------------------------------------------------------
# 夹具：一份迷你 dump.cs（书写形式与真文件一致）
# ---------------------------------------------------------------------------
FAKE_DUMP: str = """// Namespace: com.auer.game.protobuf
public class GetFreeGiftPacket // TypeDefIndex: 1
{
    [ProtoMemberAttribute]
    public long giftId { get; set; }
}

public class ProbeOnlyPacket // TypeDefIndex: 2
{
    [ProtoMemberAttribute]
    public int probeId { get; set; }
    [ProtoMemberAttribute]
    public string probeName { get; set; }
}

public class GiftPackageNetModule // TypeDefIndex: 3
{
    public void RequesGetFreeGiftPacket(GetFreeGiftPacket iGiftPackage, Action cb) { }
}

public class GiftPackage_Home_Remake_UIView // TypeDefIndex: 4
{
    public void OnGetFreeGiftRes(GetFreeGiftPacketRes iRes) { }
    public int unrelatedField { get; set; }
}

public class SomeSender // TypeDefIndex: 5
{
    public void Send() { SendPacket<GetFreeGiftPacket>(null); }
}

public class RootPacket // TypeDefIndex: 6
{
    public GetFreeGiftPacket get_GetFreeGiftPacket() { }
    public void set_GetFreeGiftPacket(GetFreeGiftPacket value) { }
}

public class GeneratedHolder // TypeDefIndex: 7
{
    public void |-Action<GetFreeGiftPacket>..ctor() { }
}
"""


@pytest.fixture()
def dump_file(tmp_path: Path) -> Path:
    """迷你 dump.cs 的路径。"""
    path = tmp_path / "dump.cs"
    path.write_text(FAKE_DUMP, encoding="utf-8")
    return path


@pytest.fixture()
def no_questions(tmp_path: Path) -> Path:
    """一个不存在的待确认清单路径（让测试与真实笔记解耦）。"""
    return tmp_path / "no_questions.md"


# ---------------------------------------------------------------------------
# 词干提取：决定了能不能抓到回调
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("class_name", "expected"),
    [
        ("GetFreeGiftPacket", "FreeGift"),
        ("GetFreeGiftPacketRes", "FreeGift"),
        ("BBQFriendsPacket", "BBQFriends"),
        ("GiftInfoClass", "GiftInfo"),
        ("DMReward", "DMReward"),
        # 词干会短到 4 个字符（Root）—— 这种必须原样返回，否则会命中大量无关代码
        ("RootPacket", "RootPacket"),
    ],
)
def test_stem_of(class_name: str, expected: str) -> None:
    assert stem_of(class_name) == expected


def test_trim_line_collapses_whitespace_and_truncates() -> None:
    assert trim_line("\tpublic   void  Foo( ) { }") == "public void Foo( ) { }"
    long_line = "x" * (REF_TEXT_WIDTH + 50)
    trimmed = trim_line(long_line)
    assert len(trimmed) == REF_TEXT_WIDTH
    assert trimmed.endswith("…")


# ---------------------------------------------------------------------------
# 目标解析
# ---------------------------------------------------------------------------
def test_normalize_targets_accepts_message_id_and_case_insensitive_name() -> None:
    targets, warnings = normalize_targets(["37019", "getfreegiftpacket"])
    assert targets == ["GetFreeGiftPacket"]
    assert warnings == []


def test_normalize_targets_warns_on_unknown_message_id() -> None:
    targets, warnings = normalize_targets(["999999"])
    assert targets == []
    assert len(warnings) == 1
    assert "999999" in warnings[0]


def test_normalize_targets_keeps_non_packet_class() -> None:
    targets, _warnings = normalize_targets(["FriendClass"])
    assert targets == ["FriendClass"]


# ---------------------------------------------------------------------------
# 引用分类与过滤：这一步直接决定「输出几十行」还是「输出几千行」
# ---------------------------------------------------------------------------
def test_classify_reference_labels() -> None:
    assert (
        classify_reference(
            "public class GetFreeGiftPacket // TypeDefIndex: 1", "GetFreeGiftPacket"
        )
        == "定义"
    )
    assert (
        classify_reference(
            "    public void Send() { SendPacket<GetFreeGiftPacket>(null); }",
            "GetFreeGiftPacket",
        )
        == "发送"
    )
    assert (
        classify_reference(
            "    public void OnGetFreeGiftRes(GetFreeGiftPacketRes iRes) { }",
            "GetFreeGiftPacketRes",
        )
        == "回调"
    )
    assert (
        classify_reference(
            "public class GiftPackageNetModule // TypeDefIndex: 3", "GetFreeGiftPacket"
        )
        == "网络模块"
    )
    # 方法的参数里出现了本类型 —— 这就是「谁在发这个包」的证据
    assert (
        classify_reference("    public void Repack(GetFreeGiftPacket p) { }", "GetFreeGiftPacket")
        == "方法"
    )


def test_classify_reference_drops_noise() -> None:
    """无关行必须被丢弃，否则输出会膨胀成几千行。"""
    assert (
        classify_reference("    public long giftId { get; set; }", "GetFreeGiftPacket") is None
    )
    assert (
        classify_reference("// Namespace: com.auer.game.protobuf", "GetFreeGiftPacket") is None
    )


# ---------------------------------------------------------------------------
# 单遍扫描
# ---------------------------------------------------------------------------
def test_scan_references_records_owner_and_total(dump_file: Path) -> None:
    refs = scan_references(dump_file, ["GetFreeGiftPacket"])
    kept, total = refs["GetFreeGiftPacket"]

    kinds = {ref.kind for ref in kept}
    # 分类会按「所属类」二次修正：NetModule 里的方法 = 请求入口，UIView 里的 = 界面动作
    assert {"定义", "发送", "网络模块", "UI", "信封槽位"} <= kinds
    assert total == len(kept)  # 迷你文件远未触及上限

    owners = {ref.owner for ref in kept if ref.kind == "网络模块"}
    assert "GiftPackageNetModule" in owners  # 正是它负责发这个包

    ui_refs = [ref for ref in kept if ref.kind == "UI"]
    assert ui_refs[0].owner == "GiftPackage_Home_Remake_UIView"


def test_scan_references_drops_compiler_generated_lines(dump_file: Path) -> None:
    """``|-Action<...>..ctor`` 这类生成物必须丢掉，否则会挤掉真正的证据。"""
    refs = scan_references(dump_file, ["GetFreeGiftPacket"])
    kept, _total = refs["GetFreeGiftPacket"]
    assert all("|-" not in ref.text for ref in kept)


def test_scan_references_respects_cap(dump_file: Path) -> None:
    refs = scan_references(dump_file, ["GetFreeGiftPacket"], per_class_cap=1)
    kept, total = refs["GetFreeGiftPacket"]
    assert len(kept) == 1
    assert total > 1  # 超出上限的部分只计数，信息不丢


def test_scan_references_handles_multiple_targets_in_one_pass(dump_file: Path) -> None:
    refs = scan_references(dump_file, ["GetFreeGiftPacket", "ProbeOnlyPacket"])
    assert refs["ProbeOnlyPacket"][1] >= 1  # 至少找到类定义那一行
    assert set(refs) == {"GetFreeGiftPacket", "ProbeOnlyPacket"}


# ---------------------------------------------------------------------------
# 分类的二次修正 + 排序：决定默认那 6 行到底有没有用
# ---------------------------------------------------------------------------
def test_refine_kind_uses_owner_class() -> None:
    assert refine_kind("方法", "GiftPackageNetModule", "public void X() { }") == "网络模块"
    assert refine_kind("方法", "GiftPackage_Home_Remake_UIView", "public void X() { }") == "UI"
    assert refine_kind("方法", "VirtualPacket", "private IEnumerator Get_X() { }") == "分发器"
    assert refine_kind("方法", "IDMAP0", "public const IDMAP0 X = 1;") == "ID映射"
    assert (
        refine_kind("方法", "RootPacket", "public GetFreeGiftPacket get_GetFreeGiftPacket() { }")
        == "信封槽位"
    )
    # 普通类里的方法保持初判不变
    assert refine_kind("方法", "SomeHelper", "public void X() { }") == "方法"
    # 编译器生成物统一标为「生成代码」（调用方会丢弃）
    assert refine_kind("方法", "Action", "|-Action<Foo>..ctor() { }") == "生成代码"


def test_reference_sort_key_prefers_call_chain_over_envelope_slots() -> None:
    net = Reference(line_no=900, kind="网络模块", text="x")
    slot = Reference(line_no=100, kind="信封槽位", text="x")
    assert reference_sort_key(net) < reference_sort_key(slot)


# ---------------------------------------------------------------------------
# 组装档案卡
# ---------------------------------------------------------------------------
def test_build_entries_reads_fields_from_generated_table(
    dump_file: Path, no_questions: Path
) -> None:
    entries, _warnings = build_entries(
        ["DMReward"], dump_path=dump_file, use_config=False, questions_path=no_questions
    )
    entry = entries[0]
    assert entry.in_table is True
    assert entry.packet_id == 6005  # 与 notes/recon_findings.md 6.1 一致
    assert entry.fields == (("misID", 1), ("mistypeID", 2))


def test_build_entries_extracts_unlisted_class_from_dump(
    dump_file: Path, no_questions: Path
) -> None:
    """表里没收录的类就地抽取 —— 不必为了看一眼字段先改工具再重新生成。"""
    entries, warnings = build_entries(
        ["ProbeOnlyPacket"],
        dump_path=dump_file,
        use_config=False,
        questions_path=no_questions,
    )
    entry = entries[0]
    assert entry.in_table is False
    assert entry.fields == (("probeId", 1), ("probeName", 2))
    assert "现场从 dump.cs" in entry.field_status
    assert any("没有消息号" in warning for warning in warnings)


def test_build_entries_pairs_neighbour_message_id(
    dump_file: Path, no_questions: Path
) -> None:
    entries, _warnings = build_entries(
        ["37019"], dump_path=dump_file, use_config=False, questions_path=no_questions
    )
    entry = entries[0]
    assert entry.class_name == "GetFreeGiftPacket"
    assert entry.pair_id == 37020
    assert entry.pair_name == "GetFreeGiftPacketRes"


def test_build_entries_sorts_references_by_usefulness(
    dump_file: Path, no_questions: Path
) -> None:
    """默认只显示 6 行，所以「谁在发这个包」必须排在信封槽位之前。"""
    entries, _warnings = build_entries(
        ["GetFreeGiftPacket"],
        dump_path=dump_file,
        use_config=False,
        questions_path=no_questions,
    )
    kinds = [ref.kind for ref in entries[0].references]
    assert kinds[0] == "发送"
    assert kinds.index("网络模块") < kinds.index("信封槽位")


def test_build_entries_survives_missing_dump(tmp_path: Path, no_questions: Path) -> None:
    """dump.cs 不在（没放素材）时不该崩，只是引用为空 + 一句告警。"""
    entries, warnings = build_entries(
        ["DMReward"],
        dump_path=tmp_path / "not_here.cs",
        use_config=False,
        questions_path=no_questions,
    )
    assert entries[0].in_table is True
    assert entries[0].references == []
    assert any("找不到 dump.cs" in warning for warning in warnings)


# ---------------------------------------------------------------------------
# 渲染：行数上限是「省 Token」的硬约束，必须被守住
# ---------------------------------------------------------------------------
def _big_entry() -> DossierEntry:
    """构造一个字段很多的档案卡，用来验证截断。"""
    return DossierEntry(
        class_name="BigPacket",
        packet_id=12345,
        fields=tuple((f"field{index}", index) for index in range(1, 30)),
    )


def test_render_respects_max_lines() -> None:
    text = render_entries([_big_entry()], max_lines=20)
    assert len(text.splitlines()) <= 20
    assert "截断" in text


def test_render_always_shows_warnings() -> None:
    """告警排在最前面，所以即使截断也不会被吃掉。"""
    text = render_entries([_big_entry()], warnings=["找不到 dump.cs"], max_lines=10)
    assert "找不到 dump.cs" in text


def test_render_full_mode_has_no_truncation_notice() -> None:
    text = render_entries([_big_entry()], max_lines=20, full=True)
    assert "截断" not in text


# ---------------------------------------------------------------------------
# 待确认清单
# ---------------------------------------------------------------------------
def test_parse_open_questions_reads_table_rows(tmp_path: Path) -> None:
    path = tmp_path / "open_questions.md"
    path.write_text(
        "# 待确认清单\n\n"
        "| ID | 问题 | 答案来源 | 阻塞板块 | 复现命令 | 状态 |\n"
        "|---|---|---|---|---|---|\n"
        "| Q-001 | `GiftInfoClass.buyCount` 语义 | dump.cs | free_gift "
        "| `python tools/proto_dossier.py GiftInfoClass` | 未解 |\n",
        encoding="utf-8",
    )
    questions = parse_open_questions(path)
    assert len(questions) == 1
    assert questions[0].qid == "Q-001"
    assert "GiftInfoClass" in questions[0].text
    assert questions[0].command == "python tools/proto_dossier.py GiftInfoClass"


def test_parse_open_questions_tolerates_missing_file_and_prose(tmp_path: Path) -> None:
    assert parse_open_questions(tmp_path / "missing.md") == []
    prose = tmp_path / "prose.md"
    prose.write_text("# 标题\n\n只有说明文字，没有表格。\n", encoding="utf-8")
    assert parse_open_questions(prose) == []


def test_parse_open_questions_merges_extra_cells_into_command(tmp_path: Path) -> None:
    """单元格里误写了 `|` 时不能让整行解析崩掉（多出的碎片并回复现命令列）。"""
    path = tmp_path / "q.md"
    path.write_text("| Q-009 | 问题 | dump.cs | login | `a | b` | 未解 |\n", encoding="utf-8")
    questions = parse_open_questions(path)
    assert len(questions) == 1
    assert "a" in questions[0].command
    assert "b" in questions[0].command


def test_match_questions_by_class_name_or_message_id() -> None:
    questions = [
        Question(
            qid="Q-002",
            text="GetGiftListPacket.cashType 取值",
            source="dump.cs 枚举",
            blocks="free_gift",
            command="python tools/proto_dossier.py GetGiftListPacket",
        ),
        Question(
            qid="Q-004",
            text="BBQFriendsPacket.groupID 语义",
            source="抓包",
            blocks="bbq_energy",
            command="python tools/proto_dossier.py 15003",
        ),
    ]
    assert [q.qid for q in match_questions(questions, "GetGiftListPacket", 37001)] == ["Q-002"]
    # 类名对不上时，靠消息号也能命中
    assert [q.qid for q in match_questions(questions, "BBQFriendsRes", 15003)] == ["Q-004"]
    assert match_questions(questions, "DMReward", 6005) == []


# ---------------------------------------------------------------------------
# 配置表候选（只看文件名，不读内容）
# ---------------------------------------------------------------------------
def test_config_keywords_puts_stem_first() -> None:
    keywords = config_keywords("GetFreeGiftPacket")
    assert keywords[0] == "FreeGift"
    assert len(keywords) <= 3  # 关键词越多，噪声越大


def test_find_config_candidates_matches_by_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    table_dir = tmp_path / "TextAsset"
    table_dir.mkdir()
    (table_dir / "GiftPackageData").write_text("id,name\n1,a\n", encoding="utf-8")
    (table_dir / "UnrelatedTable").write_text("x\n", encoding="utf-8")
    monkeypatch.setattr("config.TEXT_ASSET_DIR", table_dir)

    found = find_config_candidates("GetGiftListPacket")
    assert [cand.name for cand in found] == ["GiftPackageData"]
    assert found[0].size > 0


def test_find_config_candidates_without_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("config.TEXT_ASSET_DIR", tmp_path / "missing")
    assert find_config_candidates("GetGiftListPacket") == []


# ---------------------------------------------------------------------------
# 命令行入口（端到端）
# ---------------------------------------------------------------------------
def test_main_prints_dossier(
    dump_file: Path, no_questions: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "--dump",
            str(dump_file),
            "--no-config",
            "--questions",
            str(no_questions),
            "GetFreeGiftPacket",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "协议档案卡" in out
    assert "giftId" in out
    assert "GetFreeGiftPacket" in out


def test_main_returns_2_for_unknown_target(
    dump_file: Path, no_questions: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "--dump",
            str(dump_file),
            "--no-config",
            "--questions",
            str(no_questions),
            "999999",
        ]
    )
    assert code == 2
    assert "999999" in capsys.readouterr().out
