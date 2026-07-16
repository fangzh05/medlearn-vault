import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from medlearn_vault.cli import app
from medlearn_vault.normalization import NormalizationError, normalize_input, normalize_source


def fixture(root: Path, pages: list[str]) -> Path:
    book = root / "book"
    book.mkdir(parents=True)
    rows = [
        {
            "extraction_version": "1",
            "source_file": "book.pdf",
            "source_relative_path": "book.pdf",
            "source_sha256": "sha256:" + "a" * 64,
            "pdf_page_number": i,
            "text": text,
            "char_count": len(text),
            "text_status": "empty" if not text else "text",
        }
        for i, text in enumerate(pages, 1)
    ]
    (book / "pages.jsonl").write_text(
        "".join(json.dumps(x) + "\n" for x in rows), encoding="utf-8", newline="\n"
    )
    (book / "report.json").write_text(
        json.dumps(
            {
                "total_pages": len(rows),
                "source_file": "book.pdf",
                "source_relative_path": "book.pdf",
                "source_sha256": "sha256:" + "a" * 64,
                "extraction_version": "1",
            }
        ),
        encoding="utf-8",
    )
    return book


def test_normalizes_headers_and_preserves_mapping(tmp_path: Path) -> None:
    inp, out = tmp_path / "in", tmp_path / "out"
    fixture(
        inp,
        [
            "HEADER\nbody one\n1",
            "HEADER\nbody two\n2",
            "HEADER\nbody three\n3",
            "HEADER\nbody four\n4",
            "HEADER\nbody five\n5",
        ],
    )
    results, unknown = normalize_input(inp, out)
    assert not unknown and results[0]["total_pages"] == 5
    rows = [
        json.loads(x)
        for x in (out / "book" / "normalized-pages.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [x["pdf_page_number"] for x in rows] == [1, 2, 3, 4, 5]
    assert all("HEADER" not in x["normalized_text"] for x in rows)


def test_rejects_unsafe_output_without_creating(tmp_path: Path) -> None:
    inp = tmp_path / "in"
    fixture(inp, ["text"])
    unsafe = inp / "out"
    with pytest.raises(NormalizationError, match="NORMALIZATION_OUTPUT_PATH_UNSAFE"):
        normalize_input(inp, unsafe)
    assert not unsafe.exists()


def test_exclusion_reason_and_invalid_page(tmp_path: Path) -> None:
    inp, out = tmp_path / "in", tmp_path / "out"
    fixture(inp, ["a", "b"])
    manifest = tmp_path / "x.json"
    manifest.write_text(
        json.dumps(
            {
                "exclusion_version": "1",
                "sources": [
                    {
                        "source_relative_path": "book.pdf",
                        "excluded_pdf_pages": [2],
                        "reason": "MANUAL_QUALITY_EXCLUSION",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    results, _ = normalize_input(inp, out, manifest)
    assert results[0]["excluded_pages"] == 1
    row = json.loads(
        (out / "book" / "normalized-pages.jsonl").read_text(encoding="utf-8").splitlines()[1]
    )
    assert row["exclusion_reason"] == "MANUAL_QUALITY_EXCLUSION"


def _result(out: Path) -> dict[str, object]:
    return json.loads((out / "book" / "normalization-report.json").read_text(encoding="utf-8"))


def _rows(out: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in (out / "book" / "normalized-pages.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]


def test_clean_source_reaches_success(tmp_path: Path) -> None:
    inp, out = tmp_path / "in", tmp_path / "out"
    fixture(inp, ["A sufficiently long ordinary page " * 4])
    results, unknown = normalize_input(inp, out)
    assert unknown == []
    assert results[0]["normalization_status"] == "success"
    assert _result(out)["warning_codes"] == []


def test_warning_status_reachable_for_low_text_page(tmp_path: Path) -> None:
    inp, out = tmp_path / "in", tmp_path / "out"
    fixture(inp, ["short"])
    normalize_input(inp, out)
    assert _result(out)["normalization_status"] == "success_with_warnings"


@pytest.mark.parametrize("count", [1, 2, 3])
def test_short_pages_never_remove_the_sole_meaningful_line(tmp_path: Path, count: int) -> None:
    inp, out = tmp_path / "in", tmp_path / "out"
    fixture(inp, ["Chapter title\n" + str(i) for i in range(1, count + 1)])
    normalize_input(inp, out)
    assert all("Chapter title" in str(row["normalized_text"]) for row in _rows(out))


def test_five_line_overlap_has_unique_header_footer_counts(tmp_path: Path) -> None:
    inp, out = tmp_path / "in", tmp_path / "out"
    fixture(inp, ["H\na\nb\nc\n1" for _ in range(5)])
    normalize_input(inp, out)
    rows = _rows(out)
    assert all(
        int(r["removed_header_line_count"]) + int(r["removed_footer_line_count"]) <= 2 for r in rows
    )
    assert all("a" in str(r["normalized_text"]) for r in rows)


def test_repeated_header_threshold_requires_five_pages_and_sixty_percent(tmp_path: Path) -> None:
    inp, out = tmp_path / "in", tmp_path / "out"
    fixture(
        inp,
        [f"HEADER\nbody {i} with enough content to be unambiguous\nmore\nend" for i in range(5)],
    )
    normalize_input(inp, out)
    assert all("HEADER" not in str(row["normalized_text"]) for row in _rows(out))


def test_repeated_body_text_is_untouched(tmp_path: Path) -> None:
    inp, out = tmp_path / "in", tmp_path / "out"
    fixture(inp, [f"intro {i}\nbody repeated\nunique {i}\nlast {i}" for i in range(5)])
    normalize_input(inp, out)
    assert all("body repeated" in str(row["normalized_text"]) for row in _rows(out))


def test_whitespace_is_cleaned_after_edge_removal(tmp_path: Path) -> None:
    inp, out = tmp_path / "in", tmp_path / "out"
    fixture(inp, [f"HEADER\n\n\nbody {i}\n\n\n{i}" for i in range(5)])
    normalize_input(inp, out)
    assert all(not str(r["normalized_text"]).startswith("\n") for r in _rows(out))
    assert all("\n\n\n" not in str(r["normalized_text"]) for r in _rows(out))


@pytest.mark.parametrize(
    "reason",
    [
        "BROKEN_TEXT_LAYER",
        "IMAGE_ONLY_PAGE",
        "MANUAL_QUALITY_EXCLUSION",
        "EXTRACTION_ORDER_UNUSABLE",
    ],
)
def test_all_exclusion_reasons(tmp_path: Path, reason: str) -> None:
    inp, out, manifest = tmp_path / "in", tmp_path / "out", tmp_path / "x.json"
    fixture(inp, ["one", "two"])
    manifest.write_text(
        json.dumps(
            {
                "exclusion_version": "1",
                "sources": [
                    {
                        "source_relative_path": "book.pdf",
                        "excluded_pdf_pages": [2],
                        "reason": reason,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    normalize_input(inp, out, manifest)
    assert _rows(out)[1]["exclusion_reason"] == reason


def test_unknown_exclusion_is_batch_only(tmp_path: Path) -> None:
    inp, out, manifest = tmp_path / "in", tmp_path / "out", tmp_path / "x.json"
    fixture(inp, ["text"])
    manifest.write_text(
        json.dumps(
            {
                "exclusion_version": "1",
                "sources": [
                    {
                        "source_relative_path": "missing.pdf",
                        "excluded_pdf_pages": [1],
                        "reason": "IMAGE_ONLY_PAGE",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    results, unknown = normalize_input(inp, out, manifest)
    assert unknown == ["missing.pdf"] and results[0]["warning_codes"] == ["LOW_TEXT_PAGES_PRESENT"]


@pytest.mark.parametrize("source_file", ["/absolute.pdf", "dir/book.pdf", "bad\x00.pdf"])
def test_unsafe_source_file_is_rejected(tmp_path: Path, source_file: str) -> None:
    inp, out = tmp_path / "in", tmp_path / "out"
    book = fixture(inp, ["text"])
    rows = [json.loads(book.joinpath("pages.jsonl").read_text(encoding="utf-8"))]
    rows[0]["source_file"] = source_file
    book.joinpath("pages.jsonl").write_text(json.dumps(rows[0]) + "\n", encoding="utf-8")
    report = json.loads(book.joinpath("report.json").read_text(encoding="utf-8"))
    report["source_file"] = source_file
    book.joinpath("report.json").write_text(json.dumps(report), encoding="utf-8")
    result, _ = normalize_input(inp, out)
    assert result[0]["error_code"] == "NORMALIZATION_INPUT_INVALID"


def test_invalid_page_metadata_is_rejected(tmp_path: Path) -> None:
    inp, out = tmp_path / "in", tmp_path / "out"
    book = fixture(inp, ["text"])
    row = json.loads(book.joinpath("pages.jsonl").read_text(encoding="utf-8"))
    row["char_count"] = True
    book.joinpath("pages.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    result, _ = normalize_input(inp, out)
    assert result[0]["error_code"] == "NORMALIZATION_INPUT_INVALID"


def test_staging_failure_preserves_old_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inp, out = tmp_path / "in", tmp_path / "out"
    book = fixture(inp, ["text"])
    destination = out / "book"
    destination.mkdir(parents=True)
    destination.joinpath("old").write_text("old", encoding="utf-8")
    original_open = Path.open

    def fail_open(self: Path, *args: object, **kwargs: object):
        if ".medlearn-stage-" in str(self):
            raise OSError("injected")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_open)
    with pytest.raises(NormalizationError, match="NORMALIZATION_OUTPUT_WRITE_FAILED"):
        normalize_source(book, destination, {}, force=True)
    assert destination.joinpath("old").read_text(encoding="utf-8") == "old"


def test_replace_failure_restores_old_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inp, out = tmp_path / "in", tmp_path / "out"
    book = fixture(inp, ["text"])
    destination = out / "book"
    destination.mkdir(parents=True)
    destination.joinpath("old").write_text("old", encoding="utf-8")
    original_replace = os.replace
    calls = 0

    def fail_second(src: object, dst: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected")
        original_replace(src, dst)

    monkeypatch.setattr(os, "replace", fail_second)
    with pytest.raises(NormalizationError, match="NORMALIZATION_OUTPUT_WRITE_FAILED"):
        normalize_source(book, destination, {}, force=True)
    assert destination.joinpath("old").read_text(encoding="utf-8") == "old"


def test_idempotency_rejects_stale_output_file(tmp_path: Path) -> None:
    inp, out = tmp_path / "in", tmp_path / "out"
    fixture(inp, ["text"])
    normalize_input(inp, out)
    (out / "book" / "stale").write_text("stale", encoding="utf-8")
    result, _ = normalize_input(inp, out)
    assert result[0]["error_code"] == "NORMALIZATION_OUTPUT_CONFLICT"


def test_cli_unknown_exclusion_is_batch_warning_only(tmp_path: Path) -> None:
    inp, out, manifest = tmp_path / "in", tmp_path / "out", tmp_path / "x.json"
    fixture(inp, ["text"])
    manifest.write_text(
        json.dumps(
            {
                "exclusion_version": "1",
                "sources": [
                    {
                        "source_relative_path": "missing.pdf",
                        "excluded_pdf_pages": [1],
                        "reason": "IMAGE_ONLY_PAGE",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        app,
        [
            "sources",
            "normalize",
            "--input-root",
            str(inp),
            "--output-root",
            str(out),
            "--exclusions",
            str(manifest),
            "--json",
        ],
    )
    payload = json.loads(result.output)
    assert result.exit_code == 0
    assert payload["warning_codes"] == ["UNKNOWN_EXCLUSION_SOURCE"]
    assert payload["unknown_exclusion_source_count"] == 1
    assert "UNKNOWN_EXCLUSION_SOURCE" not in payload["sources"][0]["warning_codes"]


def test_backup_cleanup_failure_is_sanitized_and_preserves_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inp, out = tmp_path / "in", tmp_path / "out"
    book = fixture(inp, ["text"])
    destination = out / "book"
    destination.mkdir(parents=True)
    destination.joinpath("old").write_text("old", encoding="utf-8")
    from medlearn_vault import normalization

    original = normalization._remove_tree
    monkeypatch.setattr(
        normalization, "_remove_tree", lambda _path: (_ for _ in ()).throw(OSError())
    )
    with pytest.raises(NormalizationError, match="NORMALIZATION_OUTPUT_WRITE_FAILED"):
        normalize_source(book, destination, {}, force=True)
    assert any(destination.parent.glob(".medlearn-backup-*"))
    monkeypatch.setattr(normalization, "_remove_tree", original)
