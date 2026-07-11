from typing import Literal

from pydantic import Field

from medlearn_vault.domain.base import DomainModel
from medlearn_vault.domain.concepts import ConceptEntity
from medlearn_vault.identifiers import normalize_text


class AliasResolution(DomainModel):
    term: str
    status: Literal["resolved", "ambiguous", "not_found"]
    candidate_concept_ids: list[str] = Field(default_factory=list)
    resolved_concept_id: str | None = None


def resolve_alias(term: str, concepts: list[ConceptEntity]) -> AliasResolution:
    needle = normalize_text(term)
    candidates = sorted(
        {
            concept.concept_id
            for concept in concepts
            if needle == normalize_text(concept.canonical_name)
            or needle == normalize_text(concept.preferred_english or "")
            or any(needle == alias.normalized for alias in concept.aliases)
        }
    )
    if len(candidates) == 1:
        return AliasResolution(
            term=term,
            status="resolved",
            candidate_concept_ids=candidates,
            resolved_concept_id=candidates[0],
        )
    if candidates:
        return AliasResolution(term=term, status="ambiguous", candidate_concept_ids=candidates)
    return AliasResolution(term=term, status="not_found")
