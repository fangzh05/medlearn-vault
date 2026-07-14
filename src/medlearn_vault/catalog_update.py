"""Deterministic, repository-reviewed bootstrap proposals for sources and concepts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import Field, model_validator

from medlearn_vault.bundle import ContractBundle
from medlearn_vault.capture import (
    CaptureProposal,
    LearningChatSourceCandidate,
    NewConceptCandidate,
    contract_bundle_digest,
)
from medlearn_vault.domain.base import DomainModel
from medlearn_vault.domain.concepts import ConceptEntity
from medlearn_vault.domain.ids import ConceptId
from medlearn_vault.identifiers import normalize_text


def _bytes(value: Any) -> bytes:
    def json_value(item: Any) -> Any:
        if isinstance(item, DomainModel):
            return json_value(item.model_dump(mode="json"))
        if isinstance(item, dict):
            return {key: json_value(value) for key, value in item.items()}
        if isinstance(item, (list, tuple)):
            return [json_value(value) for value in item]
        return item

    return json.dumps(
        json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_bytes(value)).hexdigest()


def _byte_digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def bundle_path_identity(path: Path) -> str:
    value = path.as_posix()
    normalized = PurePosixPath(value)
    if path.is_absolute() or normalized.is_absolute() or ":" in value or ".." in normalized.parts:
        raise ValueError("INVALID_CATALOG_BUNDLE_PATH")
    return normalized.as_posix()


def _id(prefix: str, *parts: Any) -> str:
    return f"{prefix}_{hashlib.sha256(_bytes(parts)).hexdigest()[:32]}"


class CatalogConceptPromotion(DomainModel):
    candidate_id: str = Field(pattern=r"^candidate_concept_[a-f0-9]{32}$")
    concept: ConceptEntity


class IncompleteConceptMetadata(DomainModel):
    resolution_id: str
    surface_text: str
    evidence_message_ids: tuple[str, ...]
    required_fields: tuple[Literal["canonical_name", "concept_type", "scope_note"], ...]


class CatalogUpdateProposal(DomainModel):
    catalog_update_version: Literal["0.1.0"] = "0.1.0"
    catalog_update_id: str = Field(pattern=r"^catalog_update_[a-f0-9]{32}$")
    capture_proposal_id: str = Field(pattern=r"^proposal_[a-f0-9]{32}$")
    capture_proposal_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    capture_proposal_object_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    base_bundle_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    target_bundle_path: str = Field(min_length=1)
    source_candidate: LearningChatSourceCandidate | None = None
    concept_promotions: tuple[CatalogConceptPromotion, ...] = ()
    incomplete_concept_metadata: tuple[IncompleteConceptMetadata, ...] = ()
    status: Literal["ready_for_manual_merge", "blocked"]
    next_action: str

    @model_validator(mode="after")
    def validate_identity_and_manual_boundary(self) -> CatalogUpdateProposal:
        if bundle_path_identity(Path(self.target_bundle_path)) != self.target_bundle_path:
            raise ValueError("invalid catalog target bundle path")
        if self.catalog_update_id != _id(
            "catalog_update",
            self.catalog_update_version,
            self.capture_proposal_id,
            self.capture_proposal_digest,
            self.capture_proposal_object_digest,
            self.base_bundle_digest,
            self.target_bundle_path,
            self.source_candidate,
            self.concept_promotions,
            self.incomplete_concept_metadata,
        ):
            raise ValueError("catalog_update_id does not match proposal contents")
        if self.status == "blocked" and not self.incomplete_concept_metadata:
            raise ValueError("blocked catalog updates require incomplete metadata")
        if self.status == "ready_for_manual_merge" and self.incomplete_concept_metadata:
            raise ValueError("incomplete metadata must block a catalog update")
        return self


def persistent_concept_from_candidate(candidate: NewConceptCandidate) -> ConceptEntity:
    """Create a suggested persistent entity; callers must still review and merge it."""
    concept_id: ConceptId = _id(
        "concept",
        candidate.canonical_name,
        candidate.preferred_english,
        candidate.concept_type,
        candidate.scope_note,
        candidate.aliases,
    )
    return ConceptEntity(
        concept_id=concept_id,
        canonical_name=candidate.canonical_name,
        preferred_english=candidate.preferred_english,
        concept_type=candidate.concept_type,
        scope_note=candidate.scope_note,
        aliases=candidate.aliases,
    )


def build_catalog_update_proposal(
    capture_proposal: CaptureProposal,
    *,
    capture_proposal_object_digest: str,
    target_bundle_path: str,
) -> CatalogUpdateProposal:
    """Produce review-only repository patch contents; it performs no persistence."""
    incomplete = tuple(
        IncompleteConceptMetadata(
            resolution_id=item.resolution_id,
            surface_text=item.surface_text,
            evidence_message_ids=item.evidence_message_ids,
            required_fields=("canonical_name", "concept_type", "scope_note"),
        )
        for item in capture_proposal.concept_resolutions
        if item.status == "review_required" and not item.candidate_concept_ids
    )
    promotions = tuple(
        CatalogConceptPromotion(
            candidate_id=item.candidate_id,
            concept=persistent_concept_from_candidate(item),
        )
        for item in capture_proposal.new_concept_candidates
    )
    status: Literal["ready_for_manual_merge", "blocked"] = (
        "blocked" if incomplete else "ready_for_manual_merge"
    )
    next_action = (
        "Complete the listed metadata, review the source and concepts, and manually merge the "
        "repository catalog patch before rerunning the same handoff."
        if incomplete
        else "Review the source and concepts, manually merge the repository catalog patch, then "
        "rerun the same handoff."
    )
    return CatalogUpdateProposal(
        catalog_update_id=_id(
            "catalog_update",
            "0.1.0",
            capture_proposal.proposal_id,
            capture_proposal.proposal_digest,
            capture_proposal_object_digest,
            capture_proposal.base_bundle_digest,
            target_bundle_path,
            capture_proposal.source_candidate,
            promotions,
            incomplete,
        ),
        capture_proposal_id=capture_proposal.proposal_id,
        capture_proposal_digest=capture_proposal.proposal_digest,
        capture_proposal_object_digest=capture_proposal_object_digest,
        base_bundle_digest=capture_proposal.base_bundle_digest,
        target_bundle_path=target_bundle_path,
        source_candidate=capture_proposal.source_candidate,
        concept_promotions=promotions,
        incomplete_concept_metadata=incomplete,
        status=status,
        next_action=next_action,
    )


def canonical_catalog_update_json(proposal: CatalogUpdateProposal) -> bytes:
    return _bytes(proposal) + b"\n"


class CatalogPatchManifest(DomainModel):
    manifest_version: Literal["0.1.0"] = "0.1.0"
    catalog_update_id: str
    base_bundle_digest: str
    target_bundle_path: str
    sources_old_digest: str
    sources_new_digest: str
    concepts_old_digest: str
    concepts_new_digest: str


class CatalogMergeReceipt(DomainModel):
    """Immutable, repository-tracked proof that a catalog patch was merged.

    The receipt is committed at catalog_updates/<catalog_update_id>/receipt.json
    alongside the manual catalog PR.  It is the only cryptographic link between
    the blocked bootstrap Proposal and the merged catalog files.

    ReproposalOrchestrator loads and verifies the receipt from the repository
    checkout.  The receipt object digest (sha256 of exact canonical JSON bytes,
    LF-terminated) binds the reproposal Job identity.
    """

    receipt_version: Literal["0.1.0"] = "0.1.0"
    receipt_id: str = Field(pattern=r"^receipt_[a-f0-9]{32}$")
    catalog_update_id: str = Field(pattern=r"^catalog_update_[a-f0-9]{32}$")
    capture_proposal_id: str = Field(pattern=r"^proposal_[a-f0-9]{32}$")
    capture_proposal_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    capture_proposal_object_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    previous_base_bundle_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    target_bundle_path: str = Field(min_length=1)
    sources_old_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    sources_new_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    concepts_old_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    concepts_new_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_receipt_identity(self) -> CatalogMergeReceipt:
        if (
            self.sources_old_digest == self.sources_new_digest
            and self.concepts_old_digest == self.concepts_new_digest
        ):
            raise ValueError("receipt must represent an actual catalog change")
        expected = _id(
            "receipt",
            "0.1.0",
            self.catalog_update_id,
            self.capture_proposal_id,
            self.capture_proposal_digest,
            self.capture_proposal_object_digest,
            self.previous_base_bundle_digest,
            self.target_bundle_path,
            self.sources_old_digest,
            self.sources_new_digest,
            self.concepts_old_digest,
            self.concepts_new_digest,
        )
        if self.receipt_id != expected:
            raise ValueError("receipt_id does not match receipt contents")
        return self


def canonical_receipt_json(receipt: CatalogMergeReceipt) -> bytes:
    """Canonical, deterministic, LF-terminated receipt bytes."""
    return _bytes(receipt) + b"\n"


def receipt_object_digest(receipt: CatalogMergeReceipt) -> str:
    """Content-addressed identity of the exact canonical receipt bytes."""
    return _byte_digest(canonical_receipt_json(receipt))


RECEIPT_DIR_TEMPLATE = "catalog_updates/{catalog_update_id}"


class CatalogPatch(DomainModel):
    sources_json: str
    concepts_json: str
    manifest: CatalogPatchManifest
    receipt: CatalogMergeReceipt
    review_markdown: str


def _pretty_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def _deterministic_utf8_bytes(value: str) -> bytes:
    """Encode a patch artifact without allowing platform newline conversion."""
    if value.startswith("\ufeff") or "\x00" in value or "\r" in value:
        raise ValueError("INVALID_DETERMINISTIC_PATCH_TEXT")
    if not value.endswith("\n") or value.endswith("\n\n"):
        raise ValueError("INVALID_DETERMINISTIC_PATCH_TERMINATOR")
    return value.encode("utf-8")


def _concept_terms(concept: ConceptEntity) -> set[str]:
    values = (
        concept.canonical_name,
        concept.preferred_english,
        *(item.text for item in concept.aliases),
    )
    return {
        normalize_text(value)
        for value in values
        if value
    }


def prepare_catalog_patch(update: CatalogUpdateProposal, bundle_path: Path) -> CatalogPatch:
    """Build, but never apply, a deterministic reviewed catalog patch."""
    identity = bundle_path_identity(bundle_path)
    if identity != update.target_bundle_path:
        raise ValueError("CATALOG_PATCH_TARGET_MISMATCH")
    bundle = ContractBundle.from_directory(bundle_path)
    if contract_bundle_digest(bundle) != update.base_bundle_digest:
        raise ValueError("STALE_BASE_BUNDLE")
    sources_path = bundle_path / "sources.json"
    concepts_path = bundle_path / "concepts.json"
    sources_before = sources_path.read_bytes()
    concepts_before = concepts_path.read_bytes()
    raw_sources = json.loads(sources_before)
    raw_concepts = json.loads(concepts_before)
    if not isinstance(raw_sources, list) or not isinstance(raw_concepts, list):
        raise ValueError("INVALID_CATALOG_BUNDLE")

    extra_sources = () if update.source_candidate is None else (update.source_candidate.source,)
    extra_concepts = tuple(item.concept for item in update.concept_promotions)
    source_ids = {item.source_id for item in bundle.sources}
    concept_ids = {item.concept_id for item in bundle.concepts}
    if any(item.source_id in source_ids for item in extra_sources) or any(
        item.concept_id in concept_ids for item in extra_concepts
    ):
        raise ValueError("CATALOG_PATCH_ID_COLLISION")
    known_terms = {term for concept in bundle.concepts for term in _concept_terms(concept)}
    promoted_terms: set[str] = set()
    for concept in extra_concepts:
        terms = _concept_terms(concept)
        if terms & (known_terms | promoted_terms):
            raise ValueError("CATALOG_PATCH_ALIAS_COLLISION")
        promoted_terms |= terms

    sources_after = (
        sources_before
        if not extra_sources
        else _pretty_json(
            sorted(
                [
                    *raw_sources,
                    *(item.model_dump(mode="json", exclude_none=True) for item in extra_sources),
                ],
                key=lambda item: item["source_id"],
            )
        ).encode("utf-8")
    )
    concepts_after = (
        concepts_before
        if not extra_concepts
        else _pretty_json(
            sorted(
                [
                    *raw_concepts,
                    *(item.model_dump(mode="json", exclude_none=True) for item in extra_concepts),
                ],
                key=lambda item: item["concept_id"],
            )
        ).encode("utf-8")
    )
    manifest = CatalogPatchManifest(
        catalog_update_id=update.catalog_update_id,
        base_bundle_digest=update.base_bundle_digest,
        target_bundle_path=update.target_bundle_path,
        sources_old_digest=_byte_digest(sources_before),
        sources_new_digest=_byte_digest(sources_after),
        concepts_old_digest=_byte_digest(concepts_before),
        concepts_new_digest=_byte_digest(concepts_after),
    )
    receipt = CatalogMergeReceipt(
        receipt_id=_id(
            "receipt",
            "0.1.0",
            update.catalog_update_id,
            update.capture_proposal_id,
            update.capture_proposal_digest,
            update.capture_proposal_object_digest,
            update.base_bundle_digest,
            update.target_bundle_path,
            manifest.sources_old_digest,
            manifest.sources_new_digest,
            manifest.concepts_old_digest,
            manifest.concepts_new_digest,
        ),
        catalog_update_id=update.catalog_update_id,
        capture_proposal_id=update.capture_proposal_id,
        capture_proposal_digest=update.capture_proposal_digest,
        capture_proposal_object_digest=update.capture_proposal_object_digest,
        previous_base_bundle_digest=update.base_bundle_digest,
        target_bundle_path=update.target_bundle_path,
        sources_old_digest=manifest.sources_old_digest,
        sources_new_digest=manifest.sources_new_digest,
        concepts_old_digest=manifest.concepts_old_digest,
        concepts_new_digest=manifest.concepts_new_digest,
    )
    review = render_catalog_update_markdown(update) + "\n## Patch files\n" + "\n".join(
        (
            f"- sources.json: `{manifest.sources_old_digest}` -> `{manifest.sources_new_digest}`",
            "- concepts.json: "
            f"`{manifest.concepts_old_digest}` -> `{manifest.concepts_new_digest}`",
            "- Apply by manually copying these files into the target bundle on a review branch.",
            "",
        )
    )
    return CatalogPatch(
        sources_json=sources_after.decode("utf-8"),
        concepts_json=concepts_after.decode("utf-8"),
        manifest=manifest,
        receipt=receipt,
        review_markdown=review,
    )


def write_catalog_patch(patch: CatalogPatch, output: Path) -> None:
    """Write only a new output directory; this never changes the source bundle.

    Also writes the immutable receipt to a separate catalog_updates/ directory
    that must be committed alongside the patched catalog files.
    """
    sources_json = _deterministic_utf8_bytes(patch.sources_json)
    concepts_json = _deterministic_utf8_bytes(patch.concepts_json)
    manifest_json = _deterministic_utf8_bytes(_pretty_json(patch.manifest.model_dump()))
    review_markdown = _deterministic_utf8_bytes(patch.review_markdown)
    receipt_json = canonical_receipt_json(patch.receipt)
    output.mkdir(parents=True, exist_ok=False)
    (output / "sources.json").write_bytes(sources_json)
    (output / "concepts.json").write_bytes(concepts_json)
    (output / "manifest.json").write_bytes(manifest_json)
    (output / "review.md").write_bytes(review_markdown)
    # Also write the receipt to the repository-tracked path
    receipt_dir = output.parent / RECEIPT_DIR_TEMPLATE.format(
        catalog_update_id=patch.receipt.catalog_update_id
    )
    receipt_dir.mkdir(parents=True, exist_ok=True)
    (receipt_dir / "receipt.json").write_bytes(receipt_json)


def render_catalog_update_markdown(proposal: CatalogUpdateProposal) -> str:
    lines = [
        "# Catalog bootstrap update",
        "",
        f"- catalog_update_id: `{proposal.catalog_update_id}`",
        f"- capture_proposal_id: `{proposal.capture_proposal_id}`",
        f"- capture_proposal_digest: `{proposal.capture_proposal_digest}`",
        f"- capture_proposal_object_digest: `{proposal.capture_proposal_object_digest}`",
        f"- base_bundle_digest: `{proposal.base_bundle_digest}`",
        f"- target_bundle_path: `{proposal.target_bundle_path}`",
        f"- status: `{proposal.status}`",
        "",
        "## Source candidate",
    ]
    if proposal.source_candidate is None:
        lines.append("- None; the Capture Proposal already references a catalog source.")
    else:
        source = proposal.source_candidate.source
        lines.extend(
            [
                f"- candidate_id: `{proposal.source_candidate.candidate_id}`",
                f"- source_id: `{source.source_id}`",
                f"- source_type: `{source.source_type}`",
                f"- authority: `{source.authority}` (non-authoritative learner provenance)",
                f"- title: {source.title}",
            ]
        )
    lines.extend(["", "## Concept candidates"])
    if not proposal.concept_promotions:
        lines.append("- None")
    for promotion in proposal.concept_promotions:
        lines.append(
            f"- `{promotion.candidate_id}` -> `{promotion.concept.concept_id}` "
            f"({promotion.concept.canonical_name})"
        )
    lines.extend(["", "## Incomplete concept metadata"])
    if not proposal.incomplete_concept_metadata:
        lines.append("- None")
    for item in proposal.incomplete_concept_metadata:
        lines.append(
            f"- `{item.surface_text}` (`{item.resolution_id}`): missing "
            + ", ".join(item.required_fields)
        )
    lines.extend(["", "## Next lifecycle action", proposal.next_action, ""])
    return "\n".join(lines)
