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


@dataclass(frozen=True)
class SourceSpan:
    pdf_page_number: int
    page_char_start: int
    page_char_end: int


@dataclass(frozen=True)
class ParagraphBlock:
    span: SourceSpan
    paragraph_index: int
    text: str
    text_sha256: str
    section_id: str
    hard_boundary_group_id: int


@dataclass(frozen=True)
class SplitUnit:
    span: SourceSpan
    text: str
    section_id: str
    hard_boundary_group_id: int
    split_reason: str | None = None


def _paragraph_ranges(text: str) -> list[tuple[int, int]]:
    """Partition a page exactly, retaining blank-line delimiters in the prior block."""
    if not text:
        return []
    ends = [match.end() for match in re.finditer(r"(?:\r?\n[ \t]*){2,}", text)]
    starts = [0, *ends]
    stops = [*ends, len(text)]
    return [(start, stop) for start, stop in zip(starts, stops, strict=True) if stop > start]


def _sentence_ranges(text: str) -> list[tuple[int, int]]:
    closing = "”’\"'）)]】》」』"
    ranges: list[tuple[int, int]] = []
    start = 0
    index = 0
    while index < len(text):
        if text[index] in "。！？.!?":
            end = index + 1
            while end < len(text) and text[end] in closing:
                end += 1
            while end < len(text) and text[end].isspace():
                end += 1
            ranges.append((start, end))
            start = end
            index = end
        else:
            index += 1
    if start < len(text):
        ranges.append((start, len(text)))
    return ranges or [(0, len(text))]


def _split_block(block: ParagraphBlock, max_chars: int) -> list[SplitUnit]:
    if len(block.text) <= max_chars:
        return [SplitUnit(block.span, block.text, block.section_id, block.hard_boundary_group_id)]
    output: list[SplitUnit] = []
    pending_start: int | None = None
    pending_end = 0
    for start, end in _sentence_ranges(block.text):
        if end - start > max_chars:
            if pending_start is not None:
                output.append(_unit(block, pending_start, pending_end, "OVERSIZED_PARAGRAPH"))
                pending_start = None
            cursor = start
            while cursor < end:
                stop = min(cursor + max_chars, end)
                reason = (
                    "HARD_SPLIT"
                    if stop < end or end - cursor > max_chars
                    else "OVERSIZED_PARAGRAPH"
                )
                output.append(_unit(block, cursor, stop, reason))
                cursor = stop
        elif pending_start is None:
            pending_start, pending_end = start, end
        elif end - pending_start <= max_chars:
            pending_end = end
        else:
            output.append(_unit(block, pending_start, pending_end, "OVERSIZED_PARAGRAPH"))
            pending_start, pending_end = start, end
    if pending_start is not None:
        output.append(_unit(block, pending_start, pending_end, "OVERSIZED_PARAGRAPH"))
    return output


def _unit(block: ParagraphBlock, start: int, end: int, reason: str) -> SplitUnit:
    return SplitUnit(
        SourceSpan(
            block.span.pdf_page_number,
            block.span.page_char_start + start,
            block.span.page_char_start + end,
        ),
        block.text[start:end],
        block.section_id,
        block.hard_boundary_group_id,
        reason,
    )


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
        decoded_lines = pages.decode("utf-8").splitlines()
        if not decoded_lines or any(not line.strip() for line in decoded_lines):
            raise ValueError
        records = [json.loads(line) for line in decoded_lines]
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
    report_extraction = report.get("extraction_version", report.get("supported_extraction_version"))
    if report_extraction != "1" or any(
        record.get("extraction_version") != "1" for record in records
    ):
        raise ChunkingError("CHUNKING_SOURCE_IDENTITY_MISMATCH")
    _safe_basename(records[0]["source_file"])
    _safe_relative(records[0]["source_relative_path"])
    if not isinstance(records[0]["source_sha256"], str) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", records[0]["source_sha256"]
    ):
        raise ChunkingError("CHUNKING_SOURCE_IDENTITY_MISMATCH")
    digests = report.get("output_digests", {})
    if not isinstance(digests, dict):
        raise ChunkingError("CHUNKING_INPUT_INVALID")
    expected = digests.get("normalized-pages.jsonl")
    if expected is not None and (
        not isinstance(expected, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", expected)
    ):
        raise ChunkingError("CHUNKING_INPUT_INVALID")
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
        _safe_basename(record.get("source_file"))
        _safe_relative(record.get("source_relative_path"))
    status = report.get("normalization_status")
    if status is not None and status not in {"success", "success_with_warnings", "failed"}:
        raise ChunkingError("CHUNKING_INPUT_INVALID")
    if status == "failed":
        raise ChunkingError("CHUNKING_INPUT_INVALID")
    for key, page_status in (
        ("included_pages", "included"),
        ("empty_pages", "empty"),
        ("excluded_pages", "excluded"),
    ):
        if key in report and (
            type(report[key]) is not int
            or report[key] != sum(r["page_status"] == page_status for r in records)
        ):
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
        (r"^(?:第[一二三四五六七八九十百千\d]+章|chapter\s+[ivxlcdm\d]+)", 1, "EXPLICIT_CHAPTER"),
        (r"^(?:第[一二三四五六七八九十百千\d]+节|section\s+\d+)", 2, "EXPLICIT_SECTION"),
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


def _canonical_heading(value: str) -> str:
    return re.sub(r"[ \t]+", " ", value.strip()).casefold()


def _has_substantive_between(
    first: dict[str, Any], second: dict[str, Any], records: list[dict[str, Any]]
) -> bool:
    fragments: list[str] = []
    for page in range(first["page"], second["page"] + 1):
        text = records[page - 1]["normalized_text"]
        start = first["end"] if page == first["page"] else 0
        end = second["start"] if page == second["page"] else len(text)
        fragments.append(text[start:end])
    between = "".join(fragments)
    for line in between.splitlines():
        value = line.strip()
        if value and not re.fullmatch(r"(?:[-— ]*\d+[-— ]*|第\d+页|\d+\s*/\s*\d+)", value):
            return True
    return False


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


def _validate_output(
    records: list[dict[str, Any]], sections: list[dict[str, Any]], chunks: list[dict[str, Any]]
) -> None:
    try:
        if not sections:
            raise ValueError
        root = sections[0]
        if (
            root["parent_section_id"] is not None
            or root["level"] != 0
            or root["start_pdf_page_number"] != 1
            or root["end_pdf_page_number"] != len(records)
        ):
            raise ValueError
        if [section["section_index"] for section in sections] != list(range(len(sections))):
            raise ValueError
        known: set[str] = set()
        previous_heading: tuple[int, int] = (0, -1)
        for section in sections:
            if section["section_id"] in known or (
                section["parent_section_id"] is not None
                and section["parent_section_id"] not in known
            ):
                raise ValueError
            known.add(section["section_id"])
            if not (
                1
                <= section["start_pdf_page_number"]
                <= section["end_pdf_page_number"]
                <= len(records)
            ):
                raise ValueError
            if section is root:
                continue
            heading_page = section["heading_pdf_page_number"]
            heading_offset = section["heading_char_offset"]
            if not isinstance(heading_offset, int) or not (
                section["start_pdf_page_number"] <= heading_page <= section["end_pdf_page_number"]
                and 0 <= heading_offset < len(records[heading_page - 1]["normalized_text"])
                and (heading_page, heading_offset) >= previous_heading
            ):
                raise ValueError
            parent = next(s for s in sections if s["section_id"] == section["parent_section_id"])
            if not (
                parent["start_pdf_page_number"] <= section["start_pdf_page_number"]
                and section["end_pdf_page_number"] <= parent["end_pdf_page_number"]
            ):
                raise ValueError
            previous_heading = (heading_page, heading_offset)
        if [chunk["chunk_index"] for chunk in chunks] != list(range(len(chunks))):
            raise ValueError
        seen_ids: set[str] = set()
        actual_primary: list[tuple[int, int, int]] = []
        primary_text: dict[int, list[tuple[int, int]]] = {}
        section_map = {section["section_id"]: section for section in sections}
        seen_primary: dict[tuple[str, int], list[tuple[int, int, int]]] = {}

        def deepest_section(page: int, offset: int) -> str:
            active = [root]
            for candidate in sections[1:]:
                position = (candidate["heading_pdf_page_number"], candidate["heading_char_offset"])
                if position > (page, offset):
                    break
                while active[-1]["level"] >= candidate["level"]:
                    active.pop()
                active.append(candidate)
            return str(active[-1]["section_id"])

        for chunk in chunks:
            if chunk["chunk_id"] in seen_ids or chunk["primary_char_count"] <= 0:
                raise ValueError
            seen_ids.add(chunk["chunk_id"])
            chunk_section: dict[str, Any] | None = section_map.get(chunk["section_id"])
            path_ids = chunk["section_path"]
            if (
                chunk_section is None
                or not path_ids
                or path_ids[0] != sections[0]["section_id"]
                or path_ids[-1] != chunk["section_id"]
            ):
                raise ValueError
            for parent_id, child_id in zip(path_ids, path_ids[1:], strict=False):
                if section_map[child_id]["parent_section_id"] != parent_id:
                    raise ValueError
            if chunk["char_count"] != len(chunk["text"]) or chunk["text_sha256"] != _digest(
                chunk["text"]
            ):
                raise ValueError
            if chunk["char_count"] != chunk["primary_char_count"] + chunk["overlap_char_count"]:
                raise ValueError
            group = chunk["hard_boundary_group_id"]
            if not isinstance(group, int):
                raise ValueError
            cursor = 0
            primary_section: str | None = None
            previous_page: int | None = None
            for segment in chunk["source_segments"]:
                page = segment["pdf_page_number"]
                if (
                    not (1 <= page <= len(records))
                    or records[page - 1]["page_status"] == "excluded"
                ):
                    raise ValueError
                start, end = segment["page_char_start"], segment["page_char_end"]
                cstart, cend = segment["chunk_char_start"], segment["chunk_char_end"]
                if (
                    not (0 <= start < end <= len(records[page - 1]["normalized_text"]))
                    or cstart != cursor
                ):
                    raise ValueError
                segment_group = segment["hard_boundary_group_id"]
                if not isinstance(segment_group, int) or segment_group != group:
                    raise ValueError
                if previous_page is not None and any(
                    row["page_status"] == "excluded" for row in records[previous_page:page - 1]
                ):
                    raise ValueError
                if records[page - 1]["normalized_text"][start:end] != chunk["text"][cstart:cend]:
                    raise ValueError
                cursor = cend
                applicable = deepest_section(page, start)
                if deepest_section(page, end - 1) != applicable:
                    raise ValueError
                if not segment["is_overlap"]:
                    if applicable != chunk["section_id"]:
                        raise ValueError
                    primary_section = applicable
                    actual_primary.append((page, start, end))
                    primary_text.setdefault(page, []).append((start, end))
                    seen_primary.setdefault((applicable, group), []).append((page, start, end))
                else:
                    if applicable != chunk["section_id"] or primary_section is not None:
                        raise ValueError
                    if not any(
                        page == old_page and start >= old_start and end <= old_end
                        for old_page, old_start, old_end in seen_primary.get(
                            (applicable, group), []
                        )
                    ):
                        raise ValueError
                previous_page = page
            if cursor != len(chunk["text"]):
                raise ValueError
            pages = [segment["pdf_page_number"] for segment in chunk["source_segments"]]
            if (chunk["start_pdf_page_number"], chunk["end_pdf_page_number"]) != (
                min(pages),
                max(pages),
            ):
                raise ValueError
        expected = [
            (row["pdf_page_number"], 0, len(row["normalized_text"]))
            for row in records
            if row["page_status"] == "included"
        ]
        collapsed: list[tuple[int, int, int]] = []
        for page, spans in primary_text.items():
            ordered = sorted(spans)
            if ordered[0][0] != 0 or any(
                left[1] != right[0] for left, right in zip(ordered, ordered[1:], strict=False)
            ):
                raise ValueError
            collapsed.append((page, 0, ordered[-1][1]))
        if collapsed != expected or actual_primary != sorted(actual_primary):
            raise ValueError
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise ChunkingError("CHUNKING_FAILED") from exc


def _apply_small_final_policy(
    chunks: list[dict[str, Any]],
    records: list[dict[str, Any]],
    identity: dict[str, Any],
    config: Config,
) -> int:
    threshold = max(1, config.target_chars // 3)
    retained = 0
    index = len(chunks) - 1
    while index >= 0:
        chunk = chunks[index]
        is_group_final = (
            index == len(chunks) - 1
            or chunks[index + 1]["section_id"] != chunk["section_id"]
            or chunks[index + 1]["hard_boundary_group_id"] != chunk["hard_boundary_group_id"]
        )
        if is_group_final and chunk["primary_char_count"] < threshold:
            previous = chunks[index - 1] if index > 0 else None
            if (
                previous is not None
                and previous["section_id"] == chunk["section_id"]
                and previous["hard_boundary_group_id"] == chunk["hard_boundary_group_id"]
                and previous["primary_char_count"] + chunk["primary_char_count"] <= config.max_chars
            ):
                primary = [
                    segment
                    for candidate in (previous, chunk)
                    for segment in candidate["source_segments"]
                    if not segment["is_overlap"]
                ]
                text_parts: list[str] = []
                segments: list[dict[str, Any]] = []
                cursor = 0
                for segment in primary:
                    value = records[segment["pdf_page_number"] - 1]["normalized_text"][
                        segment["page_char_start"] : segment["page_char_end"]
                    ]
                    text_parts.append(value)
                    segments.append(
                        {
                            **segment,
                            "chunk_char_start": cursor,
                            "chunk_char_end": cursor + len(value),
                            "is_overlap": False,
                        }
                    )
                    cursor += len(value)
                text = "".join(text_parts)
                previous.update(
                    text=text,
                    text_sha256=_digest(text),
                    char_count=len(text),
                    primary_char_count=len(text),
                    overlap_char_count=0,
                    source_segments=segments,
                    end_pdf_page_number=segments[-1]["pdf_page_number"],
                    split_reason=chunk["split_reason"],
                )
                chunks.pop(index)
            else:
                if "SMALL_FINAL_CHUNK" not in chunk["warning_codes"]:
                    chunk["warning_codes"].append("SMALL_FINAL_CHUNK")
                retained += 1
        index -= 1
    for chunk_index, chunk in enumerate(chunks):
        chunk["chunk_index"] = chunk_index
        chunk["chunk_id"] = (
            "chunk_"
            + hashlib.sha256(
                f"{identity['source_sha256']}|1|{config}|{chunk_index}|{chunk['section_id']}|"
                f"{chunk['text_sha256']}|{chunk['source_segments']}".encode()
            ).hexdigest()[:24]
        )
    return retained


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
                        "end": start + len(line),
                        "title": line.strip(),
                        "canonical_title": _canonical_heading(line),
                        "edge_candidate": edge,
                        "level": detected[0],
                        "rule": detected[1],
                    }
                )
    kept: list[dict[str, Any]] = []
    duplicate = 0
    for heading in headings:
        if (
            kept
            and heading["canonical_title"] == kept[-1]["canonical_title"]
            and heading["level"] == kept[-1]["level"]
            and heading["page"] - kept[-1]["page"] <= 1
            and heading["edge_candidate"]
            and kept[-1]["edge_candidate"]
            and not _has_substantive_between(kept[-1], heading, records)
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
        "heading_char_offset": None,
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
            "heading_char_offset": h["start"],
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
    for index, section in enumerate(sections[1:], 1):
        next_peer = next(
            (other for other in sections[index + 1 :] if other["level"] <= section["level"]),
            None,
        )
        section["end_pdf_page_number"] = (
            next_peer["heading_pdf_page_number"] - 1 if next_peer is not None else len(records)
        )
        section["end_pdf_page_number"] = max(
            section["start_pdf_page_number"], section["end_pdf_page_number"]
        )
        span = records[section["start_pdf_page_number"] - 1 : section["end_pdf_page_number"]]
        section["included_page_count"] = sum(row["page_status"] == "included" for row in span)
        section["excluded_page_count"] = sum(row["page_status"] == "excluded" for row in span)
        section["empty_page_count"] = sum(row["page_status"] == "empty" for row in span)
    chunks: list[dict[str, Any]] = []
    current: list[SplitUnit] = []
    pending_overlap: list[SplitUnit] = []
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
        nonlocal current, pending_overlap
        if not current:
            return
        combined = [*pending_overlap, *current]
        text = "".join(unit.text for unit in combined)
        segments = []
        cursor = 0
        for position, unit in enumerate(combined):
            is_overlap = position < len(pending_overlap)
            segments.append(
                {
                    "pdf_page_number": unit.span.pdf_page_number,
                    "page_char_start": unit.span.page_char_start,
                    "page_char_end": unit.span.page_char_end,
                    "chunk_char_start": cursor,
                    "chunk_char_end": cursor + len(unit.text),
                    "is_overlap": is_overlap,
                    "hard_boundary_group_id": unit.hard_boundary_group_id,
                }
            )
            cursor += len(unit.text)
        path = []
        path_node: dict[str, Any] | None = current_section
        while path_node is not None:
            path.append(path_node)
            parent_candidate: dict[str, Any] | None = next(
                (s for s in sections if s["section_id"] == path_node["parent_section_id"]),
                None,
            )
            path_node = parent_candidate
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
                "primary_char_count": sum(len(unit.text) for unit in current),
                "overlap_char_count": sum(len(unit.text) for unit in pending_overlap),
                "start_pdf_page_number": segments[0]["pdf_page_number"],
                "end_pdf_page_number": segments[-1]["pdf_page_number"],
                "source_segments": segments,
                "split_reason": reason,
                "warning_codes": [],
                "hard_boundary_group_id": current[0].hard_boundary_group_id,
            }
        )
        if allow_overlap and config.overlap_chars:
            tail: list[SplitUnit] = []
            size = 0
            for unit in reversed(current):
                if size + len(unit.text) > config.overlap_chars:
                    break
                tail.append(unit)
                size += len(unit.text)
            pending_overlap = list(reversed(tail))
        else:
            pending_overlap = []
        current = []

    for row in records:
        page = row["pdf_page_number"]
        if row["page_status"] == "excluded":
            flush("EXCLUDED_PAGE_GAP")
            pending_overlap = []
            excluded_gap = True
            continue
        if row["page_status"] != "included":
            continue
        text = row["normalized_text"]
        for paragraph_index, (start, end) in enumerate(_paragraph_ranges(text)):
            heading_offsets = sorted(
                (heading_start, section)
                for (heading_page, heading_start), section in heading_starts.items()
                if heading_page == page and start <= heading_start < end
            )
            boundaries = [start, *(offset for offset, _ in heading_offsets), end]
            for sub_start, sub_end in zip(boundaries, boundaries[1:], strict=False):
                if sub_start >= sub_end:
                    continue
                candidate_section = next(
                    (section for offset, section in heading_offsets if offset == sub_start), None
                )
                if (
                    candidate_section is not None
                    and candidate_section["section_id"] != current_section["section_id"]
                ):
                    flush("SECTION_END")
                    pending_overlap = []
                    current_section = candidate_section
                block = ParagraphBlock(
                    SourceSpan(page, sub_start, sub_end),
                    paragraph_index,
                    text[sub_start:sub_end],
                    _digest(text[sub_start:sub_end]),
                    current_section["section_id"],
                    sum(r["page_status"] == "excluded" for r in records[:page]),
                )
                for unit in _split_block(block, config.max_chars):
                    primary_size = sum(len(item.text) for item in current)
                    projected = primary_size + len(unit.text)
                    if current and (
                        projected > config.max_chars
                        or (primary_size >= config.target_chars and projected > config.target_chars)
                    ):
                        flush("TARGET_REACHED", allow_overlap=True)
                    current.append(unit)
                    if unit.split_reason in {"HARD_SPLIT", "OVERSIZED_PARAGRAPH"}:
                        flush(unit.split_reason, allow_overlap=unit.split_reason != "HARD_SPLIT")
    flush("SOURCE_END")
    small_final_count = _apply_small_final_policy(chunks, records, identity, config)
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
    if any(chunk["split_reason"] == "HARD_SPLIT" for chunk in chunks):
        warnings.append("HARD_SPLIT_REQUIRED")
    if any(chunk["char_count"] < max(1, config.target_chars // 3) for chunk in chunks):
        warnings.append("LOW_TEXT_CHUNKS_PRESENT")
    if small_final_count:
        warnings.append("SMALL_FINAL_CHUNK")
    _validate_output(records, sections, chunks)
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
        "small_final_chunk_count": small_final_count,
        "excluded_page_gap_count": sum(
            row["page_status"] == "excluded"
            and (index == 0 or records[index - 1]["page_status"] != "excluded")
            for index, row in enumerate(records)
        ),
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
