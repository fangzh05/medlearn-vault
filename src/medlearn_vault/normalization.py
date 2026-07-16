"""Deterministic local normalization for extraction-contract page JSONL."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REASONS = {
    "BROKEN_TEXT_LAYER",
    "IMAGE_ONLY_PAGE",
    "MANUAL_QUALITY_EXCLUSION",
    "EXTRACTION_ORDER_UNUSABLE",
}


class NormalizationError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Exclusion:
    pages: frozenset[int]
    reason: str


def _digest(v: bytes) -> str:
    return "sha256:" + hashlib.sha256(v).hexdigest()


def _json(v: object) -> bytes:
    return (
        json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _link(path: Path) -> bool:
    check = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(check and check())


def _canonical(line: str) -> str:
    return re.sub(r"[ \t]+", " ", line.strip()).casefold()


def _safe_relative(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or "\\" in value
        or value.startswith("/")
        or re.match(r"^[A-Za-z]:", value)
        or ".." in value.split("/")
    ):
        raise NormalizationError("NORMALIZATION_INPUT_INVALID")
    return value


def _clean(text: str) -> str:
    lines = [
        x.rstrip(" \t")
        for x in text.lstrip("\ufeff")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\x00", "")
        .split("\n")
    ]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    out = []
    blanks = 0
    for line in lines:
        blanks = blanks + 1 if not line.strip() else 0
        if blanks <= 2:
            out.append(line)
    return "\n".join(out)


def load_exclusion_manifest(path: Path | None) -> dict[str, Exclusion]:
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NormalizationError("NORMALIZATION_INPUT_INVALID") from exc
    if (
        not isinstance(data, dict)
        or data.get("exclusion_version") != "1"
        or not isinstance(data.get("sources"), list)
    ):
        raise NormalizationError("NORMALIZATION_INPUT_INVALID")
    result = {}
    for item in data["sources"]:
        if not isinstance(item, dict):
            raise NormalizationError("NORMALIZATION_INPUT_INVALID")
        source = _safe_relative(item.get("source_relative_path"))
        pages = item.get("excluded_pdf_pages")
        reason = item.get("reason")
        if (
            source in result
            or not isinstance(pages, list)
            or not pages
            or reason not in REASONS
            or any(type(x) is not int or x < 1 for x in pages)
            or len(set(pages)) != len(pages)
        ):
            raise NormalizationError("NORMALIZATION_INPUT_INVALID")
        result[source] = Exclusion(frozenset(pages), reason)
    return result


def discover_normalization_sources(root: Path) -> list[Path]:
    if not root.is_dir():
        raise NormalizationError("NORMALIZATION_INPUT_INVALID")
    root = root.resolve()
    found = []
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if not _link(Path(current) / d)]
        if "pages.jsonl" in files and "report.json" in files:
            found.append(Path(current))
    return sorted(found, key=lambda p: p.as_posix().casefold())


def _load(src: Path) -> tuple[list[dict[str, Any]], dict[str, Any], bytes, bytes]:
    try:
        raw = (src / "pages.jsonl").read_bytes()
        repb = (src / "report.json").read_bytes()
        lines = raw.decode("utf-8").splitlines()
        report = json.loads(repb)
        if not lines or any(not x.strip() for x in lines):
            raise ValueError
        rows = [json.loads(x) for x in lines]
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise NormalizationError("NORMALIZATION_INPUT_INVALID") from exc
    if not all(isinstance(x, dict) for x in rows) or not isinstance(report, dict):
        raise NormalizationError("NORMALIZATION_INPUT_INVALID")
    if [r.get("pdf_page_number") for r in rows] != list(range(1, len(rows) + 1)) or report.get(
        "total_pages"
    ) != len(rows):
        raise NormalizationError("NORMALIZATION_PAGE_SEQUENCE_INVALID")
    keys = ("source_relative_path", "source_sha256", "source_file", "extraction_version")
    for key in keys:
        if any(r.get(key) != rows[0].get(key) for r in rows) or report.get(key) != rows[0].get(key):
            raise NormalizationError("NORMALIZATION_SOURCE_IDENTITY_MISMATCH")
    if rows[0].get("extraction_version") != "1":
        raise NormalizationError("NORMALIZATION_UNSUPPORTED_EXTRACTION_VERSION")
    _safe_relative(rows[0]["source_relative_path"])
    if not isinstance(rows[0]["source_sha256"], str) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", rows[0]["source_sha256"]
    ):
        raise NormalizationError("NORMALIZATION_SOURCE_IDENTITY_MISMATCH")
    for r in rows:
        text = r.get("text")
        status = r.get("text_status")
        if (
            not isinstance(text, str)
            or "\x00" in text
            or r.get("char_count") != len(text)
            or status not in {"text", "empty"}
            or (status == "empty" and text)
            or (status == "text" and not text.strip())
        ):
            raise NormalizationError("NORMALIZATION_INPUT_INVALID")
    return rows, report, raw, repb


def _edge_number(line: str) -> bool:
    return bool(re.fullmatch(r"(?:[-— ]*\d+[-— ]*|第\d+页|\d+\s*/\s*\d+)", line.strip()))


def normalize_source(
    src: Path, out: Path, exclusions: dict[str, Exclusion], force: bool = False
) -> dict[str, Any]:
    rows, report, raw, repb = _load(src)
    identity = report["source_relative_path"]
    ex = exclusions.get(identity, Exclusion(frozenset(), ""))
    if ex.pages - set(range(1, len(rows) + 1)):
        raise NormalizationError("NORMALIZATION_INPUT_INVALID")
    included = [r for r in rows if r["pdf_page_number"] not in ex.pages and r["text"].strip()]
    zones: dict[str, dict[str, int]] = {"top": {}, "bottom": {}}
    if len(included) >= 5:
        for r in included:
            ls = [x for x in _clean(r["text"]).split("\n") if x.strip()]
            for zone, part in (("top", ls[:3]), ("bottom", ls[-3:])):
                for line in set(_canonical(x) for x in part if 2 <= len(_canonical(x)) <= 160):
                    zones[zone][line] = zones[zone].get(line, 0) + 1
    threshold = max(5, (len(included) * 3 + 4) // 5)
    repeated = {z: {x for x, n in d.items() if n >= threshold} for z, d in zones.items()}
    output = []
    rh = rf = 0
    for r in rows:
        rawtext = r["text"]
        warns = []
        head = foot = 0
        if r["pdf_page_number"] in ex.pages:
            text = ""
            status = "excluded"
            reason = ex.reason
        else:
            lines = _clean(rawtext).split("\n") if _clean(rawtext) else []
            non = [i for i, x in enumerate(lines) if x.strip()]
            remove = set()
            for zone, idxs in (("top", non[:3]), ("bottom", non[-3:])):
                for i in idxs:
                    if len(non) > 1 and (
                        _canonical(lines[i]) in repeated[zone] or _edge_number(lines[i])
                    ):
                        remove.add(i)
                        head += zone == "top"
                        foot += zone == "bottom"
            text = "\n".join(x for i, x in enumerate(lines) if i not in remove)
            status = "empty" if not text.strip() else "included"
            reason = None
        if "\ufffd" in text:
            warns.append("REPLACEMENT_CHARACTERS_REMAIN")
        if status == "included" and len(re.sub(r"\s+", "", text)) < 80:
            warns.append("LOW_TEXT_PAGE")
        if head:
            warns.append("HEADER_REMOVED")
        if foot:
            warns.append("FOOTER_REMOVED")
        rh += head
        rf += foot
        output.append(
            {
                "normalization_version": "1",
                "extraction_version": r["extraction_version"],
                "source_file": r["source_file"],
                "source_relative_path": identity,
                "source_sha256": r["source_sha256"],
                "pdf_page_number": r["pdf_page_number"],
                "raw_text_sha256": _digest(rawtext.encode()),
                "normalized_text": text,
                "normalized_char_count": len(text),
                "page_status": status,
                "exclusion_reason": reason,
                "removed_header_line_count": head,
                "removed_footer_line_count": foot,
                "warning_codes": warns,
            }
        )
    pages = b"".join(_json(x) for x in output)
    warnings = []
    for code, cond in (
        ("EXCLUDED_PAGES_PRESENT", bool(ex.pages)),
        ("EMPTY_PAGES_PRESENT", any(x["page_status"] == "empty" for x in output)),
        ("LOW_TEXT_PAGES_PRESENT", any("LOW_TEXT_PAGE" in x["warning_codes"] for x in output)),
        ("HEADER_FOOTER_LINES_REMOVED", bool(rh + rf)),
        (
            "REPLACEMENT_CHARACTERS_REMAIN",
            any("REPLACEMENT_CHARACTERS_REMAIN" in x["warning_codes"] for x in output),
        ),
        ("NO_REPEATED_HEADER_FOOTER_DETECTED", not (rh + rf)),
    ):
        if cond:
            warnings.append(code)
    rep = {
        "normalization_version": "1",
        "supported_extraction_version": "1",
        "source_file": report["source_file"],
        "source_relative_path": identity,
        "source_sha256": report["source_sha256"],
        "input_pages_jsonl_sha256": _digest(raw),
        "input_report_json_sha256": _digest(repb),
        "total_pages": len(output),
        "included_pages": sum(x["page_status"] == "included" for x in output),
        "empty_pages": sum(x["page_status"] == "empty" for x in output),
        "excluded_pages": len(ex.pages),
        "pages_with_warnings": sum(bool(x["warning_codes"]) for x in output),
        "raw_total_characters": sum(len(x["text"]) for x in rows),
        "normalized_total_characters": sum(x["normalized_char_count"] for x in output),
        "removed_header_line_count": rh,
        "removed_footer_line_count": rf,
        "replacement_characters_before": sum(x["text"].count("\ufffd") for x in rows),
        "replacement_characters_after": sum(x["normalized_text"].count("\ufffd") for x in output),
        "exclusion_reasons": {ex.reason: len(ex.pages)} if ex.pages else {},
        "warning_codes": warnings,
        "output_files": ["normalized-pages.jsonl", "normalization-report.json"],
        "output_digests": {"normalized-pages.jsonl": _digest(pages)},
        "normalization_status": "success_with_warnings" if warnings else "success",
    }
    files = {"normalized-pages.jsonl": pages, "normalization-report.json": _json(rep)}
    parent = out.parent
    parent.mkdir(parents=True, exist_ok=True)
    same = out.exists() and all(
        (out / n).is_file() and (out / n).read_bytes() == b for n, b in files.items()
    )
    if same:
        return {
            k: rep[k]
            for k in (
                "source_relative_path",
                "total_pages",
                "included_pages",
                "excluded_pages",
                "empty_pages",
                "normalized_total_characters",
                "normalization_status",
                "warning_codes",
            )
        }
    if out.exists() and not force:
        raise NormalizationError("NORMALIZATION_OUTPUT_CONFLICT")
    stage = Path(tempfile.mkdtemp(prefix=".medlearn-stage-", dir=parent))
    backup = parent / (".medlearn-backup-" + stage.name.rsplit("-", 1)[-1])
    try:
        for n, b in files.items():
            with (stage / n).open("wb") as f:
                f.write(b)
                f.flush()
                os.fsync(f.fileno())
        if out.exists():
            os.replace(out, backup)
        try:
            os.replace(stage, out)
        except OSError:
            if backup.exists():
                os.replace(backup, out)
            raise
        shutil.rmtree(backup, ignore_errors=True)
    except OSError as exc:
        shutil.rmtree(stage, ignore_errors=True)
        shutil.rmtree(backup, ignore_errors=True)
        raise NormalizationError("NORMALIZATION_OUTPUT_WRITE_FAILED") from exc
    return {
        k: rep[k]
        for k in (
            "source_relative_path",
            "total_pages",
            "included_pages",
            "excluded_pages",
            "empty_pages",
            "normalized_total_characters",
            "normalization_status",
            "warning_codes",
        )
    }


def normalize_input(
    input_root: Path, output_root: Path, exclusions: Path | None = None, force: bool = False
) -> tuple[list[dict[str, Any]], list[str]]:
    inp = input_root.resolve()
    out = output_root.resolve(strict=False)
    if inp == out or _inside(out, inp) or _inside(inp, out):
        raise NormalizationError("NORMALIZATION_OUTPUT_PATH_UNSAFE")
    manifest = load_exclusion_manifest(exclusions)
    sources = discover_normalization_sources(inp)
    if not sources:
        raise NormalizationError("NORMALIZATION_INPUT_INVALID")
    results = []
    for src in sources:
        try:
            results.append(normalize_source(src, out / src.relative_to(inp), manifest, force))
        except NormalizationError as exc:
            results.append(
                {"source_relative_path": src.relative_to(inp).as_posix(), "error_code": exc.code}
            )
    unknown = sorted(set(manifest) - {x.get("source_relative_path") for x in results})
    return results, unknown
