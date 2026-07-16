import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from medlearn_vault.cli import app
from medlearn_vault.source_index import SourceIndexError, build_index, search_index, snippet


def make_source(root: Path, name: str = "book.pdf", text: str = "心力衰竭 Heart Failure") -> Path:
    source = root / name
    source.mkdir(parents=True)
    identity = {
        "source_relative_path": f"subject/{name}",
        "source_file": name,
        "source_sha256": "sha256:" + "a" * 64,
    }
    section = {"section_id": "sec_1", "title": "心脏章节"}
    chunk = {
        **identity,
        "chunking_version": "1",
        "structure_version": "1",
        "normalization_version": "1",
        "chunk_id": f"chunk_{name}",
        "chunk_index": 0,
        "section_id": "sec_1",
        "section_path": ["sec_1"],
        "section_titles": ["心脏章节"],
        "start_pdf_page_number": 1,
        "end_pdf_page_number": 1,
        "text": text,
        "text_sha256": "sha256:" + __import__("hashlib").sha256(text.encode()).hexdigest(),
        "char_count": len(text),
    }
    for filename, rows in (("sections.jsonl", [section]), ("chunks.jsonl", [chunk])):
        (source / filename).write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
        )
    data = (source / "chunks.jsonl").read_bytes()
    (source / "chunking-report.json").write_text(
        json.dumps(
            {
                **identity,
                "chunking_version": "1",
                "structure_version": "1",
                "supported_normalization_version": "1",
                "chunk_count": 1,
                "output_digests": {
                    "chunks.jsonl": "sha256:" + __import__("hashlib").sha256(data).hexdigest()
                },
            }
        ),
        encoding="utf-8",
    )
    return source


def test_index_and_search(tmp_path: Path) -> None:
    make_source(tmp_path / "in")
    result = build_index(tmp_path / "in", tmp_path / "index.sqlite3", "0.21.0")
    assert result["source_count"] == 1 and result["chunk_count"] == 1
    assert search_index(tmp_path / "index.sqlite3", "心力衰竭")["results"][0]["score"] >= 1000
    assert search_index(tmp_path / "index.sqlite3", "heart failure")["result_count"] == 1
    assert build_index(tmp_path / "in", tmp_path / "index.sqlite3", "0.21.0")["idempotent"]


def test_bad_source_does_not_stop_good_source(tmp_path: Path) -> None:
    make_source(tmp_path / "in", "good.pdf")
    bad = make_source(tmp_path / "in", "bad.pdf")
    (bad / "chunks.jsonl").write_text("\n", encoding="utf-8")
    result = build_index(tmp_path / "in", tmp_path / "index.sqlite3", "0.21.0")
    assert result["source_count"] == 1 and result["source_failures"]


def test_cli_validation_is_safe(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app, ["sources", "search", " ", "--index", str(tmp_path / "x.sqlite3")]
    )
    assert (
        result.exit_code == 1
        and "SOURCE_INDEX_INVALID_QUERY" in result.output
        and "Traceback" not in result.output
    )


def test_missing_field_is_sanitized(tmp_path: Path) -> None:
    source = make_source(tmp_path / "in")
    row = json.loads((source / "chunks.jsonl").read_text(encoding="utf-8").splitlines()[0])
    del row["text"]
    (source / "chunks.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(SourceIndexError, match="SOURCE_INDEX_INPUT_INVALID"):
        build_index(tmp_path / "in", tmp_path / "index.sqlite3", "0.21.0")


def test_unsafe_source_file_rejected(tmp_path: Path) -> None:
    source = make_source(tmp_path / "in")
    row = json.loads((source / "chunks.jsonl").read_text(encoding="utf-8").splitlines()[0])
    row["source_file"] = "../book.pdf"
    (source / "chunks.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(SourceIndexError, match="SOURCE_INDEX_INPUT_INVALID"):
        build_index(tmp_path / "in", tmp_path / "index.sqlite3", "0.21.0")


def test_bad_output_digest_and_zero_sources(tmp_path: Path) -> None:
    source = make_source(tmp_path / "in")
    report = json.loads((source / "chunking-report.json").read_text())
    report["output_digests"] = []
    (source / "chunking-report.json").write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(SourceIndexError, match="SOURCE_INDEX_INPUT_INVALID"):
        build_index(tmp_path / "in", tmp_path / "index.sqlite3", "0.21.0")
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(SourceIndexError, match="SOURCE_INDEX_INPUT_INVALID"):
        build_index(empty, tmp_path / "empty.sqlite3", "0.21.0")


def test_duplicate_chunk_id_rejected(tmp_path: Path) -> None:
    source = make_source(tmp_path / "in")
    row = (source / "chunks.jsonl").read_text(encoding="utf-8")
    (source / "chunks.jsonl").write_text(row + row, encoding="utf-8")
    report = json.loads((source / "chunking-report.json").read_text())
    report["chunk_count"] = 2
    (source / "chunking-report.json").write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(SourceIndexError, match="SOURCE_INDEX_INPUT_INVALID"):
        build_index(tmp_path / "in", tmp_path / "index.sqlite3", "0.21.0")


def test_conflict_force_and_missing_search_index(tmp_path: Path) -> None:
    make_source(tmp_path / "in")
    index = tmp_path / "index.sqlite3"
    build_index(tmp_path / "in", index, "0.21.0")
    source = tmp_path / "in" / "book.pdf" / "chunks.jsonl"
    changed = json.loads(source.read_text(encoding="utf-8").splitlines()[0])
    changed["text"] = "New Text"
    changed["char_count"] = len(changed["text"])
    changed["text_sha256"] = (
        "sha256:" + __import__("hashlib").sha256(changed["text"].encode()).hexdigest()
    )
    source.write_text(json.dumps(changed) + "\n", encoding="utf-8")
    report = json.loads((tmp_path / "in" / "book.pdf" / "chunking-report.json").read_text())
    report["output_digests"] = {}
    (tmp_path / "in" / "book.pdf" / "chunking-report.json").write_text(
        json.dumps(report), encoding="utf-8"
    )
    with pytest.raises(SourceIndexError, match="SOURCE_INDEX_OUTPUT_CONFLICT"):
        build_index(tmp_path / "in", index, "0.21.0")
    build_index(tmp_path / "in", index, "0.21.0", force=True)
    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(SourceIndexError, match="SOURCE_INDEX_FAILED"):
        search_index(missing, "心力衰竭")
    assert not missing.exists()


def test_incompatible_schema_and_snippet_fallback(tmp_path: Path) -> None:
    index = tmp_path / "bad.sqlite3"
    import sqlite3

    with sqlite3.connect(index) as db:
        db.execute("create table metadata(key text,value text)")
        db.execute("insert into metadata values ('index_schema_version','9')")
    with pytest.raises(SourceIndexError, match="SOURCE_INDEX_FAILED"):
        search_index(index, "x")
    assert "term here" in snippet("prefix term here and more", "missing term")


def test_multi_term_source_filter_and_tie_order(tmp_path: Path) -> None:
    make_source(tmp_path / "in", "z.pdf", "alpha beta")
    make_source(tmp_path / "in", "a.pdf", "alpha beta")
    index = tmp_path / "index.sqlite3"
    build_index(tmp_path / "in", index, "0.21.0")
    result = search_index(index, "alpha beta", source_filter="subject/")
    assert result["result_count"] == 2
    assert [row["source_relative_path"] for row in result["results"]] == [
        "subject/a.pdf",
        "subject/z.pdf",
    ]
