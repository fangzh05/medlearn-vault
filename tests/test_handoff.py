import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from medlearn_vault.bundle import ContractBundle
from medlearn_vault.capture import (
    CaptureContext,
    CaptureDraft,
    IntakeEnvelope,
    build_capture_proposal,
    extract_capture_draft,
    intake_envelope_digest,
    learning_chat_source_id,
    legacy_learning_chat_source_id,
)
from medlearn_vault.domain.sources import SourceDocument
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
    assert draft.context.source_id == learning_chat_source_id(draft.context)
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


def test_handoff_rejects_mixed_role_assertion_evidence_before_conversion() -> None:
    value = payload()
    value["claims"][0]["evidence_local_ids"] = ["e001", "e002"]  # type: ignore[index]
    with pytest.raises(ValidationError, match="assertion evidence must have exactly one"):
        MedLearnHandoff.model_validate(value)


def test_synthetic_nonempty_handoff_converts_to_a_valid_intake_envelope() -> None:
    fixture = Path("examples/intake/project-handoff-synthetic.json")
    source = json.loads(fixture.read_text(encoding="utf-8"))
    handoff = MedLearnHandoff.model_validate(source)
    exact, _ = handoff_submission(handoff)
    envelope = IntakeEnvelope.model_validate_json(exact)
    assert extract_capture_draft(exact, intake_envelope_digest(exact))
    assert len(envelope.draft.concept_mentions) == 5
    assert len(envelope.draft.claim_candidates) == 4
    assert len(envelope.draft.learner_evidence_candidates) == 1
    assert len(envelope.draft.misconception_candidates) == 1


def test_apl_worker_python_source_identity_golden_bootstraps_a_candidate() -> None:
    handoff = MedLearnHandoff.model_validate_json(
        Path("examples/intake/apl-bootstrap-sanitized.json").read_text(encoding="utf-8")
    )
    golden = json.loads(
        Path("examples/intake/apl-bootstrap-identity.json").read_text(encoding="utf-8")
    )
    exact, _ = handoff_submission(handoff)
    envelope = IntakeEnvelope.model_validate_json(exact)
    assert envelope.draft.context.session_id == golden["session_id"]
    assert envelope.draft.context.session_id == golden["session_id"]
    assert envelope.draft.context.source_id == golden["stable_source_id"]
    assert envelope.draft.context.source_id != golden["source_id"]
    assert intake_envelope_digest(exact) == golden["intake_sha256"]
    nullable = CaptureContext(
        source_id="source_00000000000000000000000000000000",
        **golden["nullable_context"],
    )
    assert legacy_learning_chat_source_id(nullable) == golden["nullable_source_id"]
    assert learning_chat_source_id(nullable) == golden["stable_nullable_source_id"]
    worker_exact = Path("examples/intake/apl-bootstrap-worker-envelope.json").read_bytes()
    worker_envelope = IntakeEnvelope.model_validate_json(worker_exact)
    assert intake_envelope_digest(worker_exact) == golden["worker_intake_sha256"]
    assert extract_capture_draft(worker_exact, intake_envelope_digest(worker_exact))
    assert worker_envelope.draft.context.source_id == golden["stable_source_id"]
    proposal = build_capture_proposal(
        ContractBundle.from_directory(Path("examples/copd")), worker_envelope.draft
    )
    assert proposal.source_candidate is not None
    assert "MISSING_SOURCE" not in {issue.code for issue in proposal.issues}


def test_learning_chat_source_is_stable_across_capture_timestamps() -> None:
    first_payload = payload()
    second_payload = payload()
    for value in (first_payload, second_payload):
        for key in [
            "evidence_messages",
            "concepts",
            "claims",
            "learner_evidence",
            "misconceptions",
            "unresolved_questions",
        ]:
            value[key] = []
    second_payload["session"]["session_started_at"] = "2026-07-13T21:41:00+08:00"  # type: ignore[index]
    second_payload["session"]["captured_at"] = "2026-07-14T01:20:00+08:00"  # type: ignore[index]

    first = handoff_to_intake(MedLearnHandoff.model_validate(first_payload)).draft
    second = handoff_to_intake(MedLearnHandoff.model_validate(second_payload)).draft

    assert first.context.session_id != second.context.session_id
    assert first.context.captured_at != second.context.captured_at
    assert first.context.source_id == second.context.source_id

    base = ContractBundle.from_directory(Path("examples/copd"))
    bundle = base.model_copy(
        update={
            "sources": (
                *base.sources,
                SourceDocument(
                    source_id=first.context.source_id,
                    source_type="learning_chat",
                    title="Learning chat captures for medicine / internal_medicine / hematology",
                    authority=0,
                ),
            )
        }
    )
    proposal = build_capture_proposal(bundle, second)
    assert proposal.source_candidate is None
    assert "CATALOG_UPDATE_REQUIRED" not in {issue.code for issue in proposal.issues}


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


# ── Converter v4 namespace tests ────────────────────────────────────────


def test_converter_v4_idempotency_key_is_stable_golden() -> None:
    """The v4 idempotency key must be deterministic across platforms."""
    from medlearn_vault.handoff import HANDOFF_CONVERSION_VERSION

    assert HANDOFF_CONVERSION_VERSION == "medlearn.handoff_to_intake.v4"
    handoff = MedLearnHandoff.model_validate(payload())
    key = handoff_idempotency_key(handoff)
    assert key.startswith("medlearn-handoff-v4-")
    # The key must be 64 hex chars after the prefix.
    assert len(key) == len("medlearn-handoff-v4-") + 64
    assert key == "medlearn-handoff-v4-" + handoff_digest(handoff)[7:]


def test_converter_v4_idempotency_key_differs_from_older_namespaces() -> None:
    """The v4 key must use a different prefix namespace than v1/v2/v3."""
    handoff = MedLearnHandoff.model_validate(payload())
    v4_key = handoff_idempotency_key(handoff)
    # The old v1 key would have been medlearn-handoff-<digest>
    v1_key = "medlearn-handoff-" + handoff_digest(handoff)[7:]
    v2_key = "medlearn-handoff-v2-" + handoff_digest(handoff)[7:]
    v3_key = "medlearn-handoff-v3-" + handoff_digest(handoff)[7:]
    assert v1_key != v4_key
    assert v2_key != v4_key
    assert v3_key != v4_key
    assert v4_key.startswith("medlearn-handoff-v4-")
    assert v3_key.startswith("medlearn-handoff-v3-")
    assert v1_key.startswith("medlearn-handoff-")
    assert v2_key.startswith("medlearn-handoff-v2-")
    # Both share the same semantic digest portion after the prefix
    assert v4_key[len("medlearn-handoff-v4-") :] == v1_key[len("medlearn-handoff-") :]


def test_converter_v4_intake_envelope_is_lf_only() -> None:
    """Exact intake envelope bytes must be LF-only (no CR, no CRLF)."""
    handoff = MedLearnHandoff.model_validate(payload())
    exact, _ = handoff_submission(handoff)
    text = exact.decode("utf-8")
    assert "\r" not in text
    # The Python converter produces compact JSON; the Worker appends \n.
    # In both cases the bytes are CR-free.
    assert b"\r\n" not in exact


def test_converter_v4_splits_multi_term_learner_evidence() -> None:
    source = payload()
    source["learner_evidence"][0]["concept_terms"] = ["HLA-B27诊断边界", "强直性脊柱炎"]  # type: ignore[index]
    draft = handoff_to_intake(MedLearnHandoff.model_validate(source)).draft

    assert [item.concept_terms for item in draft.learner_evidence_candidates] == [
        ("HLA-B27诊断边界",),
        ("强直性脊柱炎",),
    ]
    assert {item.evidence_message_ids for item in draft.learner_evidence_candidates} == {
        draft.learner_evidence_candidates[0].evidence_message_ids
    }


def test_converter_v4_no_random_nonce_in_key() -> None:
    """The idempotency key must be purely deterministic — no UUID, nonce, or random."""
    handoff = MedLearnHandoff.model_validate(payload())
    first = handoff_idempotency_key(handoff)
    second = handoff_idempotency_key(handoff)
    third = handoff_idempotency_key(
        MedLearnHandoff.model_validate(json.loads(canonical_handoff_json(handoff)))
    )
    assert first == second == third
