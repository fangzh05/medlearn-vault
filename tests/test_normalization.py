import json
from pathlib import Path

import pytest

from medlearn_vault.normalization import NormalizationError, normalize_input


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
