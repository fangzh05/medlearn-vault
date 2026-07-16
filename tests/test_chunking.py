import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from medlearn_vault.chunking import ChunkingError, chunk_input, validate_config
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
    pages = [("段落甲\n" * 120 + "段落乙\n" * 120, "included")]
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
