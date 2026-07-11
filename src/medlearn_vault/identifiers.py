"""Opaque identities and normalized content fingerprints."""

import hashlib
import json
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import uuid4


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


def new_opaque_id(prefix: str) -> str:
    """Mint a permanent identity that does not encode mutable content."""
    if not prefix.isalpha() or not prefix.islower():
        raise ValueError("ID prefix must contain lowercase ASCII letters only")
    return f"{prefix}_{uuid4().hex}"


def concept_id() -> str:
    return new_opaque_id("concept")


def source_id() -> str:
    return new_opaque_id("source")


def lens_id() -> str:
    return new_opaque_id("lens")


def concept_fingerprint(concept_type: str, canonical_name: str, aliases: Sequence[str] = ()) -> str:
    """Create a mutable matching fingerprint; never use this as the concept ID."""
    return stable_id(
        "cfp", concept_type, canonical_name, sorted({normalize_text(a) for a in aliases})
    )


def claim_id() -> str:
    return new_opaque_id("claim")


def claim_fingerprint(statement: str, concept_ids: Sequence[str]) -> str:
    return stable_id("clfp", statement, sorted(set(concept_ids)))


def relation_id() -> str:
    return new_opaque_id("relation")


def relation_fingerprint(source_concept_id: str, relation_type: str, target_concept_id: str) -> str:
    return stable_id("relfp", source_concept_id, relation_type, target_concept_id)


def knowledge_unit_id() -> str:
    return new_opaque_id("unit")


def knowledge_unit_fingerprint(unit_type: str, title: str, concept_ids: Sequence[str]) -> str:
    return stable_id("kufp", unit_type, title, sorted(set(concept_ids)))
