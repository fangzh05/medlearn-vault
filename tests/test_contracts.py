from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from medlearn_vault.domain import (
    ConceptAlias,
    ConceptEntity,
    DisciplineLens,
    LearnerEvidence,
    MedicalClaim,
    SourceCitation,
)
from medlearn_vault.domain.chapters import ChapterDossier, ExamSummary
from medlearn_vault.identifiers import claim_id, concept_id, knowledge_unit_id, relation_id
from medlearn_vault.registry import preview_merge, resolve_alias


def gerd() -> ConceptEntity:
    cid = "disease_gerd"
    return ConceptEntity(
        concept_id=cid,
        canonical_name="胃食管反流病",
        preferred_english="gastroesophageal reflux disease",
        concept_type="disease",
        aliases=[ConceptAlias(text="GERD", language="en", alias_type="abbreviation")],
        discipline_lenses=[
            DisciplineLens(lens_id="internal", concept_id=cid, discipline_id="internal_medicine"),
            DisciplineLens(lens_id="surgery", concept_id=cid, discipline_id="surgery"),
        ],
    )


def test_aliases_resolve_to_one_concept() -> None:
    concepts = [gerd()]
    assert resolve_alias("GERD", concepts).resolved_concept_id == "disease_gerd"
    assert resolve_alias("胃食管反流病", concepts).resolved_concept_id == "disease_gerd"


def test_one_concept_has_multiple_discipline_lenses() -> None:
    assert {lens.discipline_id for lens in gerd().discipline_lenses} == {
        "internal_medicine",
        "surgery",
    }


def test_chapters_share_canonical_concept() -> None:
    common = dict(
        title="GERD",
        topic_archetype="disease",
        primary_concept_ids=["disease_gerd"],
        exam_summary=ExamSummary(),
    )
    internal = ChapterDossier(
        chapter_id="i", course_id="im", discipline_id="internal_medicine", **common
    )
    surgery = ChapterDossier(chapter_id="s", course_id="su", discipline_id="surgery", **common)
    assert internal.primary_concept_ids == surgery.primary_concept_ids


def test_ambiguous_terms_return_candidates() -> None:
    concepts = [
        ConceptEntity(concept_id="symptom_aaa", canonical_name="AAA", concept_type="symptom"),
        ConceptEntity(concept_id="disease_aaa", canonical_name="AAA", concept_type="disease"),
    ]
    result = resolve_alias("AAA", concepts)
    assert result.status == "ambiguous"
    assert result.resolved_concept_id is None
    assert len(result.candidate_concept_ids) == 2


def test_stable_ids_are_stable() -> None:
    functions = [
        lambda: concept_id("胃食管反流病", "disease"),
        lambda: claim_id("PPI improves symptoms", ["disease_gerd"]),
        lambda: relation_id("disease_gerd", "treated_by", "drug_class_ppi"),
        lambda: knowledge_unit_id("treatment", "治疗", ["disease_gerd"]),
    ]
    assert all(fn() == fn() for fn in functions)


def test_chinese_round_trip_and_display_form() -> None:
    original = gerd()
    restored = ConceptEntity.model_validate_json(original.model_dump_json())
    assert restored == original
    assert restored.canonical_name == "胃食管反流病"
    assert restored.aliases[0].text == "GERD"
    assert restored.aliases[0].normalized == "gerd"


@pytest.mark.parametrize(
    "path", ["C:/vault/note.md", "/vault/note.md", "notes/../secret.md", "..\\secret.md"]
)
def test_invalid_vault_paths_rejected(path: str) -> None:
    with pytest.raises(ValidationError):
        SourceCitation(source_id="src", locator="p1", vault_path=path)


def test_all_datetimes_require_timezone() -> None:
    kwargs = dict(
        evidence_id="e1",
        concept_id="disease_gerd",
        evidence_type="incorrect",
        confidence=0.8,
        rationale="x",
        message_id="m1",
    )
    with pytest.raises(ValidationError):
        LearnerEvidence(observed_at=datetime(2026, 1, 1), **kwargs)
    assert LearnerEvidence(observed_at=datetime(2026, 1, 1, tzinfo=UTC), **kwargs)


def test_learning_evidence_cannot_change_claim_truth() -> None:
    statement = "PPI treats GERD"
    claim = MedicalClaim(
        claim_id=claim_id(statement, ["disease_gerd"]),
        claim_type="treatment",
        statement=statement,
        concept_ids=["disease_gerd"],
        verification_status="source_backed",
        medical_authority=4,
    )
    evidence = LearnerEvidence(
        evidence_id="e1",
        concept_id="disease_gerd",
        evidence_type="incorrect",
        confidence=0.9,
        rationale="learner confused classes",
        message_id="m1",
        observed_at=datetime.now(UTC),
    )
    assert evidence.model_dump().keys().isdisjoint({"truth_value", "statement", "citations"})
    assert claim.truth_value == "supported"


def test_lens_must_reference_enclosing_concept() -> None:
    with pytest.raises(ValidationError):
        ConceptEntity(
            concept_id="disease_gerd",
            canonical_name="GERD",
            concept_type="disease",
            discipline_lenses=[
                DisciplineLens(lens_id="x", concept_id="other_concept", discipline_id="surgery")
            ],
        )


def test_concept_merge_is_preview_only() -> None:
    first = ConceptEntity(concept_id="disease_aaa", canonical_name="AAA", concept_type="disease")
    second = ConceptEntity(
        concept_id="disease_bbb", canonical_name="三A综合征", concept_type="disease"
    )
    preview = preview_merge(first, second, target_concept_id="disease_aaa")
    assert preview.requires_confirmation is True
    assert preview.source_concept_ids == ("disease_aaa", "disease_bbb")
    assert first.status == second.status == "active"
