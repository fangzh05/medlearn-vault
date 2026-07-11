from medlearn_vault.domain import ConceptAlias, ConceptEntity
from medlearn_vault.terminology import (
    TerminologyPolicy,
    expand_registered_abbreviations,
    format_concept_label,
)

COPD_ID = "concept_" + "1" * 32


def copd() -> ConceptEntity:
    return ConceptEntity(
        concept_id=COPD_ID,
        canonical_name="慢性阻塞性肺疾病",
        preferred_english="chronic obstructive pulmonary disease",
        concept_type="disease",
        scope_note="test",
        aliases=[ConceptAlias(text="COPD", language="en", alias_type="abbreviation")],
    )


def test_bilingual_abbreviation_label_uses_registered_alias() -> None:
    concept = copd()
    assert format_concept_label(concept, surface_text="copd") == "COPD（慢性阻塞性肺疾病）"
    assert format_concept_label(concept, surface_text="慢性阻塞性肺疾病") == "慢性阻塞性肺疾病"


def test_abbreviation_expands_once_and_not_when_already_expanded() -> None:
    concept = copd()
    text, expanded = expand_registered_abbreviations(
        "COPD 与 COPD", concepts=(concept,), concept_ids=(concept.concept_id,)
    )
    assert text == "COPD（慢性阻塞性肺疾病） 与 COPD"
    repeated, _ = expand_registered_abbreviations(
        "COPD", concepts=(concept,), concept_ids=(concept.concept_id,), already_expanded=expanded
    )
    assert repeated == "COPD"
    existing, _ = expand_registered_abbreviations(
        "COPD（慢性阻塞性肺疾病）",
        concepts=(concept,),
        concept_ids=(concept.concept_id,),
    )
    assert existing == "COPD（慢性阻塞性肺疾病）"


def test_ascii_boundaries_do_not_expand_ms_inside_mssa() -> None:
    concept = ConceptEntity(
        concept_id="concept_" + "2" * 32,
        canonical_name="二尖瓣狭窄",
        concept_type="disease",
        scope_note="test",
        aliases=[ConceptAlias(text="MS", language="en-US", alias_type="abbreviation")],
    )
    text, _ = expand_registered_abbreviations(
        "MSSA 与 MS", concepts=(concept,), concept_ids=(concept.concept_id,)
    )
    assert text == "MSSA 与 MS（二尖瓣狭窄）"


def test_expansion_only_uses_claim_linked_concepts() -> None:
    concept = copd()
    text, _ = expand_registered_abbreviations(
        "COPD 与 PPI", concepts=(concept,), concept_ids=(concept.concept_id,)
    )
    assert text == "COPD（慢性阻塞性肺疾病） 与 PPI"


def test_policy_can_disable_first_use_expansion() -> None:
    assert (
        format_concept_label(
            copd(),
            surface_text="COPD",
            policy=TerminologyPolicy(expand_abbreviation_on_first_use=False),
        )
        == "COPD"
    )


def test_ambiguous_abbreviation_is_not_expanded() -> None:
    first = ConceptEntity(
        concept_id="concept_" + "7" * 32,
        canonical_name="二尖瓣狭窄",
        concept_type="disease",
        scope_note="test",
        aliases=[ConceptAlias(text="MS", language="en", alias_type="abbreviation")],
    )
    second = ConceptEntity(
        concept_id="concept_" + "8" * 32,
        canonical_name="多发性硬化",
        concept_type="disease",
        scope_note="test",
        aliases=[ConceptAlias(text="MS", language="en", alias_type="abbreviation")],
    )
    text, _ = expand_registered_abbreviations(
        "MS", concepts=(first, second), concept_ids=(first.concept_id, second.concept_id)
    )
    assert text == "MS"
