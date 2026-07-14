import json

import pytest
from pydantic import ValidationError

from medlearn_vault.capture import CaptureDraft
from medlearn_vault.handoff import (
    MAX_EXCERPT,
    MedLearnHandoff,
    canonical_handoff_json,
    handoff_digest,
    handoff_idempotency_key,
    handoff_submission,
    handoff_to_intake,
)


def payload() -> dict[str, object]:
    return {
        "handoff_version": "0.1.0",
        "session": {
            "title": "血液系统 复习",
            "discipline_id": "medicine",
            "course_id": "internal_medicine",
            "chapter_id": "hematology",
            "session_started_at": "2026-07-13T20:41:00+08:00",
            "captured_at": "2026-07-14T00:20:00+08:00",
        },
        "learning_goals": [],
        "evidence_messages": [
            {
                "local_id": "e001",
                "role": "user",
                "observed_at": None,
                "excerpt": "基因异常无法产生固定 CD55 和 CD59 的锚点 🙂",
                "purpose": "knowledge_answer",
            },
            {
                "local_id": "e002",
                "role": "assistant",
                "observed_at": "2026-07-13T21:00:00+08:00",
                "excerpt": "纠正说明",
                "purpose": "correction",
            },
        ],
        "concepts": [
            {
                "name": "阵发性睡眠性血红蛋白尿",
                "preferred_english": "paroxysmal nocturnal hemoglobinuria",
                "concept_type": "disease",
                "scope_note": None,
                "evidence_local_ids": ["e001"],
            }
        ],
        "claims": [
            {
                "statement": "PIGA 异常导致 GPI 锚缺失",
                "claim_type": "mechanism",
                "concept_terms": ["PNH", "PIGA"],
                "evidence_local_ids": ["e001"],
                "question_priority": "medium",
            }
        ],
        "learner_evidence": [
            {
                "concept_terms": ["PNH 机制"],
                "evidence_type": "correct_independent",
                "confidence": 0.9,
                "rationale": "用户独立说明",
                "evidence_local_ids": ["e001"],
            }
        ],
        "misconceptions": [
            {
                "observed_error_logic": "误认为补体不是原因",
                "concept_terms": ["PNH"],
                "observed_error_local_ids": ["e001"],
                "correction_local_ids": ["e002"],
                "proposed_correction": "CD55/CD59 缺失增加补体敏感性",
                "correction_terms": ["CD55"],
                "severity": "medium",
            }
        ],
        "unresolved_questions": [
            {
                "statement": "为何夜间更明显？",
                "concept_terms": ["PNH"],
                "evidence_local_ids": ["e001"],
                "question_priority": "medium",
            }
        ],
        "unfinished_topics": [],
    }


def test_handoff_is_utf8_deterministic_and_revalidates_draft() -> None:
    handoff = MedLearnHandoff.model_validate(payload())
    first, key = handoff_submission(handoff)
    second, second_key = handoff_submission(
        MedLearnHandoff.model_validate(json.loads(canonical_handoff_json(handoff)))
    )
    assert first == second
    assert key == second_key == handoff_idempotency_key(handoff)
    assert handoff_digest(handoff).startswith("sha256:")
    draft = handoff_to_intake(handoff).draft
    assert CaptureDraft.model_validate_json(draft.model_dump_json()) == draft
    assert draft.context.source_id == f"source_{handoff_digest(handoff)[7:39]}"
    assert draft.evidence_messages[0].observed_at == draft.context.captured_at
    assert (
        handoff_to_intake(handoff).draft.evidence_messages[0].message_id
        == draft.evidence_messages[0].message_id
    )


def test_minimal_handoff_and_all_learner_evidence_types_are_valid() -> None:
    minimal = payload()
    for key in [
        "learning_goals",
        "evidence_messages",
        "concepts",
        "claims",
        "learner_evidence",
        "misconceptions",
        "unresolved_questions",
        "unfinished_topics",
    ]:
        minimal[key] = []
    assert handoff_to_intake(MedLearnHandoff.model_validate(minimal)).draft.evidence_messages == ()

    for evidence_type in [
        "correct_independent",
        "correct_after_hint",
        "guessed_correct",
        "partial",
        "unknown",
        "incorrect",
        "high_confidence_incorrect",
        "self_report_only",
    ]:
        value = payload()
        value["learner_evidence"][0]["evidence_type"] = evidence_type  # type: ignore[index]
        assert (
            MedLearnHandoff.model_validate(value).learner_evidence[0].evidence_type == evidence_type
        )


def test_unmappable_handoff_metadata_fails_instead_of_being_dropped() -> None:
    for key, value in [
        ("learning_goals", ["理解 PNH 机制"]),
        ("unfinished_topics", [{"title": "流式细胞术", "evidence_local_ids": ["e001"]}]),
    ]:
        source = payload()
        source[key] = value
        with pytest.raises(ValueError, match="HANDOFF_CONVERSION_FAILURE"):
            handoff_to_intake(MedLearnHandoff.model_validate(source))


def test_handoff_rejects_duplicate_dangling_invalid_types_and_extra_fields() -> None:
    duplicate = payload()
    duplicate["evidence_messages"] = [
        *duplicate["evidence_messages"],
        {**duplicate["evidence_messages"][0]},
    ]  # type: ignore[index]
    dangling = payload()
    dangling["concepts"][0]["evidence_local_ids"] = ["missing"]  # type: ignore[index]
    invalid_concept = payload()
    invalid_concept["concepts"][0]["concept_type"] = "not-a-concept"  # type: ignore[index]
    invalid_evidence = payload()
    invalid_evidence["learner_evidence"][0]["evidence_type"] = "invented"  # type: ignore[index]
    invalid_severity = payload()
    invalid_severity["misconceptions"][0]["severity"] = "urgent"  # type: ignore[index]
    empty = payload()
    empty["evidence_messages"][0]["excerpt"] = ""  # type: ignore[index]
    extra = payload()
    extra["unexpected"] = True
    for value in [
        duplicate,
        dangling,
        invalid_concept,
        invalid_evidence,
        invalid_severity,
        empty,
        extra,
    ]:
        with pytest.raises(ValidationError):
            MedLearnHandoff.model_validate(value)


def test_handoff_rejects_long_excerpt_and_accepts_misconception_without_correction() -> None:
    too_long = payload()
    too_long["evidence_messages"][0]["excerpt"] = "x" * (MAX_EXCERPT + 1)  # type: ignore[index]
    with pytest.raises(ValidationError):
        MedLearnHandoff.model_validate(too_long)
    no_correction = payload()
    no_correction["misconceptions"][0].update(
        {"correction_local_ids": [], "proposed_correction": None, "correction_terms": []}
    )  # type: ignore[index]
    assert (
        MedLearnHandoff.model_validate(no_correction).misconceptions[0].proposed_correction is None
    )
