"""Deterministic, local-only SQLite indexing for chunking outputs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, NoReturn


class SourceIndexError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _digest(value: bytes | str) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _invalid() -> NoReturn:
    raise SourceIndexError("SOURCE_INDEX_INPUT_INVALID")


def _safe_relative(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and "\x00" not in value
        and "\\" not in value
        and not value.startswith("/")
        and not re.match(r"^[A-Za-z]:", value)
        and ".." not in value.split("/")
    )


def _safe_basename(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and "\x00" not in value
        and "/" not in value
        and "\\" not in value
        and not re.match(r"^[A-Za-z]:", value)
    )


def _sha(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"sha256:[0-9a-f]{64}", value))


def _real_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        if not lines or any(not line.strip() for line in lines):
            _invalid()
        rows = [json.loads(line) for line in lines]
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise SourceIndexError("SOURCE_INDEX_INPUT_INVALID") from exc
    if not all(isinstance(row, dict) for row in rows):
        _invalid()
    return rows


def _load_source(directory: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    sections = _jsonl(directory / "sections.jsonl")
    chunks_path = directory / "chunks.jsonl"
    chunks = _jsonl(chunks_path)
    try:
        report = json.loads((directory / "chunking-report.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise SourceIndexError("SOURCE_INDEX_INPUT_INVALID") from exc
    if not isinstance(report, dict) or not chunks:
        _invalid()
    first = chunks[0]
    identity = ("source_relative_path", "source_file", "source_sha256")
    if not _safe_relative(first.get("source_relative_path")):
        _invalid()
    if not _safe_basename(first.get("source_file")) or not _sha(first.get("source_sha256")):
        _invalid()
    if any(row.get(key) != first.get(key) for row in chunks for key in identity):
        _invalid()
    if any(report.get(key) != first.get(key) for key in identity):
        _invalid()
    if (report.get("chunking_version"), report.get("structure_version")) != ("1", "1"):
        _invalid()
    if report.get("supported_normalization_version") != "1":
        _invalid()
    if [row.get("chunk_index") for row in chunks] != list(range(len(chunks))):
        _invalid()
    if report.get("chunk_count") != len(chunks):
        _invalid()
    section_ids = {row.get("section_id") for row in sections}
    seen: set[str] = set()
    for chunk in chunks:
        if any(
            chunk.get(key) != "1"
            for key in ("chunking_version", "structure_version", "normalization_version")
        ):
            _invalid()
        text = chunk.get("text")
        if (
            not isinstance(chunk.get("chunk_id"), str)
            or not chunk["chunk_id"]
            or chunk["chunk_id"] in seen
        ):
            _invalid()
        if chunk.get("section_id") not in section_ids or not isinstance(text, str) or not text:
            _invalid()
        path = chunk.get("section_path")
        titles = chunk.get("section_titles")
        if not isinstance(path, list) or not path or not all(isinstance(x, str) for x in path):
            _invalid()
        if not isinstance(titles, list) or not all(isinstance(x, str) for x in titles):
            _invalid()
        if not _sha(chunk.get("source_sha256")) or not _real_int(chunk.get("chunk_index")):
            _invalid()
        if "\x00" in text or chunk.get("char_count") != len(text):
            _invalid()
        if not _real_int(chunk.get("char_count")):
            _invalid()
        if chunk.get("text_sha256") != _digest(text):
            _invalid()
        start, end = chunk.get("start_pdf_page_number"), chunk.get("end_pdf_page_number")
        if not _real_int(start) or not _real_int(end):
            _invalid()
        assert isinstance(start, int) and isinstance(end, int)
        if start <= 0 or end <= 0 or start > end:
            _invalid()
        seen.add(chunk["chunk_id"])
    chunk_bytes = chunks_path.read_bytes()
    outputs = report.get("output_digests", {})
    if "output_digests" in report and not isinstance(outputs, dict):
        _invalid()
    expected = outputs.get("chunks.jsonl") if isinstance(outputs, dict) else None
    if expected is not None and not _sha(expected):
        _invalid()
    if expected is not None and expected != _digest(chunk_bytes):
        _invalid()
    section_seen: set[str] = set()
    for section in sections:
        sid = section.get("section_id")
        if not isinstance(sid, str) or not sid or sid in section_seen:
            _invalid()
        section_seen.add(sid)
        for key in identity:
            if key in section and section[key] != first.get(key):
                _invalid()
    meta = {key: first[key] for key in identity}
    meta.update(
        chunk_count=len(chunks),
        chunks_sha256=_digest(chunk_bytes),
        chunking_report_sha256=_digest((directory / "chunking-report.json").read_bytes()),
    )
    return meta, chunks


def _discover(root: Path) -> list[Path]:
    if not root.is_dir():
        _invalid()
    required = {"sections.jsonl", "chunks.jsonl", "chunking-report.json"}
    found = [Path(path) for path, _, names in os.walk(root) if required <= set(names)]
    return sorted(found, key=lambda path: path.as_posix().casefold())


def _payload(
    sources: list[tuple[dict[str, Any], list[dict[str, Any]]]],
    failures: list[str],
    digest: str,
    idempotent: bool,
) -> dict[str, Any]:
    return {
        "status": "indexed",
        "source_count": len(sources),
        "chunk_count": sum(len(c) for _, c in sources),
        "input_digest": digest,
        "source_failures": failures,
        "idempotent": idempotent,
    }


def build_index(
    input_root: Path, output: Path, package_version: str, force: bool = False
) -> dict[str, Any]:
    root, destination = input_root.resolve(), output.resolve()
    if (
        (output.exists() and output.is_dir())
        or root == destination
        or root in destination.parents
        or destination in root.parents
    ):
        raise SourceIndexError("SOURCE_INDEX_OUTPUT_CONFLICT")
    sources: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    failures: list[str] = []
    for directory in _discover(root):
        try:
            sources.append(_load_source(directory))
        except SourceIndexError:
            failures.append(directory.relative_to(root).as_posix())
    if not sources:
        raise SourceIndexError("SOURCE_INDEX_INPUT_INVALID")
    metadata = [
        item[0] for item in sorted(sources, key=lambda item: item[0]["source_relative_path"])
    ]
    input_digest = _digest(json.dumps(metadata, sort_keys=True, separators=(",", ":")))
    if destination.exists():
        try:
            with sqlite3.connect(destination) as connection:
                old = dict(connection.execute("select key,value from metadata"))
            connection.close()
        except (sqlite3.Error, OSError, ValueError, TypeError):
            old = {}
        if old.get("index_schema_version") == "1" and old.get("input_digest") == input_digest:
            return _payload(sources, failures, input_digest, True)
        if not force:
            raise SourceIndexError("SOURCE_INDEX_OUTPUT_CONFLICT")
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".medlearn-index-", suffix=".sqlite3", dir=output.parent)
        os.close(fd)
        temporary = Path(name)
        try:
            with sqlite3.connect(temporary) as connection:
                connection.executescript(
                    "create table metadata(key text primary key,value text not null);"
                    "create table sources(source_relative_path text primary key,"
                    "source_file text not null,"
                    "source_sha256 text not null,chunks_sha256 text not null,"
                    "chunking_report_sha256 text not null,chunk_count integer not null);"
                    "create table chunks(chunk_id text primary key,"
                    "source_relative_path text not null,source_file text not null,"
                    "source_sha256 text not null,chunk_index integer not null,"
                    "section_id text not null,section_path_json text not null,"
                    "section_titles_json text not null,"
                    "start_pdf_page_number integer not null,end_pdf_page_number integer not null,"
                    "text text not null,text_sha256 text not null,char_count integer not null);"
                    "create index chunks_source on chunks(source_relative_path);"
                    "create index chunks_section on chunks(section_id);"
                    "create index chunks_source_index on chunks(source_relative_path,chunk_index);"
                )
                connection.executemany(
                    "insert into metadata values (?,?)",
                    [
                        ("index_schema_version", "1"),
                        ("package_version", package_version),
                        ("source_count", str(len(sources))),
                        ("chunk_count", str(sum(len(c) for _, c in sources))),
                        ("input_digest", input_digest),
                    ],
                )
                for meta, chunks in sources:
                    connection.execute(
                        "insert into sources values (?,?,?,?,?,?)",
                        (
                            meta["source_relative_path"],
                            meta["source_file"],
                            meta["source_sha256"],
                            meta["chunks_sha256"],
                            meta["chunking_report_sha256"],
                            meta["chunk_count"],
                        ),
                    )
                    connection.executemany(
                        "insert into chunks values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        [
                            (
                                c["chunk_id"],
                                c["source_relative_path"],
                                c["source_file"],
                                c["source_sha256"],
                                c["chunk_index"],
                                c["section_id"],
                                json.dumps(c["section_path"]),
                                json.dumps(c["section_titles"], ensure_ascii=False),
                                c["start_pdf_page_number"],
                                c["end_pdf_page_number"],
                                c["text"],
                                c["text_sha256"],
                                c["char_count"],
                            )
                            for c in chunks
                        ],
                    )
            connection.close()
            with temporary.open("r+b") as handle:
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, output)
        finally:
            if temporary.exists():
                temporary.unlink()
    except OSError as exc:
        raise SourceIndexError("SOURCE_INDEX_OUTPUT_WRITE_FAILED") from exc
    except sqlite3.Error as exc:
        raise SourceIndexError("SOURCE_INDEX_FAILED") from exc
    return _payload(sources, failures, input_digest, False)


def search_index(
    index: Path, query: str, limit: int = 10, source_filter: str | None = None
) -> dict[str, Any]:
    normalized = " ".join(query.split()).casefold()
    if not normalized or not 1 <= limit <= 50:
        raise SourceIndexError("SOURCE_INDEX_INVALID_QUERY")
    if not index.exists() or not index.is_file():
        raise SourceIndexError("SOURCE_INDEX_FAILED")
    try:
        with sqlite3.connect(index) as connection:
            tables = {
                row[0]
                for row in connection.execute("select name from sqlite_master where type='table'")
            }
            if not {"metadata", "sources", "chunks"} <= tables:
                raise SourceIndexError("SOURCE_INDEX_FAILED")
            metadata = dict(connection.execute("select key,value from metadata"))
            if metadata.get("index_schema_version") != "1":
                raise SourceIndexError("SOURCE_INDEX_FAILED")
            cursor = connection.execute("select * from chunks")
            names, rows = [item[0] for item in cursor.description], cursor.fetchall()
    except SourceIndexError:
        raise
    except (sqlite3.Error, json.JSONDecodeError, TypeError, ValueError, OSError) as exc:
        raise SourceIndexError("SOURCE_INDEX_FAILED") from exc
    terms = list(dict.fromkeys(normalized.split()))
    results: list[dict[str, Any]] = []
    for values in rows:
        try:
            row = dict(zip(names, values, strict=True))
            text = row["text"].casefold()
            path = row["source_relative_path"].casefold()
            titles = " ".join(json.loads(row["section_titles_json"])).casefold()
        except (KeyError, json.JSONDecodeError, TypeError, ValueError, AttributeError) as exc:
            raise SourceIndexError("SOURCE_INDEX_FAILED") from exc
        if (source_filter and source_filter.casefold() not in path) or not all(
            term in text or term in titles or term in path for term in terms
        ):
            continue
        row["score"] = (
            min(100, text.count(normalized)) * 1000
            + min(100, titles.count(normalized)) * 200
            + min(100, path.count(normalized)) * 50
            + sum(
                min(100, text.count(term)) * 20 + min(100, titles.count(term)) * 10
                for term in terms
            )
        )
        row["section_path"] = json.loads(row.pop("section_path_json"))
        row["section_titles"] = json.loads(row.pop("section_titles_json"))
        results.append(row)
    results.sort(
        key=lambda row: (
            -row["score"],
            row["source_relative_path"].casefold(),
            row["start_pdf_page_number"],
            row["chunk_index"],
            row["chunk_id"],
        )
    )
    for rank, row in enumerate(results[:limit], 1):
        row["rank"] = rank
    return {
        "query": query,
        "normalized_query": normalized,
        "limit": limit,
        "source_filter": source_filter,
        "result_count": min(len(results), limit),
        "results": results[:limit],
    }


def snippet(text: str, query: str) -> str:
    value = " ".join(text.split())
    lowered = value.casefold()
    normalized = " ".join(query.split()).casefold()
    position = lowered.find(normalized)
    if position < 0:
        positions = [lowered.find(term) for term in normalized.split()]
        positions = [item for item in positions if item >= 0]
        position = min(positions) if positions else 0
    start, end = max(0, position - 100), min(len(value), position + 140)
    return ("…" if start else "") + value[start:end] + ("…" if end < len(value) else "")
