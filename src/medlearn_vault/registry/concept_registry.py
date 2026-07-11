from typing import Literal

from medlearn_vault.domain.base import DomainModel
from medlearn_vault.domain.concepts import ConceptEntity


class ConceptMergePreview(DomainModel):
    operation: Literal["merge"] = "merge"
    source_concept_ids: tuple[str, str]
    target_concept_id: str
    alias_texts_added: list[str]
    requires_confirmation: Literal[True] = True


def preview_merge(
    first: ConceptEntity, second: ConceptEntity, *, target_concept_id: str
) -> ConceptMergePreview:
    if first.concept_id == second.concept_id:
        raise ValueError("merge requires two distinct concepts")
    aliases = {alias.text for concept in (first, second) for alias in concept.aliases}
    aliases.update((first.canonical_name, second.canonical_name))
    return ConceptMergePreview(
        source_concept_ids=(first.concept_id, second.concept_id),
        target_concept_id=target_concept_id,
        alias_texts_added=sorted(aliases),
    )
