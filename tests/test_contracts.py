from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from medlearn_vault.domain import (
    ConceptAlias,
    ConceptEntity,
    ConceptRelation,
    DisciplineLens,
    LearnerEvidence,
    LearnerState,
    LearningCapture,
    MedicalClaim,
    MisconceptionObservation,
    MisconceptionState,
    SourceCitation,
    SourceDocument,
)
from medlearn_vault.domain.chapters import ChapterDossier, ExamSummary
from medlearn_vault.domain.learner import ConceptMention
from medlearn_vault.domain.sources import PageLocator
from medlearn_vault.identifiers import (
    claim_fingerprint,
    claim_id,
    concept_fingerprint,
    concept_id,
    knowledge_unit_fingerprint,
    normalize_text,
    relation_fingerprint,
    relation_id,
)
from medlearn_vault.registry import preview_merge, resolve_alias

CID = "concept_" + "a" * 32
OTHER_CID = "concept_" + "b" * 32
CLAIM_ID = "claim_" + "c" * 32
RELATION_ID = "relation_" + "d" * 32
SOURCE_ID = "source_" + "e" * 32


def gerd() -> ConceptEntity:
    return ConceptEntity(
        concept_id=CID,
        canonical_name="胃食管反流病",
        preferred_english="gastroesophageal reflux disease",
        concept_type="disease",
        scope_note="本概念指临床诊断实体 GERD，不泛指偶发性生理反流。",
        aliases=[ConceptAlias(text="GERD", language="en", alias_type="abbreviation")],
    )


def citation() -> SourceCitation:
    return SourceCitation(source_id=SOURCE_ID, locator=PageLocator(page=321))


def test_aliases_resolve_to_one_concept() -> None:
    assert resolve_alias("GERD", [gerd()]).resolved_concept_id == CID
    assert resolve_alias("胃食管反流病", [gerd()]).resolved_concept_id == CID


def test_one_concept_can_have_multiple_independent_discipline_lenses() -> None:
    lenses = [
        DisciplineLens(lens_id="lens_internal", concept_id=CID, discipline_id="internal"),
        DisciplineLens(lens_id="lens_surgery", concept_id=CID, discipline_id="surgery"),
    ]
    assert {lens.concept_id for lens in lenses} == {CID}


def test_chapters_share_one_forward_concept_reference() -> None:
    common = dict(
        title="GERD", topic_archetype="disease", concept_ids=[CID], exam_summary=ExamSummary()
    )
    internal = ChapterDossier(
        chapter_id="chapter_i", course_id="im", discipline_id="internal", **common
    )
    surgery = ChapterDossier(
        chapter_id="chapter_s", course_id="su", discipline_id="surgery", **common
    )
    assert internal.concept_ids == surgery.concept_ids == [CID]


def test_ambiguous_terms_return_candidates_without_resolution() -> None:
    concepts = [
        ConceptEntity(
            concept_id=CID, canonical_name="AAA", concept_type="symptom", scope_note="symptom"
        ),
        ConceptEntity(
            concept_id=OTHER_CID, canonical_name="AAA", concept_type="disease", scope_note="disease"
        ),
    ]
    result = resolve_alias("AAA", concepts)
    assert result.status == "ambiguous"
    assert result.resolved_concept_id is None
    assert result.candidate_concept_ids == [CID, OTHER_CID]


def test_opaque_ids_do_not_encode_content() -> None:
    assert concept_id().startswith("concept_")
    assert claim_id().startswith("claim_")
    assert relation_id().startswith("relation_")
    assert concept_id() != concept_id()


def test_fingerprint_golden_contracts() -> None:
    assert concept_fingerprint("disease", "GERD", ["胃食管反流病"]) == "cfp_937087f50d53e173"
    assert claim_fingerprint("PPI treats GERD", [CID]) == "clfp_ffbc701ab9aeadd1"
    assert relation_fingerprint(CID, "treated_by", OTHER_CID) == "relfp_a6a155915e5ac035"
    assert knowledge_unit_fingerprint("treatment", "治疗", [CID]) == "kufp_1452b11937baa408"


def test_fingerprints_normalize_unicode_and_set_order() -> None:
    assert normalize_text("  GＥＲＤ  ") == "gerd"
    assert claim_fingerprint("ＡＢＣ", [OTHER_CID, CID]) == claim_fingerprint(
        "abc", [CID, OTHER_CID]
    )


def test_mutation_keeps_id_and_updates_fingerprint() -> None:
    claim = MedicalClaim(
        claim_id=CLAIM_ID, claim_type="treatment", statement="PPI treats GERD", concept_ids=[CID]
    )
    old_id, old_fingerprint = claim.claim_id, claim.fingerprint
    claim.statement = "PPI improves GERD symptoms"
    assert claim.claim_id == old_id
    assert claim.fingerprint != old_fingerprint


def test_chinese_round_trip_retains_scope_and_display_form() -> None:
    restored = ConceptEntity.model_validate_json(gerd().model_dump_json())
    assert restored == gerd()
    assert restored.aliases[0].text == "GERD"
    assert restored.scope_note.startswith("本概念")


@pytest.mark.parametrize(
    "path", ["C:/vault/note.md", "/vault/note.md", "notes/../secret.md", "..\\secret.md"]
)
def test_invalid_source_paths_rejected(path: str) -> None:
    with pytest.raises(ValidationError):
        SourceDocument(
            source_id=SOURCE_ID, source_type="textbook", title="教材", authority=4, vault_path=path
        )


def test_python_and_json_datetimes_require_timezone() -> None:
    kwargs = dict(
        evidence_id="e1",
        concept_id=CID,
        evidence_type="incorrect",
        confidence=0.8,
        rationale="x",
        message_id="m1",
    )
    with pytest.raises(ValidationError):
        LearnerEvidence(observed_at=datetime(2026, 1, 1), **kwargs)
    payload = (
        f'{{"evidence_id":"e1","concept_id":"{CID}",'
        '"evidence_type":"incorrect","confidence":0.8,"rationale":"x",'
        '"message_id":"m1","observed_at":"2026-01-01T08:00:00"}'
    )
    with pytest.raises(ValidationError):
        LearnerEvidence.model_validate_json(payload)
    assert LearnerEvidence(observed_at=datetime(2026, 1, 1, tzinfo=UTC), **kwargs)


def test_claim_source_invariants_and_authority_ownership() -> None:
    source = SourceDocument(
        source_id=SOURCE_ID, source_type="guideline", title="GERD guideline", authority=5
    )
    claim = MedicalClaim(
        claim_id=CLAIM_ID,
        claim_type="treatment",
        statement="PPI treats GERD",
        concept_ids=[CID],
        evidence_state="supported",
        verification_status="verified_reference",
        citations=[citation()],
    )
    assert source.authority == 5
    assert claim.verification_status == "verified_reference"
    assert "medical_authority" not in MedicalClaim.model_fields
    with pytest.raises(ValidationError):
        MedicalClaim(
            claim_id=CLAIM_ID,
            claim_type="treatment",
            statement="PPI treats GERD",
            concept_ids=[CID],
            verification_status="source_backed",
        )
    with pytest.raises(ValidationError):
        MedicalClaim(
            claim_id=CLAIM_ID,
            claim_type="treatment",
            statement="x",
            concept_ids=[CID],
            course_relevance={"internal": 6},
        )


def test_concept_status_invariants() -> None:
    with pytest.raises(ValidationError):
        ConceptEntity.model_validate(
            {**gerd().model_dump(exclude={"fingerprint"}), "status": "merged", "merged_into": None}
        )
    with pytest.raises(ValidationError):
        gerd().merged_into = OTHER_CID


def test_resolved_mention_requires_one_matching_candidate() -> None:
    assert ConceptMention(
        surface_text="GERD",
        candidate_concept_ids=[CID],
        resolved_concept_id=CID,
        resolution_status="resolved",
        confidence=1,
    )
    with pytest.raises(ValidationError):
        ConceptMention(
            surface_text="MS",
            candidate_concept_ids=[CID],
            resolved_concept_id=CID,
            resolution_status="ambiguous",
            confidence=0.5,
        )


def test_relations_are_independent_edges() -> None:
    relation = ConceptRelation(
        relation_id=RELATION_ID,
        source_concept_id=CID,
        relation_type="treated_by",
        target_concept_id=OTHER_CID,
    )
    assert relation.source_concept_id == CID
    assert "relations" not in ConceptEntity.model_fields


def test_learning_observations_are_separate_from_state() -> None:
    at = datetime(2026, 1, 1, tzinfo=UTC)
    observation = MisconceptionObservation(
        observation_id="obs_1",
        concept_ids=[CID],
        error_logic="PPI is an antacid",
        correct_logic="PPI inhibits the proton pump",
        severity="medium",
        evidence_message_ids=["m1"],
        observed_at=at,
    )
    capture = LearningCapture(
        session_id="session_1",
        source_id=SOURCE_ID,
        captured_at=at,
        discipline_id="internal",
        misconception_observations=[observation],
    )
    state = LearnerState(
        learner_id="learner_1",
        computed_at=at,
        misconception_states=[
            MisconceptionState(
                misconception_id="mis_1",
                concept_ids=[CID],
                first_seen_at=at,
                last_seen_at=at,
                current_status="active",
            )
        ],
    )
    assert "current_status" not in MisconceptionObservation.model_fields
    assert capture.misconception_observations[0].error_logic
    assert state.misconception_states[0].current_status == "active"
    with pytest.raises(ValidationError):
        capture.discipline_id = "surgery"


def test_fingerprint_inputs_cannot_mutate_in_place() -> None:
    concept = gerd()
    with pytest.raises(ValidationError):
        concept.aliases[0].text = "new alias"
    old_fingerprint = concept.fingerprint
    concept.canonical_name = "GERD"
    assert concept.fingerprint != old_fingerprint


def test_merge_preview_is_non_mutating() -> None:
    second = ConceptEntity(
        concept_id=OTHER_CID,
        canonical_name="反流病",
        concept_type="disease",
        scope_note="candidate",
    )
    preview = preview_merge(gerd(), second, target_concept_id=CID)
    assert preview.requires_confirmation is True
    assert second.status == "active"
