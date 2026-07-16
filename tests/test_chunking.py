import json
import os
from copy import deepcopy
from pathlib import Path

import pytest
from typer.testing import CliRunner

from medlearn_vault.chunking import (
    ChunkingError,
    ParagraphBlock,
    SourceSpan,
    _paragraph_ranges,
    _split_block,
    _transaction,
    _validate_output,
    chunk_input,
    validate_config,
)
from medlearn_vault.cli import app


def source(root: Path, pages: list[tuple[str, str]]) -> Path:
    book = root / "book"
    book.mkdir(parents=True)
    rows = [
        {
            "normalization_version": "1",
            "extraction_version": "1",
            "source_file": "book.pdf",
            "source_relative_path": "subject/book.pdf",
            "source_sha256": "sha256:" + "a" * 64,
            "pdf_page_number": i,
            "normalized_text": text,
            "normalized_char_count": len(text),
            "page_status": status,
            "exclusion_reason": "BROKEN_TEXT_LAYER" if status == "excluded" else None,
        }
        for i, (text, status) in enumerate(pages, 1)
    ]
    body = "".join(
        json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows
    )
    (book / "normalized-pages.jsonl").write_text(body, encoding="utf-8", newline="\n")
    report = {
        "normalization_version": "1",
        "extraction_version": "1",
        "source_file": "book.pdf",
        "source_relative_path": "subject/book.pdf",
        "source_sha256": "sha256:" + "a" * 64,
        "total_pages": len(rows),
        "output_digests": {
            "normalized-pages.jsonl": "sha256:"
            + __import__("hashlib").sha256(body.encode()).hexdigest()
        },
    }
    (book / "normalization-report.json").write_text(json.dumps(report), encoding="utf-8")
    return book


def rows(path: Path, name: str) -> list[dict[str, object]]:
    return [json.loads(x) for x in (path / "book" / name).read_text(encoding="utf-8").splitlines()]


def test_root_and_chinese_heading_are_deterministic(tmp_path: Path) -> None:
    source(tmp_path / "in", [("第一章 总论\n\n甲" * 40, "included")])
    out = tmp_path / "out"
    first = chunk_input(tmp_path / "in", out, validate_config(200, 400, 0))
    second = chunk_input(tmp_path / "in", out, validate_config(200, 400, 0))
    assert first == second and len(rows(out, "sections.jsonl")) == 2


def test_excluded_pages_never_appear_and_make_boundary(tmp_path: Path) -> None:
    source(tmp_path / "in", [("甲" * 250, "included"), ("", "excluded"), ("乙" * 250, "included")])
    out = tmp_path / "out"
    chunk_input(tmp_path / "in", out, validate_config(200, 300, 0))
    chunks = rows(out, "chunks.jsonl")
    assert all({s["pdf_page_number"] for s in c["source_segments"]} <= {1, 3} for c in chunks)
    assert all(not ({1, 3} <= {s["pdf_page_number"] for s in c["source_segments"]}) for c in chunks)


@pytest.mark.parametrize("args", [(199, 2200, 0), (200, 199, 0), (200, 2200, 200)])
def test_config_invalid(args: tuple[int, int, int]) -> None:
    with pytest.raises(ChunkingError, match="CHUNKING_CONFIG_INVALID"):
        validate_config(*args)


def test_toc_is_suppressed_and_cli_hides_text(tmp_path: Path) -> None:
    source(
        tmp_path / "in",
        [
            (
                "第一章 A .... 1\n第二章 B .... 2\n第三章 C .... 3\n"
                "第四章 D .... 4\n第五章 E .... 5",
                "included",
            )
        ],
    )
    out = tmp_path / "out"
    result = CliRunner().invoke(
        app,
        [
            "sources",
            "chunk",
            "--input-root",
            str(tmp_path / "in"),
            "--output-root",
            str(out),
            "--json",
        ],
    )
    assert result.exit_code == 0 and "第一章" not in result.output
    report = json.loads((out / "book" / "chunking-report.json").read_text(encoding="utf-8"))
    assert "POSSIBLE_TOC_PAGE" in report["warning_codes"]


def test_bad_normalized_count_is_blocker(tmp_path: Path) -> None:
    book = source(tmp_path / "in", [("text", "included")])
    data = json.loads((book / "normalized-pages.jsonl").read_text())
    data["normalized_char_count"] = 0
    (book / "normalized-pages.jsonl").write_text(json.dumps(data) + "\n", encoding="utf-8")
    results = chunk_input(tmp_path / "in", tmp_path / "out", validate_config(200, 300, 0))
    assert results[0]["error_code"] == "CHUNKING_INPUT_INVALID"


def test_overlap_is_measured_and_zero_overlap_is_empty(tmp_path: Path) -> None:
    pages = [("\n\n".join("段落甲" * 12 for _ in range(8)), "included")]
    source(tmp_path / "in", pages)
    chunk_input(tmp_path / "in", tmp_path / "overlap", validate_config(200, 300, 80))
    with_overlap = rows(tmp_path / "overlap", "chunks.jsonl")
    assert any(int(chunk["overlap_char_count"]) > 0 for chunk in with_overlap[1:])
    assert all(
        int(chunk["primary_char_count"]) + int(chunk["overlap_char_count"])
        == int(chunk["char_count"])
        for chunk in with_overlap
    )
    chunk_input(tmp_path / "in", tmp_path / "zero", validate_config(200, 300, 0))
    assert all(
        int(chunk["overlap_char_count"]) == 0 for chunk in rows(tmp_path / "zero", "chunks.jsonl")
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("page_status", "bad"),
        ("source_file", "dir/book.pdf"),
        ("source_relative_path", "../book.pdf"),
    ],
)
def test_malformed_metadata_is_blocker(tmp_path: Path, field: str, value: str) -> None:
    book = source(tmp_path / "in", [("text", "included")])
    record = json.loads((book / "normalized-pages.jsonl").read_text())
    record[field] = value
    (book / "normalized-pages.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    results = chunk_input(tmp_path / "in", tmp_path / "out", validate_config(200, 300, 0))
    assert "error_code" in results[0]


def test_transaction_replace_failure_restores_existing_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book = source(tmp_path / "in", [("text " * 100, "included")])
    destination = tmp_path / "out" / "book"
    destination.mkdir(parents=True)
    (destination / "old").write_text("old")
    original = os.replace
    calls = 0

    def fail_second(src: object, dst: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected")
        original(src, dst)

    monkeypatch.setattr(os, "replace", fail_second)
    from medlearn_vault.chunking import chunk_source

    with pytest.raises(ChunkingError, match="CHUNKING_OUTPUT_WRITE_FAILED"):
        chunk_source(book, destination, validate_config(200, 300, 0), force=True)
    assert (destination / "old").read_text() == "old"


def test_target_changes_boundaries_and_no_space_chinese_headings(tmp_path: Path) -> None:
    source(
        tmp_path / "in",
        [("第一章总论\n\n" + "\n\n".join("段落甲" * 20 for _ in range(8)), "included")],
    )
    chunk_input(tmp_path / "in", tmp_path / "small", validate_config(200, 500, 0))
    chunk_input(tmp_path / "in", tmp_path / "large", validate_config(400, 500, 0))
    small = rows(tmp_path / "small", "chunks.jsonl")
    large = rows(tmp_path / "large", "chunks.jsonl")
    assert [chunk["text_sha256"] for chunk in small] != [chunk["text_sha256"] for chunk in large]
    assert len(rows(tmp_path / "small", "sections.jsonl")) == 2


def test_pending_overlap_does_not_emit_at_section_or_gap(tmp_path: Path) -> None:
    source(
        tmp_path / "in",
        [("甲\n" * 160, "included"), ("第一章总论\n乙\n" * 80, "included"), ("", "excluded")],
    )
    chunk_input(tmp_path / "in", tmp_path / "out", validate_config(200, 300, 80))
    chunks = rows(tmp_path / "out", "chunks.jsonl")
    assert all(int(chunk["primary_char_count"]) > 0 for chunk in chunks)
    assert all(
        int(chunk["primary_char_count"]) + int(chunk["overlap_char_count"])
        == int(chunk["char_count"])
        for chunk in chunks
    )


@pytest.mark.parametrize("excluded,expected", [([3], 1), ([3, 4, 5], 1), ([3, 5], 2)])
def test_excluded_gap_runs(tmp_path: Path, excluded: list[int], expected: int) -> None:
    source(
        tmp_path / "in",
        [
            (
                "x" * 250 if page not in excluded else "",
                "excluded" if page in excluded else "included",
            )
            for page in range(1, 7)
        ],
    )
    chunk_input(tmp_path / "in", tmp_path / "out", validate_config(200, 300, 0))
    report = json.loads((tmp_path / "out" / "book" / "chunking-report.json").read_text())
    assert report["excluded_page_gap_count"] == expected


def test_paragraph_ranges_reconstruct_blank_delimiters() -> None:
    text = "first\ninternal\n\nsecond\n\n\nthird"
    ranges = _paragraph_ranges(text)
    assert "".join(text[start:end] for start, end in ranges) == text
    assert text[ranges[0][0] : ranges[0][1]].endswith("\n\n")


@pytest.mark.parametrize(
    "text",
    ["甲。乙！丙？", "First. Second! Third?", '甲。" Next!（尾？）'],
)
def test_sentence_splitting_reconstructs_exact_text(text: str) -> None:
    block = ParagraphBlock(SourceSpan(1, 0, len(text)), 0, text, "sha256:x", "sec", 0)
    units = _split_block(block, 8)
    assert "".join(unit.text for unit in units) == text
    assert all(
        unit.span.page_char_end - unit.span.page_char_start == len(unit.text) for unit in units
    )


def test_indivisible_sentence_uses_hard_split() -> None:
    text = "x" * 25 + "."
    block = ParagraphBlock(SourceSpan(1, 0, len(text)), 0, text, "sha256:x", "sec", 0)
    units = _split_block(block, 10)
    assert "".join(unit.text for unit in units) == text
    assert any(unit.split_reason == "HARD_SPLIT" for unit in units)


def test_small_final_merges_backward_and_gap_prevents_merge(tmp_path: Path) -> None:
    source(tmp_path / "in", [("a" * 180 + "\n\n" + "b" * 30, "included")])
    chunk_input(tmp_path / "in", tmp_path / "merged", validate_config(200, 300, 0))
    assert len(rows(tmp_path / "merged", "chunks.jsonl")) == 1
    source(tmp_path / "gap", [("a" * 220, "included"), ("", "excluded"), ("b" * 30, "included")])
    chunk_input(tmp_path / "gap", tmp_path / "retained", validate_config(200, 300, 0))
    report = json.loads((tmp_path / "retained" / "book" / "chunking-report.json").read_text())
    assert report["small_final_chunk_count"] == 1


def test_duplicate_heading_body_control(tmp_path: Path) -> None:
    source(tmp_path / "plain", [("第一章总论\n\n第一章总论\n\nbody", "included")])
    chunk_input(tmp_path / "plain", tmp_path / "suppressed", validate_config(200, 300, 0))
    assert len(rows(tmp_path / "suppressed", "sections.jsonl")) == 2
    source(tmp_path / "body", [("第一章总论\nbody\n第一章总论\nmore", "included")])
    chunk_input(tmp_path / "body", tmp_path / "retained", validate_config(200, 300, 0))
    assert len(rows(tmp_path / "retained", "sections.jsonl")) == 3


def test_heading_offsets_assign_preheading_body_and_deepest_section(tmp_path: Path) -> None:
    text = "前言内容\n第一章总论\n章节正文\n第一节基础\n小节正文"
    source(tmp_path / "in", [(text, "included")])
    chunk_input(tmp_path / "in", tmp_path / "out", validate_config(200, 300, 0))
    sections = rows(tmp_path / "out", "sections.jsonl")
    chunks = rows(tmp_path / "out", "chunks.jsonl")
    primary = "".join(
        "".join(
            segment_text(chunk, segment)
            for segment in chunk["source_segments"]
            if not segment["is_overlap"]
        )
        for chunk in chunks
    )
    assert primary == text
    assert chunks[0]["section_id"] == sections[0]["section_id"]
    assert any(chunk["section_id"] == sections[1]["section_id"] for chunk in chunks)
    assert any(chunk["section_id"] == sections[2]["section_id"] for chunk in chunks)


def segment_text(chunk: dict[str, object], segment: dict[str, object]) -> str:
    return str(chunk["text"])[int(segment["chunk_char_start"]) : int(segment["chunk_char_end"])]


@pytest.mark.parametrize(
    "text,expected_sections",
    [
        ("前言\n第一章总论\n正文", 2),
        ("第一章总论\n正文", 2),
        ("第一章甲\n正文\n第二章乙\n正文", 3),
        ("第一章甲\n正文\n第一节基础\n正文", 3),
    ],
)
def test_heading_offsets_inside_one_paragraph(
    tmp_path: Path, text: str, expected_sections: int
) -> None:
    source(tmp_path / "in", [(text, "included")])
    chunk_input(tmp_path / "in", tmp_path / "out", validate_config(200, 300, 0))
    assert len(rows(tmp_path / "out", "sections.jsonl")) == expected_sections


def test_small_final_policy_per_hard_boundary_group(tmp_path: Path) -> None:
    source(tmp_path / "same", [("a" * 180 + "\n\n" + "b" * 30, "included")])
    chunk_input(tmp_path / "same", tmp_path / "same-out", validate_config(200, 300, 0))
    assert len(rows(tmp_path / "same-out", "chunks.jsonl")) == 1
    source(tmp_path / "gap", [("a" * 220, "included"), ("", "excluded"), ("b" * 30, "included")])
    chunk_input(tmp_path / "gap", tmp_path / "gap-out", validate_config(200, 300, 0))
    chunks = rows(tmp_path / "gap-out", "chunks.jsonl")
    report = json.loads((tmp_path / "gap-out" / "book" / "chunking-report.json").read_text())
    assert len(chunks) == 2 and report["small_final_chunk_count"] == 1
    assert chunks[0]["hard_boundary_group_id"] != chunks[1]["hard_boundary_group_id"]


def valid_output(
    tmp_path: Path,
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    source(
        tmp_path / "in",
        [("前言\n第一章总论\n" + "\n\n".join("正文" * 30 for _ in range(8)), "included")],
    )
    chunk_input(tmp_path / "in", tmp_path / "out", validate_config(200, 300, 80))
    book = tmp_path / "out" / "book"
    input_path = tmp_path / "in" / "book" / "normalized-pages.jsonl"
    records = [json.loads(line) for line in input_path.read_text(encoding="utf-8").splitlines()]
    return records, rows(book.parent, "sections.jsonl"), rows(book.parent, "chunks.jsonl")


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown-section", "invalid-path", "invalid-parent-chain", "bad-start-page",
        "bad-end-page", "segment-group", "mixed-groups", "future-overlap",
        "other-section-overlap", "root-instead-child",
        "primary-crosses-heading", "heading-outside-range", "child-outside-parent",
    ],
    ids=lambda value: value,
)
def test_validator_rejects_each_structural_corruption(tmp_path: Path, mutation: str) -> None:
    records, sections, chunks = valid_output(tmp_path)
    records, sections, chunks = deepcopy((records, sections, chunks))
    child = sections[1]
    child_chunk = next(chunk for chunk in chunks if chunk["section_id"] == child["section_id"])
    if mutation == "unknown-section":
        child_chunk["section_id"] = "missing"
    elif mutation == "invalid-path":
        child_chunk["section_path"] = [child["section_id"]]
    elif mutation == "invalid-parent-chain":
        child_chunk["section_path"] = [
            sections[0]["section_id"], sections[0]["section_id"], child["section_id"]
        ]
    elif mutation == "bad-start-page":
        child_chunk["start_pdf_page_number"] = 2
    elif mutation == "bad-end-page":
        child_chunk["end_pdf_page_number"] = 2
    elif mutation == "segment-group":
        child_chunk["source_segments"][0]["hard_boundary_group_id"] = 99
    elif mutation == "mixed-groups":
        child_chunk["source_segments"][-1]["hard_boundary_group_id"] = 99
    elif mutation == "future-overlap":
        overlap = next(
            segment
            for chunk in chunks
            for segment in chunk["source_segments"]
            if segment["is_overlap"]
        )
        overlap["is_overlap"] = False
    elif mutation == "other-section-overlap":
        overlap_chunk = next(
            chunk for chunk in chunks if any(s["is_overlap"] for s in chunk["source_segments"])
        )
        overlap_chunk["section_id"] = sections[0]["section_id"]
        overlap_chunk["section_path"] = [sections[0]["section_id"]]
    elif mutation == "root-instead-child":
        child_chunk["section_id"] = sections[0]["section_id"]
        child_chunk["section_path"] = [sections[0]["section_id"]]
    elif mutation == "primary-crosses-heading":
        segment = child_chunk["source_segments"][-1]
        segment["page_char_start"] = 0
    elif mutation == "heading-outside-range":
        child["start_pdf_page_number"] = 2
    else:
        child["end_pdf_page_number"] = 2
    with pytest.raises(ChunkingError, match="CHUNKING_FAILED"):
        _validate_output(records, sections, chunks)


def test_transaction_idempotency_conflict_stale_and_force(tmp_path: Path) -> None:
    out = tmp_path / "out"
    _transaction(out, {"a": b"one"}, False)
    _transaction(out, {"a": b"one"}, False)
    with pytest.raises(ChunkingError, match="CHUNKING_OUTPUT_CONFLICT"):
        _transaction(out, {"a": b"two"}, False)
    (out / "stale").write_bytes(b"stale")
    with pytest.raises(ChunkingError, match="CHUNKING_OUTPUT_CONFLICT"):
        _transaction(out, {"a": b"one"}, False)
    _transaction(out, {"a": b"two"}, True)
    assert {path.name for path in out.iterdir()} == {"a"}


@pytest.mark.parametrize("failure", ["mkdir", "mkdtemp", "write", "fsync"])
def test_transaction_staging_failures_are_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    out = tmp_path / "parent" / "out"
    if failure == "mkdir":
        monkeypatch.setattr(Path, "mkdir", lambda *args, **kwargs: (_ for _ in ()).throw(OSError()))
    elif failure == "mkdtemp":
        import medlearn_vault.chunking as module

        monkeypatch.setattr(
            module.tempfile, "mkdtemp", lambda *args, **kwargs: (_ for _ in ()).throw(OSError())
        )
    elif failure == "write":
        original = Path.open
        monkeypatch.setattr(
            Path,
            "open",
            lambda self, *args, **kwargs: (
                (_ for _ in ()).throw(OSError())
                if ".medlearn-stage-" in str(self)
                else original(self, *args, **kwargs)
            ),
        )
    else:
        monkeypatch.setattr(os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError()))
    with pytest.raises(ChunkingError, match="CHUNKING_OUTPUT_WRITE_FAILED"):
        _transaction(out, {"a": b"one"}, False)
    assert not out.exists()


def test_transaction_restore_failure_preserves_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "old").write_bytes(b"old")
    original = os.replace
    calls = 0

    def fail_commit_and_restore(src: object, dst: object) -> None:
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise OSError("injected")
        original(src, dst)

    monkeypatch.setattr(os, "replace", fail_commit_and_restore)
    with pytest.raises(ChunkingError, match="CHUNKING_OUTPUT_WRITE_FAILED"):
        _transaction(out, {"a": b"new"}, True)
    backups = list(tmp_path.glob(".medlearn-backup-*"))
    assert len(backups) == 1 and (backups[0] / "old").read_bytes() == b"old"
    assert not list(tmp_path.glob(".medlearn-stage-*"))


def test_transaction_backup_cleanup_failure_is_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "old").write_bytes(b"old")
    original = __import__("shutil").rmtree

    def fail_backup(path: object, *args: object, **kwargs: object) -> None:
        if ".medlearn-backup-" in str(path):
            raise OSError("injected")
        original(path, *args, **kwargs)

    import medlearn_vault.chunking as module

    monkeypatch.setattr(module.shutil, "rmtree", fail_backup)
    with pytest.raises(ChunkingError, match="CHUNKING_OUTPUT_WRITE_FAILED"):
        _transaction(out, {"a": b"new"}, True)
    assert (out / "a").read_bytes() == b"new"
    assert len(list(tmp_path.glob(".medlearn-backup-*"))) == 1
