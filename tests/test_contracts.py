import json
from datetime import UTC, datetime
from pathlib import Path

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
from medlearn_vault.domain.chapters import ChapterDossier, ExamSummary, KnowledgeUnit
from medlearn_vault.domain.claims import CourseRelevance
from medlearn_vault.domain.learner import ConceptMention
from medlearn_vault.domain.sources import PageLocator
from medlearn_vault.identifiers import (
    claim_fingerprint,
    claim_id,
    concept_fingerprint,
    concept_id,
    knowledge_unit_fingerprint,
    lens_id,
    normalize_text,
    relation_fingerprint,
    relation_id,
    source_id,
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
        DisciplineLens(lens_id="lens_" + "1" * 32, concept_id=CID, discipline_id="internal"),
        DisciplineLens(lens_id="lens_" + "2" * 32, concept_id=CID, discipline_id="surgery"),
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
    assert internal.concept_ids == surgery.concept_ids == (CID,)


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
    assert result.candidate_concept_ids == (CID, OTHER_CID)


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
    old_id, old_fingerprint = claim.claim_id, claim.match_fingerprint
    claim.statement = "PPI improves GERD symptoms"
    assert claim.claim_id == old_id
    assert claim.match_fingerprint != old_fingerprint


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


@pytest.mark.parametrize("path", [r"C:relative\file.md", "notes/CON.txt", "notes/a:b.md"])
def test_windows_unsafe_relative_paths_rejected(path: str) -> None:
    with pytest.raises(ValidationError):
        SourceDocument(
            source_id=SOURCE_ID,
            source_type="textbook",
            title="教材",
            authority=4,
            vault_path=path,
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
        observed_error_logic="PPI is an antacid",
        proposed_correction="PPI inhibits the proton pump",
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
    assert capture.misconception_observations[0].observed_error_logic
    assert state.misconception_states[0].current_status == "active"
    with pytest.raises(ValidationError):
        capture.discipline_id = "surgery"


def test_fingerprint_inputs_cannot_mutate_in_place() -> None:
    concept = gerd()
    with pytest.raises(ValidationError):
        concept.aliases[0].text = "new alias"
    old_fingerprint = concept.match_fingerprint
    concept.canonical_name = "GERD"
    assert concept.match_fingerprint != old_fingerprint


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


@pytest.mark.parametrize(
    ("verification_status", "evidence_state"),
    [
        ("unverified_chat", "refuted"),
        ("unverified_chat", "conflicting"),
        ("verified_reference", "unassessed"),
        ("conflicted", "supported"),
    ],
)
def test_claim_state_matrix_rejects_contradictions(
    verification_status: str, evidence_state: str
) -> None:
    citations = [citation()] if verification_status != "unverified_chat" else []
    with pytest.raises(ValidationError):
        MedicalClaim(
            claim_id=CLAIM_ID,
            claim_type="treatment",
            statement="PPI treats GERD",
            concept_ids=[CID],
            verification_status=verification_status,
            evidence_state=evidence_state,
            citations=citations,
        )


def test_invariant_collections_cannot_mutate_in_place() -> None:
    claim = MedicalClaim(
        claim_id=CLAIM_ID,
        claim_type="treatment",
        statement="candidate",
        concept_ids=[CID],
        course_relevance=[CourseRelevance(course_id="internal", score=5)],
    )
    chapter = ChapterDossier(
        chapter_id="chapter_1",
        course_id="course_1",
        discipline_id="internal",
        title="GERD",
        topic_archetype="disease",
        concept_ids=[CID],
        exam_summary=ExamSummary(),
    )
    with pytest.raises(TypeError):
        claim.course_relevance[0] = CourseRelevance(course_id="internal", score=4)
    with pytest.raises(AttributeError):
        chapter.concept_ids.clear()  # type: ignore[attr-defined]


def test_match_fingerprint_and_content_hash_have_distinct_jobs() -> None:
    concept = gerd()
    match_before, content_before = concept.match_fingerprint, concept.content_hash
    concept.scope_note = "新的语义边界"
    assert concept.match_fingerprint == match_before
    assert concept.content_hash != content_before

    unit = KnowledgeUnit(
        unit_id="unit_" + "f" * 32,
        unit_type="definition",
        title="定义",
        concept_ids=[CID],
        content={"text": "before"},
    )
    unit_hash = unit.content_hash
    unit.content = {"text": "after"}
    assert unit.content_hash != unit_hash


def test_chapter_rejects_out_of_scope_unit_concept() -> None:
    unit = KnowledgeUnit(
        unit_id="unit_" + "f" * 32,
        unit_type="definition",
        title="定义",
        concept_ids=[OTHER_CID],
        content="x",
    )
    with pytest.raises(ValidationError):
        ChapterDossier(
            chapter_id="chapter_1",
            course_id="course_1",
            discipline_id="internal",
            title="GERD",
            topic_archetype="disease",
            concept_ids=[CID],
            knowledge_units=[unit],
            exam_summary=ExamSummary(),
        )


def test_alias_resolver_handles_blank_and_lifecycle() -> None:
    assert resolve_alias("  ", [gerd()]).status == "not_found"
    merged = gerd().model_copy(update={"status": "merged", "merged_into": OTHER_CID})
    redirected = resolve_alias("GERD", [merged])
    assert redirected.status == "redirected"
    assert redirected.resolved_concept_id == OTHER_CID
    deprecated = gerd().model_copy(update={"status": "deprecated"})
    assert resolve_alias("GERD", [deprecated]).status == "not_found"


def test_relation_sources_flow_through_claim_ids() -> None:
    relation = ConceptRelation(
        relation_id=RELATION_ID,
        source_concept_id=CID,
        relation_type="treated_by",
        target_concept_id=OTHER_CID,
        supporting_claim_ids=[CLAIM_ID],
    )
    assert relation.supporting_claim_ids == (CLAIM_ID,)
    assert "citations" not in ConceptRelation.model_fields


def test_source_and_lens_id_helpers_follow_one_style() -> None:
    assert source_id().startswith("source_")
    assert lens_id().startswith("lens_")


def test_migrated_gerd_fixture_matches_contract_boundaries() -> None:
    root = Path("examples/gerd")

    def load(name: str) -> object:
        return json.loads((root / name).read_text(encoding="utf-8"))

    sources = [SourceDocument.model_validate(item) for item in load("sources.json")]
    concepts = [ConceptEntity.model_validate(item) for item in load("concepts.json")]
    claims = [MedicalClaim.model_validate(item) for item in load("claims.json")]
    relations = [ConceptRelation.model_validate(item) for item in load("relations.json")]
    lenses = [DisciplineLens.model_validate(item) for item in load("discipline_lenses.json")]
    chapters = [ChapterDossier.model_validate(item) for item in load("chapters.json")]
    capture = LearningCapture.model_validate(load("learning_capture.json"))

    assert len(sources) == 1
    assert len(concepts) == 2
    assert relations[0].supporting_claim_ids == (claims[0].claim_id,)
    assert {lens.concept_id for lens in lenses} == {concepts[0].concept_id}
    assert {chapter.concept_ids[0] for chapter in chapters} == {concepts[0].concept_id}
    assert capture.misconception_observations[0].correction_claim_ids == (claims[0].claim_id,)
