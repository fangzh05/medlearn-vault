"""Deterministic, repository-reviewed bootstrap proposals for sources and concepts."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import Field, model_validator

from medlearn_vault.capture import (
    CaptureProposal,
    LearningChatSourceCandidate,
    NewConceptCandidate,
)
from medlearn_vault.domain.base import DomainModel
from medlearn_vault.domain.concepts import ConceptEntity
from medlearn_vault.domain.ids import ConceptId


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
    source_candidate: LearningChatSourceCandidate | None = None
    concept_promotions: tuple[CatalogConceptPromotion, ...] = ()
    incomplete_concept_metadata: tuple[IncompleteConceptMetadata, ...] = ()
    status: Literal["ready_for_manual_merge", "blocked"]
    next_action: str

    @model_validator(mode="after")
    def validate_identity_and_manual_boundary(self) -> CatalogUpdateProposal:
        if self.catalog_update_id != _id(
            "catalog_update",
            self.catalog_update_version,
            self.capture_proposal_id,
            self.capture_proposal_digest,
            self.capture_proposal_object_digest,
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
    capture_proposal: CaptureProposal, *, capture_proposal_object_digest: str
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
            capture_proposal.source_candidate,
            promotions,
            incomplete,
        ),
        capture_proposal_id=capture_proposal.proposal_id,
        capture_proposal_digest=capture_proposal.proposal_digest,
        capture_proposal_object_digest=capture_proposal_object_digest,
        source_candidate=capture_proposal.source_candidate,
        concept_promotions=promotions,
        incomplete_concept_metadata=incomplete,
        status=status,
        next_action=next_action,
    )


def canonical_catalog_update_json(proposal: CatalogUpdateProposal) -> bytes:
    return _bytes(proposal) + b"\n"


def render_catalog_update_markdown(proposal: CatalogUpdateProposal) -> str:
    lines = [
        "# Catalog bootstrap update",
        "",
        f"- catalog_update_id: `{proposal.catalog_update_id}`",
        f"- capture_proposal_id: `{proposal.capture_proposal_id}`",
        f"- capture_proposal_digest: `{proposal.capture_proposal_digest}`",
        f"- capture_proposal_object_digest: `{proposal.capture_proposal_object_digest}`",
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
