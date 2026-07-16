"""Deterministic, local-only chapter recognition and paragraph-aware chunking."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any

REASONS = {
    "BROKEN_TEXT_LAYER",
    "IMAGE_ONLY_PAGE",
    "MANUAL_QUALITY_EXCLUSION",
    "EXTRACTION_ORDER_UNUSABLE",
}


class ChunkingError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Config:
    target_chars: int = 1600
    max_chars: int = 2200
    overlap_chars: int = 160


def _digest(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode()
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _link(path: Path) -> bool:
    junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(junction and junction())


def _safe_basename(value: object) -> str:
    if not isinstance(value, str) or not value or any(x in value for x in ("\x00", "/", "\\", ":")):
        raise ChunkingError("CHUNKING_INPUT_INVALID")
    return value


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
        raise ChunkingError("CHUNKING_INPUT_INVALID")
    return value


def validate_config(target_chars: int, max_chars: int, overlap_chars: int) -> Config:
    if (
        type(target_chars) is not int
        or type(max_chars) is not int
        or type(overlap_chars) is not int
        or target_chars < 200
        or target_chars > 8000
        or max_chars < target_chars
        or max_chars > 10000
        or overlap_chars < 0
        or overlap_chars >= target_chars
    ):
        raise ChunkingError("CHUNKING_CONFIG_INVALID")
    return Config(target_chars, max_chars, overlap_chars)


def discover_chunking_sources(root: Path) -> list[Path]:
    if not root.is_dir() or _link(root):
        raise ChunkingError("CHUNKING_INPUT_INVALID")
    resolved = root.resolve()
    found: list[Path] = []
    for current, dirs, files in os.walk(resolved, followlinks=False):
        dirs[:] = [d for d in dirs if not _link(Path(current) / d)]
        if {"normalized-pages.jsonl", "normalization-report.json"}.issubset(files):
            candidate = Path(current).resolve()
            if not _inside(candidate, resolved):
                raise ChunkingError("CHUNKING_INPUT_INVALID")
            found.append(candidate)
    return sorted(found, key=lambda x: x.as_posix().casefold())


def _load(source: Path) -> tuple[list[dict[str, Any]], dict[str, Any], bytes, bytes]:
    try:
        pages = (source / "normalized-pages.jsonl").read_bytes()
        report_bytes = (source / "normalization-report.json").read_bytes()
        records = [json.loads(line) for line in pages.decode("utf-8").splitlines()]
        report = json.loads(report_bytes)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ChunkingError("CHUNKING_INPUT_INVALID") from exc
    if not records or not isinstance(report, dict) or not all(isinstance(x, dict) for x in records):
        raise ChunkingError("CHUNKING_INPUT_INVALID")
    if [r.get("pdf_page_number") for r in records] != list(range(1, len(records) + 1)):
        raise ChunkingError("CHUNKING_PAGE_SEQUENCE_INVALID")
    if type(report.get("total_pages")) is not int or report["total_pages"] != len(records):
        raise ChunkingError("CHUNKING_PAGE_SEQUENCE_INVALID")
    keys = ("source_relative_path", "source_file", "source_sha256", "normalization_version")
    for key in keys:
        if (
            key not in report
            or any(r.get(key) != records[0].get(key) for r in records)
            or report[key] != records[0].get(key)
        ):
            raise ChunkingError("CHUNKING_SOURCE_IDENTITY_MISMATCH")
    if records[0].get("normalization_version") != "1":
        raise ChunkingError("CHUNKING_UNSUPPORTED_NORMALIZATION_VERSION")
    if records[0].get("extraction_version") != "1":
        raise ChunkingError("CHUNKING_SOURCE_IDENTITY_MISMATCH")
    _safe_basename(records[0]["source_file"])
    _safe_relative(records[0]["source_relative_path"])
    if not isinstance(records[0]["source_sha256"], str) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", records[0]["source_sha256"]
    ):
        raise ChunkingError("CHUNKING_SOURCE_IDENTITY_MISMATCH")
    expected = report.get("output_digests", {}).get("normalized-pages.jsonl")
    if expected is not None and expected != _digest(pages):
        raise ChunkingError("CHUNKING_INPUT_INVALID")
    for record in records:
        text, status = record.get("normalized_text"), record.get("page_status")
        if (
            type(record.get("pdf_page_number")) is not int
            or type(record.get("normalized_char_count")) is not int
            or not isinstance(text, str)
            or "\x00" in text
            or record["normalized_char_count"] != len(text)
            or status not in {"included", "empty", "excluded"}
        ):
            raise ChunkingError("CHUNKING_INPUT_INVALID")
        if status == "included" and not text.strip() or status in {"empty", "excluded"} and text:
            raise ChunkingError("CHUNKING_INPUT_INVALID")
        if status == "excluded" and record.get("exclusion_reason") not in REASONS:
            raise ChunkingError("CHUNKING_INPUT_INVALID")
        if status != "excluded" and record.get("exclusion_reason") is not None:
            raise ChunkingError("CHUNKING_INPUT_INVALID")
    return records, report, pages, report_bytes


def _line_data(text: str) -> list[tuple[int, str]]:
    pos = 0
    output = []
    for line in text.splitlines(keepends=True):
        output.append((pos, line))
        pos += len(line)
    return output


def _toc(lines: list[tuple[int, str]]) -> bool:
    nonempty = [line for _, line in lines if line.strip()]
    dotted = sum(bool(re.search(r"[.．…]{3,}.*\d+\s*$", x)) for x in nonempty)
    suffix = sum(bool(re.search(r"\d+\s*$", x)) for x in nonempty)
    return dotted >= 5 or (len(nonempty) >= 8 and suffix * 100 >= len(nonempty) * 70)


def _heading(line: str, edge: bool, isolated: bool) -> tuple[int, str] | None:
    value = line.strip()
    if (
        not value
        or len(value) > 120
        or re.fullmatch(r"[-—\d /]+", value)
        or re.match(r"^(图|表|figure|table|参考文献)", value, re.I)
    ):
        return None
    patterns = (
        (r"^第[一二三四五六七八九十百千\d]+篇\b", 1, "EXPLICIT_PART"),
        (r"^(?:第[一二三四五六七八九十百千\d]+章|chapter\s+[ivxlcdm\d]+)\b", 1, "EXPLICIT_CHAPTER"),
        (r"^(?:第[一二三四五六七八九十百千\d]+节|section\s+\d+)\b", 2, "EXPLICIT_SECTION"),
        (r"^[一二三四五六七八九十]+、\S+", 2, "NUMBERED_LEVEL_2"),
        (r"^[（(][一二三四五六七八九十]+[）)]\s*\S+", 3, "NUMBERED_LEVEL_3"),
        (r"^\d+(?:\.\d+){1,2}\s*\S+", 4, "NUMBERED_LEVEL_4"),
        (r"^\d+[.、]\s*\S+", 3, "NUMBERED_LEVEL_3"),
    )
    for pattern, level, rule in patterns:
        # Numbered forms are intentionally stricter than explicit chapter markers:
        # ordinary list items often occur near a page edge.
        allowed = (edge or isolated) if rule.startswith("EXPLICIT") else isolated
        if re.match(pattern, value, re.I) and allowed:
            if not re.search(r"[。！？；]$", value):
                return level, rule
    return None


def _transaction(out: Path, files: dict[str, bytes], force: bool) -> None:
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists():
            same = (
                out.is_dir()
                and {x.name for x in out.iterdir()} == set(files)
                and all(
                    (out / n).is_file() and (out / n).read_bytes() == b for n, b in files.items()
                )
            )
            if same:
                return
            if not force:
                raise ChunkingError("CHUNKING_OUTPUT_CONFLICT")
        stage = Path(tempfile.mkdtemp(prefix=".medlearn-stage-", dir=out.parent))
    except ChunkingError:
        raise
    except OSError as exc:
        raise ChunkingError("CHUNKING_OUTPUT_WRITE_FAILED") from exc
    backup = out.parent / (".medlearn-backup-" + stage.name.rsplit("-", 1)[-1])
    committed = False
    try:
        for name, body in files.items():
            with (stage / name).open("wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
        if out.exists():
            os.replace(out, backup)
        try:
            os.replace(stage, out)
            committed = True
        except OSError:
            if backup.exists():
                os.replace(backup, out)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    except OSError as exc:
        if not committed and stage.exists():
            try:
                shutil.rmtree(stage)
            except OSError:
                pass
        raise ChunkingError("CHUNKING_OUTPUT_WRITE_FAILED") from exc


def chunk_source(source: Path, output: Path, config: Config, force: bool = False) -> dict[str, Any]:
    records, norm_report, page_bytes, report_bytes = _load(source)
    identity = records[0]
    root_title = Path(identity["source_file"]).stem
    headings: list[dict[str, Any]] = []
    toc_pages: list[int] = []
    for row in records:
        if row["page_status"] != "included":
            continue
        lines = _line_data(row["normalized_text"])
        if _toc(lines):
            toc_pages.append(row["pdf_page_number"])
            continue
        for index, (start, line) in enumerate(lines):
            edge = index < 8
            isolated = (index == 0 or not lines[index - 1][1].strip()) and (
                index + 1 == len(lines) or not lines[index + 1][1].strip()
            )
            detected = _heading(line, edge, isolated)
            if detected:
                headings.append(
                    {
                        "page": row["pdf_page_number"],
                        "line": index,
                        "start": start,
                        "title": line.strip(),
                        "level": detected[0],
                        "rule": detected[1],
                    }
                )
    kept: list[dict[str, Any]] = []
    duplicate = 0
    for heading in headings:
        if (
            kept
            and heading["title"].casefold() == kept[-1]["title"].casefold()
            and heading["level"] == kept[-1]["level"]
            and heading["page"] - kept[-1]["page"] <= 1
        ):
            duplicate += 1
            continue
        kept.append(heading)
    headings = kept
    sections: list[dict[str, Any]] = []

    def section_id(index: int, title: str, level: int, page: int | None, line: int | None) -> str:
        return (
            "sec_"
            + hashlib.sha256(
                f"{identity['source_sha256']}|1|{index}|{page}|{line}|{_digest(title)}|{level}".encode()
            ).hexdigest()[:24]
        )

    root = {
        "structure_version": "1",
        "normalization_version": "1",
        **{k: identity[k] for k in ("source_file", "source_relative_path", "source_sha256")},
        "section_index": 0,
        "parent_section_id": None,
        "level": 0,
        "title": root_title,
        "title_sha256": _digest(root_title),
        "heading_pdf_page_number": None,
        "heading_line_index": None,
        "detection_rule": "SYNTHETIC_ROOT",
        "start_pdf_page_number": 1,
        "end_pdf_page_number": len(records),
        "included_page_count": sum(r["page_status"] == "included" for r in records),
        "excluded_page_count": sum(r["page_status"] == "excluded" for r in records),
        "empty_page_count": sum(r["page_status"] == "empty" for r in records),
        "warning_codes": [],
    }
    root["section_id"] = section_id(0, root_title, 0, None, None)
    sections.append(root)
    stack = [root]
    jump = False
    for h in headings:
        while stack[-1]["level"] >= h["level"]:
            stack.pop()
        if h["level"] > stack[-1]["level"] + 1:
            jump = True
        parent = stack[-1]
        sec = {
            **root,
            "section_index": len(sections),
            "parent_section_id": parent["section_id"],
            "level": h["level"],
            "title": h["title"],
            "title_sha256": _digest(h["title"]),
            "heading_pdf_page_number": h["page"],
            "heading_line_index": h["line"],
            "detection_rule": h["rule"],
            "start_pdf_page_number": h["page"],
            "end_pdf_page_number": len(records),
            "warning_codes": [],
        }
        sec["section_id"] = section_id(
            sec["section_index"], h["title"], h["level"], h["page"], h["line"]
        )
        sections.append(sec)
        stack.append(sec)
    chunks: list[dict[str, Any]] = []
    current: list[tuple[int, int, str, bool]] = []
    current_section: dict[str, Any] = root
    excluded_gap = False
    heading_starts: dict[tuple[int, int], dict[str, Any]] = {
        (h["page"], h["start"]): next(
            s
            for s in sections[1:]
            if s["heading_pdf_page_number"] == h["page"] and s["heading_line_index"] == h["line"]
        )
        for h in headings
    }

    def flush(reason: str, allow_overlap: bool = False) -> None:
        nonlocal current
        if not current:
            return
        text = "".join(x[2] for x in current)
        segments = []
        cursor = 0
        for page, start, value, is_overlap in current:
            segments.append(
                {
                    "pdf_page_number": page,
                    "page_char_start": start,
                    "page_char_end": start + len(value),
                    "chunk_char_start": cursor,
                    "chunk_char_end": cursor + len(value),
                    "is_overlap": is_overlap,
                }
            )
            cursor += len(value)
        path = []
        node: dict[str, Any] | None = current_section
        while node is not None:
            path.append(node)
            node = next((s for s in sections if s["section_id"] == node["parent_section_id"]), None)
        path.reverse()
        index = len(chunks)
        cid = (
            "chunk_"
            + hashlib.sha256(
                f"{identity['source_sha256']}|1|{config}|{index}|{current_section['section_id']}|{_digest(text)}|{segments}".encode()
            ).hexdigest()[:24]
        )
        chunks.append(
            {
                "chunking_version": "1",
                "structure_version": "1",
                "normalization_version": "1",
                **{
                    k: identity[k] for k in ("source_file", "source_relative_path", "source_sha256")
                },
                "chunk_id": cid,
                "chunk_index": index,
                "section_id": current_section["section_id"],
                "section_path": [s["section_id"] for s in path],
                "section_titles": [s["title"] for s in path],
                "text": text,
                "text_sha256": _digest(text),
                "char_count": len(text),
                "primary_char_count": sum(len(x[2]) for x in current if not x[3]),
                "overlap_char_count": sum(len(x[2]) for x in current if x[3]),
                "start_pdf_page_number": segments[0]["pdf_page_number"],
                "end_pdf_page_number": segments[-1]["pdf_page_number"],
                "source_segments": segments,
                "split_reason": reason,
                "warning_codes": [],
            }
        )
        if allow_overlap and config.overlap_chars:
            tail: list[tuple[int, int, str, bool]] = []
            size = 0
            for piece in reversed(current):
                if piece[3] or size + len(piece[2]) > config.overlap_chars:
                    break
                tail.append((piece[0], piece[1], piece[2], True))
                size += len(piece[2])
            current = list(reversed(tail))
        else:
            current = []

    for row in records:
        page = row["pdf_page_number"]
        if row["page_status"] == "excluded":
            flush("EXCLUDED_PAGE_GAP")
            excluded_gap = True
            continue
        if row["page_status"] != "included":
            continue
        for start, line in _line_data(row["normalized_text"]):
            candidate_section = heading_starts.get((page, start))
            if candidate_section is not None:
                flush("SECTION_END")
                current_section = candidate_section
            value = line
            if current and sum(len(x[2]) for x in current) + len(value) > config.max_chars:
                flush("TARGET_REACHED", allow_overlap=True)
            while len(value) > config.max_chars:
                current.append((page, start, value[: config.max_chars], False))
                flush("HARD_SPLIT")
                start += config.max_chars
                value = value[config.max_chars :]
            current.append((page, start, value, False))
    flush("SOURCE_END")
    warnings = []
    if not headings:
        warnings.append("NO_EXPLICIT_HEADINGS_DETECTED")
    if toc_pages:
        warnings.append("POSSIBLE_TOC_PAGE")
    if duplicate:
        warnings.append("DUPLICATE_HEADING_SUPPRESSED")
    if jump:
        warnings.append("HEADING_LEVEL_JUMP")
    if excluded_gap:
        warnings.append("EXCLUDED_PAGE_GAP_PRESENT")
    section_bytes = b"".join(_json(s) for s in sections)
    chunk_bytes = b"".join(_json(c) for c in chunks)
    counts = [c["char_count"] for c in chunks]
    report = {
        "chunking_version": "1",
        "structure_version": "1",
        "supported_normalization_version": "1",
        **{k: identity[k] for k in ("source_file", "source_relative_path", "source_sha256")},
        "input_normalized_pages_sha256": _digest(page_bytes),
        "input_normalization_report_sha256": _digest(report_bytes),
        "configuration": {
            "target_chars": config.target_chars,
            "max_chars": config.max_chars,
            "overlap_chars": config.overlap_chars,
        },
        "total_pages": len(records),
        "included_pages": root["included_page_count"],
        "empty_pages": root["empty_page_count"],
        "excluded_pages": root["excluded_page_count"],
        "possible_toc_pages": toc_pages,
        "heading_count": len(headings),
        "heading_counts_by_level": {
            str(level): sum(h["level"] == level for h in headings) for level in range(1, 5)
        },
        "heading_counts_by_rule": {
            rule: sum(h["rule"] == rule for h in headings)
            for rule in sorted({h["rule"] for h in headings})
        },
        "duplicate_headings_suppressed": duplicate,
        "section_count": len(sections),
        "chunk_count": len(chunks),
        "total_primary_characters": sum(c["primary_char_count"] for c in chunks),
        "total_overlap_characters": sum(c["overlap_char_count"] for c in chunks),
        "minimum_chunk_characters": min(counts, default=0),
        "maximum_chunk_characters": max(counts, default=0),
        "median_chunk_characters": median(counts) if counts else 0,
        "hard_split_count": sum(c["split_reason"] == "HARD_SPLIT" for c in chunks),
        "small_final_chunk_count": 0,
        "excluded_page_gap_count": sum(r["page_status"] == "excluded" for r in records),
        "pages_represented_in_chunks": sorted(
            {s["pdf_page_number"] for c in chunks for s in c["source_segments"]}
        ),
        "warning_codes": warnings,
        "output_files": ["sections.jsonl", "chunks.jsonl", "chunking-report.json"],
        "output_digests": {
            "sections.jsonl": _digest(section_bytes),
            "chunks.jsonl": _digest(chunk_bytes),
        },
        "chunking_status": "success_with_warnings" if warnings else "success",
    }
    _transaction(
        output,
        {
            "sections.jsonl": section_bytes,
            "chunks.jsonl": chunk_bytes,
            "chunking-report.json": _json(report),
        },
        force,
    )
    return {
        k: report[k]
        for k in (
            "source_relative_path",
            "total_pages",
            "excluded_pages",
            "section_count",
            "chunk_count",
            "total_primary_characters",
            "total_overlap_characters",
            "chunking_status",
            "warning_codes",
        )
    }


def chunk_input(
    input_root: Path, output_root: Path, config: Config, force: bool = False
) -> list[dict[str, Any]]:
    inp = input_root.resolve()
    out = output_root.resolve(strict=False)
    if inp == out or _inside(out, inp) or _inside(inp, out) or _link(out):
        raise ChunkingError("CHUNKING_OUTPUT_PATH_UNSAFE")
    sources = discover_chunking_sources(inp)
    if not sources:
        raise ChunkingError("CHUNKING_INPUT_INVALID")
    results = []
    for source in sources:
        try:
            results.append(chunk_source(source, out / source.relative_to(inp), config, force))
        except ChunkingError as exc:
            results.append(
                {"source_relative_path": source.relative_to(inp).as_posix(), "error_code": exc.code}
            )
    return results
