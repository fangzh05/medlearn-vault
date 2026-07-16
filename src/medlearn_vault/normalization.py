"""Deterministic, conservative normalization of extracted PDF page JSONL."""

from __future__ import annotations
import hashlib, json, os, re, shutil, tempfile
from pathlib import Path
from typing import Any


class NormalizationError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _digest(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def _json(v: object) -> bytes:
    return (
        json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def _safe(p: Path) -> Path:
    if "\x00" in str(p):
        raise NormalizationError("NORMALIZATION_OUTPUT_PATH_UNSAFE")
    return p.resolve()


def _norm(s: str) -> str:
    s = s.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    lines = [x.rstrip(" \t") for x in s.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    out = []
    blanks = 0
    for x in lines:
        if not x.strip():
            blanks += 1
        else:
            blanks = 0
        if blanks <= 2:
            out.append(x)
    return "\n".join(out)


def normalize_one(
    src: Path, out: Path, exclusions: dict[str, list[int]], force=False
) -> dict[str, Any]:
    try:
        raw = (src / "pages.jsonl").read_bytes()
        report = json.loads((src / "report.json").read_text(encoding="utf-8"))
        rows = [json.loads(x) for x in raw.decode("utf-8").splitlines() if x.strip()]
    except Exception as e:
        raise NormalizationError("NORMALIZATION_INPUT_INVALID") from e
    if len(rows) != report.get("total_pages") or [r.get("pdf_page_number") for r in rows] != list(
        range(1, len(rows) + 1)
    ):
        raise NormalizationError("NORMALIZATION_PAGE_SEQUENCE_INVALID")
    if any(
        r.get("source_sha256") != report.get("source_sha256")
        or r.get("source_relative_path") != report.get("source_relative_path")
        or r.get("char_count") != len(r.get("text", ""))
        for r in rows
    ):
        raise NormalizationError("NORMALIZATION_SOURCE_IDENTITY_MISMATCH")
    excluded = set(exclusions.get(report["source_relative_path"], []))
    unknown = excluded - set(range(1, len(rows) + 1))
    if unknown:
        raise NormalizationError("NORMALIZATION_INPUT_INVALID")
    outrows = []
    removed = 0
    for r in rows:
        text = "" if r["pdf_page_number"] in excluded else _norm(r.get("text", ""))
        status = (
            "excluded"
            if r["pdf_page_number"] in excluded
            else ("empty" if not text else "included")
        )
        outrows.append(
            {
                "normalization_version": "1",
                "extraction_version": r["extraction_version"],
                "source_file": r["source_file"],
                "source_relative_path": r["source_relative_path"],
                "source_sha256": r["source_sha256"],
                "pdf_page_number": r["pdf_page_number"],
                "raw_text_sha256": _digest(r.get("text", "").encode()),
                "normalized_text": text,
                "normalized_char_count": len(text),
                "page_status": status,
                "exclusion_reason": "BROKEN_TEXT_LAYER" if status == "excluded" else None,
                "removed_header_line_count": 0,
                "removed_footer_line_count": 0,
                "warning_codes": [],
            }
        )
    pages = b"".join(_json(x) for x in outrows)
    rep = {
        "normalization_version": "1",
        "supported_extraction_version": "1",
        "source_file": report["source_file"],
        "source_relative_path": report["source_relative_path"],
        "source_sha256": report["source_sha256"],
        "input_pages_jsonl_sha256": _digest(raw),
        "input_report_json_sha256": _digest((src / "report.json").read_bytes()),
        "total_pages": len(rows),
        "included_pages": sum(x["page_status"] == "included" for x in outrows),
        "empty_pages": sum(x["page_status"] == "empty" for x in outrows),
        "excluded_pages": len(excluded),
        "pages_with_warnings": 0,
        "raw_total_characters": sum(len(r.get("text", "")) for r in rows),
        "normalized_total_characters": sum(x["normalized_char_count"] for x in outrows),
        "removed_header_line_count": 0,
        "removed_footer_line_count": 0,
        "exclusion_reasons": {"BROKEN_TEXT_LAYER": len(excluded)} if excluded else {},
        "warning_codes": ["EXCLUDED_PAGES_PRESENT"] if excluded else [],
        "output_files": ["normalized-pages.jsonl", "normalization-report.json"],
        "output_digests": {"normalized-pages.jsonl": _digest(pages)},
        "normalization_status": "success_with_warnings" if excluded else "success",
    }
    files = {"normalized-pages.jsonl": pages, "normalization-report.json": _json(rep)}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.mkdir(exist_ok=True)
    for n, b in files.items():
        p = out / n
        if p.exists() and p.read_bytes() != b and not force:
            raise NormalizationError("NORMALIZATION_OUTPUT_CONFLICT")
    for n, b in files.items():
        p = out / n
        p.write_bytes(b)
    return {
        "source_relative_path": report["source_relative_path"],
        "total_pages": len(rows),
        "included_pages": rep["included_pages"],
        "excluded_pages": len(excluded),
        "empty_pages": rep["empty_pages"],
        "normalized_total_characters": rep["normalized_total_characters"],
        "normalization_status": rep["normalization_status"],
        "warning_codes": rep["warning_codes"],
    }
