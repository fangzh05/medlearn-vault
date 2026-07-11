"""Stable, content-addressed identifiers."""

import hashlib
import json
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any


def normalize_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).strip().casefold().split())


def _canonical(value: Any) -> Any:
    if isinstance(value, str):
        return normalize_text(value)
    if isinstance(value, Mapping):
        return {str(k): _canonical(v) for k, v in sorted(value.items(), key=lambda p: str(p[0]))}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_canonical(item) for item in value]
    return value


def stable_id(prefix: str, *parts: Any, length: int = 16) -> str:
    payload = json.dumps(_canonical(parts), ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]
    return f"{prefix}_{digest}"


def concept_id(canonical_name: str, concept_type: str) -> str:
    return stable_id("concept", concept_type, canonical_name)


def claim_id(statement: str, concept_ids: Sequence[str]) -> str:
    return stable_id("cl", statement, sorted(concept_ids))


def relation_id(source_concept_id: str, relation_type: str, target_concept_id: str) -> str:
    return stable_id("rel", source_concept_id, relation_type, target_concept_id)


def knowledge_unit_id(unit_type: str, title: str, concept_ids: Sequence[str]) -> str:
    return stable_id("ku", unit_type, title, sorted(concept_ids))
