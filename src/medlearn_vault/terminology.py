import re
from typing import Literal

from medlearn_vault.domain.base import DomainModel
from medlearn_vault.domain.concepts import ConceptEntity
from medlearn_vault.domain.ids import ConceptId
from medlearn_vault.identifiers import normalize_text


class TerminologyPolicy(DomainModel):
    locale: Literal["zh-CN"] = "zh-CN"
    expand_abbreviation_on_first_use: bool = True
    use_fullwidth_parentheses: bool = True


DEFAULT_POLICY = TerminologyPolicy()


def english_abbreviations(concept: ConceptEntity) -> tuple[str, ...]:
    return tuple(
        alias.text
        for alias in concept.aliases
        if alias.alias_type == "abbreviation"
        and (alias.language.casefold() == "en" or alias.language.casefold().startswith("en-"))
    )


def format_concept_label(
    concept: ConceptEntity,
    *,
    surface_text: str | None = None,
    policy: TerminologyPolicy = DEFAULT_POLICY,
) -> str:
    abbreviations = english_abbreviations(concept)
    normalized_surface = normalize_text(surface_text or "")
    abbreviation = next(
        (item for item in abbreviations if normalize_text(item) == normalized_surface),
        (
            abbreviations[0]
            if abbreviations
            and (
                surface_text is None
                or normalized_surface == normalize_text(concept.preferred_english or "")
            )
            else None
        ),
    )
    if abbreviation is None:
        return concept.canonical_name
    if not policy.expand_abbreviation_on_first_use:
        return abbreviation
    left, right = ("（", "）") if policy.use_fullwidth_parentheses else ("(", ")")
    return f"{abbreviation}{left}{concept.canonical_name}{right}"


def expand_registered_abbreviations(
    text: str,
    *,
    concepts: tuple[ConceptEntity, ...],
    concept_ids: tuple[ConceptId, ...],
    already_expanded: set[ConceptId] | None = None,
    policy: TerminologyPolicy = DEFAULT_POLICY,
) -> tuple[str, set[ConceptId]]:
    expanded = set(already_expanded or ())
    by_id = {concept.concept_id: concept for concept in concepts}
    abbreviation_owners: dict[str, set[ConceptId]] = {}
    for concept_id in concept_ids:
        concept = by_id.get(concept_id)
        if concept is not None:
            for abbreviation in english_abbreviations(concept):
                abbreviation_owners.setdefault(normalize_text(abbreviation), set()).add(concept_id)
    result = text
    for concept_id in concept_ids:
        concept = by_id.get(concept_id)
        if concept is None:
            continue
        for abbreviation in sorted(english_abbreviations(concept), key=len, reverse=True):
            if len(abbreviation_owners[normalize_text(abbreviation)]) > 1:
                continue
            pattern = re.compile(
                rf"(?<![A-Za-z0-9]){re.escape(abbreviation)}(?![A-Za-z0-9])",
                re.IGNORECASE,
            )

            def replace(
                match: re.Match[str],
                current: str = result,
                selected: ConceptEntity = concept,
                selected_id: ConceptId = concept_id,
                registered: str = abbreviation,
            ) -> str:
                end = match.end()
                suffixes = (
                    f"（{selected.canonical_name}）",
                    f"({selected.canonical_name})",
                )
                if selected_id in expanded or current[end:].startswith(suffixes):
                    expanded.add(selected_id)
                    return registered
                expanded.add(selected_id)
                return format_concept_label(selected, surface_text=registered, policy=policy)

            result = pattern.sub(replace, result)
    return result, expanded


def has_chinese_display_name(concept: ConceptEntity) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", concept.canonical_name))
