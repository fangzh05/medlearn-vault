from collections.abc import Sequence
from typing import Literal

from medlearn_vault.domain.base import DomainModel
from medlearn_vault.domain.concepts import ConceptEntity
from medlearn_vault.identifiers import normalize_text


class AliasResolution(DomainModel):
    term: str
    status: Literal["resolved", "redirected", "ambiguous", "review_required", "not_found"]
    candidate_concept_ids: tuple[str, ...] = ()
    resolved_concept_id: str | None = None
    lifecycle_reasons: tuple[str, ...] = ()


def resolve_alias(term: str, concepts: Sequence[ConceptEntity]) -> AliasResolution:
    needle = normalize_text(term)
    if not needle:
        return AliasResolution(term=term, status="not_found")
    matches = [
        concept
        for concept in concepts
        if needle == normalize_text(concept.canonical_name)
        or (
            concept.preferred_english is not None
            and needle == normalize_text(concept.preferred_english)
        )
        or any(needle == alias.normalized for alias in concept.aliases)
    ]
    non_deprecated = [concept for concept in matches if concept.status != "deprecated"]
    review = {concept.concept_id for concept in non_deprecated if concept.status == "split_pending"}
    if review:
        return AliasResolution(
            term=term,
            status="review_required",
            candidate_concept_ids=tuple(sorted(concept.concept_id for concept in non_deprecated)),
            lifecycle_reasons=tuple(
                f"{concept.concept_id}:{concept.status}"
                for concept in sorted(non_deprecated, key=lambda item: item.concept_id)
            ),
        )
    active = {concept.concept_id for concept in matches if concept.status == "active"}
    redirects = {
        concept.merged_into
        for concept in matches
        if concept.status == "merged" and concept.merged_into is not None
    }
    candidates = tuple(sorted(active | redirects))
    if len(candidates) == 1:
        return AliasResolution(
            term=term,
            status="redirected" if redirects and not active else "resolved",
            candidate_concept_ids=candidates,
            resolved_concept_id=candidates[0],
        )
    if candidates:
        return AliasResolution(term=term, status="ambiguous", candidate_concept_ids=candidates)
    return AliasResolution(term=term, status="not_found")
