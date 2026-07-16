import hashlib
import json
from pathlib import Path

import fitz  # type: ignore[import-untyped]
import pytest
from typer.testing import CliRunner

import medlearn_vault.source_pdf as source_pdf
from medlearn_vault.cli import app
from medlearn_vault.source_pdf import PdfExtractionError, extract_input


def make_pdf(path: Path, pages: list[str]) -> None:
    document = fitz.open()
    for text in pages:
        page = document.new_page()
        if text:
            page.insert_text((72, 72), text, fontname="china-s")
    document.save(path)
    document.close()


def test_page_preserving_chinese_output_is_deterministic_and_immutable(tmp_path: Path) -> None:
    raw, output = tmp_path / "raw", tmp_path / "generated"
    raw.mkdir()
    pdf = raw / "内科学" / "教材.PDF"
    pdf.parent.mkdir()
    make_pdf(pdf, ["第一页 中文内容", "", "第三页 内容"])
    before = pdf.read_bytes()
    result = extract_input(raw, output)
    assert len(result) == 1
    folder = output / "内科学" / "教材"
    records = [
        json.loads(line)
        for line in (folder / "pages.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    report = json.loads((folder / "report.json").read_text(encoding="utf-8"))
    assert [record["pdf_page_number"] for record in records] == [1, 2, 3]
    assert records[1]["text"] == "" and records[1]["text_status"] == "empty"
    assert records[0]["source_relative_path"] == "内科学/教材.PDF"
    assert records[0]["source_sha256"] == "sha256:" + hashlib.sha256(before).hexdigest()
    assert report["source_byte_length"] == len(before)
    assert report["total_pages"] == 3 and report["empty_pages"] == 1
    assert (folder / "fulltext.txt").read_text(encoding="utf-8").endswith("\n")
    initial = (folder / "pages.jsonl").read_bytes()
    extract_input(raw, output)
    assert (folder / "pages.jsonl").read_bytes() == initial
    assert pdf.read_bytes() == before


def test_conflict_force_and_directory_partial_failure(tmp_path: Path) -> None:
    raw, output = tmp_path / "raw", tmp_path / "generated"
    raw.mkdir()
    make_pdf(raw / "one.pdf", ["native text"])
    (raw / "bad.PdF").write_bytes(b"not a pdf")
    extract_input(raw / "one.pdf", output)
    target = output / "one" / "fulltext.txt"
    target.write_text("different\n", encoding="utf-8")
    conflict = extract_input(raw / "one.pdf", output)
    assert conflict == [{"source_relative_path": "one.pdf", "error_code": "PDF_OUTPUT_CONFLICT"}]
    extract_input(raw / "one.pdf", output, force=True)
    runner = CliRunner()
    response = runner.invoke(
        app, ["sources", "extract-pdf", "--input", str(raw), "--output-root", str(output), "--json"]
    )
    assert response.exit_code == 1
    payload = json.loads(response.output)
    assert payload["discovered_count"] == 2 and payload["failed_count"] == 1
    assert all("text" not in item for item in payload["files"])


def test_no_pdf_and_unsafe_destination_are_rejected(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    with pytest.raises(PdfExtractionError, match="PDF_NOT_FOUND"):
        extract_input(raw, tmp_path / "generated")
    make_pdf(raw / "book.pdf", ["text"])
    unsafe = raw / "generated"
    with pytest.raises(PdfExtractionError, match="PDF_OUTPUT_PATH_UNSAFE"):
        extract_input(raw, unsafe)
    assert not unsafe.exists()


def test_whitespace_only_page_is_empty(tmp_path: Path) -> None:
    raw, output = tmp_path / "raw", tmp_path / "generated"
    raw.mkdir()
    make_pdf(raw / "book.pdf", ["   \n\t"])
    extract_input(raw, output)
    record = json.loads((output / "book" / "pages.jsonl").read_text(encoding="utf-8"))
    assert record["text"] == "" and record["text_status"] == "empty"


def test_staging_failure_cleans_and_force_preserves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw, output = tmp_path / "raw", tmp_path / "generated"
    raw.mkdir()
    make_pdf(raw / "book.pdf", ["text"])
    calls = 0
    original = source_pdf._atomic_write

    def fail_second(path: Path, content: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PdfExtractionError("PDF_OUTPUT_WRITE_FAILED")
        original(path, content)

    monkeypatch.setattr(source_pdf, "_atomic_write", fail_second)
    result = extract_input(raw, output, force=True)
    assert result == [{"source_relative_path": "book.pdf", "error_code": "PDF_OUTPUT_WRITE_FAILED"}]
    assert not (output / "book").exists()
    assert not list(output.glob(".medlearn-*")) if output.exists() else True

    monkeypatch.setattr(source_pdf, "_atomic_write", original)
    extract_input(raw, output)
    before = {p.name: p.read_bytes() for p in (output / "book").iterdir()}
    (output / "book" / "fulltext.txt").write_text("different\n", encoding="utf-8")
    calls = 0
    monkeypatch.setattr(source_pdf, "_atomic_write", fail_second)
    result = extract_input(raw, output, force=True)
    assert result == [{"source_relative_path": "book.pdf", "error_code": "PDF_OUTPUT_WRITE_FAILED"}]
    assert {p.name: p.read_bytes() for p in (output / "book").iterdir()} == before
    assert not list(output.glob(".medlearn-*"))


def test_commit_failure_restores_previous_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw, output = tmp_path / "raw", tmp_path / "generated"
    raw.mkdir()
    make_pdf(raw / "book.pdf", ["text"])
    extract_input(raw, output)
    before = {p.name: p.read_bytes() for p in (output / "book").iterdir()}
    original_replace = source_pdf.os.replace
    calls = 0

    def fail_commit(source: str | Path, destination: str | Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected replace failure")
        original_replace(source, destination)

    monkeypatch.setattr(source_pdf.os, "replace", fail_commit)
    result = extract_input(raw, output, force=True)
    assert result == [{"source_relative_path": "book.pdf", "error_code": "PDF_OUTPUT_WRITE_FAILED"}]
    assert {p.name: p.read_bytes() for p in (output / "book").iterdir()} == before
    assert not list(output.glob(".medlearn-*"))
