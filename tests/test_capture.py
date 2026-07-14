import json
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from medlearn_vault.bundle import ContractBundle
from medlearn_vault.capture import (
    CaptureDraft,
    CaptureProposal,
    build_capture_proposal,
    capture_draft_digest,
    capture_proposal_digest,
    materialize_learning_capture,
    render_capture_proposal_markdown,
)
from medlearn_vault.cli import app
from medlearn_vault.domain import LearnerEvidence, LearningCapture


def draft_payload(name: str = "copd-session") -> dict[str, object]:
    return json.loads(Path(f"examples/capture/{name}/draft.json").read_text(encoding="utf-8"))


def draft(name: str = "copd-session") -> CaptureDraft:
    return CaptureDraft.model_validate(draft_payload(name))


def test_capture_golden_is_deterministic_review_only_and_materializable() -> None:
    bundle = ContractBundle.from_directory(Path("examples/copd"))
    value = draft()
    before_bundle, before_draft = bundle.model_dump(), value.model_dump()
    first = build_capture_proposal(bundle, value)
    second = build_capture_proposal(bundle, value)
    assert first == second
    assert first.status == "ready_for_review"
    assert first.claim_proposals[0].proposed_verification_status == "unverified_chat"
    assert first.claim_proposals[0].matching_existing_claim_ids
    observation = first.learning_capture_candidate.observations[0]
    assert observation.observation_type == "misconception"
    assert observation.correction_claim_ids
    materialized = materialize_learning_capture(bundle, first)
    assert isinstance(materialized, LearningCapture)
    assert materialized == first.learning_capture_candidate.capture
    misconception = materialized.misconception_observations[0]
    assert misconception.observed_at.isoformat() == "2026-07-11T09:55:00+08:00"
    assert misconception.severity == "high"
    assert misconception.concept_ids
    assert misconception.correction_claim_ids
    assert bundle.model_dump() == before_bundle
    assert value.model_dump() == before_draft
    assert capture_draft_digest(value) == capture_draft_digest(value.model_copy())
    assert capture_proposal_digest(first) == first.proposal_digest
    assert "COPD（慢性阻塞性肺疾病）" in render_capture_proposal_markdown(first, bundle=bundle)
    expected = CaptureProposal.model_validate_json(
        Path("examples/capture/copd-session/expected_proposal.json").read_text(encoding="utf-8")
    )
    assert first == expected
    assert render_capture_proposal_markdown(first, bundle=bundle) == Path(
        "examples/capture/copd-session/expected_review.md"
    ).read_text(encoding="utf-8")


def test_forged_user_to_assistant_role_relabeling_is_rejected() -> None:
    payload = draft_payload()
    claim = payload["claim_candidates"][0]  # type: ignore[index]
    claim["evidence_message_ids"] = ["message:user-001"]  # type: ignore[index]
    claim["speaker_role"] = "assistant"  # type: ignore[index]
    with pytest.raises(ValidationError):
        CaptureDraft.model_validate(payload)


def test_mixed_role_assertion_evidence_is_rejected() -> None:
    payload = draft_payload()
    claim = payload["claim_candidates"][0]  # type: ignore[index]
    claim["evidence_message_ids"] = [  # type: ignore[index]
        "message:user-001",
        "message:assistant-001",
    ]
    with pytest.raises(ValidationError, match="exactly one derived speaker role"):
        CaptureDraft.model_validate(payload)


def test_missing_assertion_evidence_and_assistant_owned_learner_evidence_are_rejected() -> None:
    payload = draft_payload()
    payload["claim_candidates"][0]["evidence_message_ids"] = []  # type: ignore[index]
    with pytest.raises(ValidationError, match="require evidence_message_ids"):
        CaptureDraft.model_validate(payload)
    payload = draft_payload()
    payload["learner_evidence_candidates"] = [
        {
            "concept_terms": ["COPD"],
            "evidence_message_ids": ["message:assistant-001"],
            "evidence_type": "correct_independent",
            "confidence": 1,
            "rationale": "forged learner ownership",
        }
    ]
    with pytest.raises(ValidationError, match="owned by user"):
        CaptureDraft.model_validate(payload)


def test_origin_is_not_identity_and_misconception_error_evidence_is_user_owned() -> None:
    payload = draft_payload()
    payload["context"]["origin"] = "chatgpt_work"  # type: ignore[index]
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CaptureDraft.model_validate(payload)
    payload = draft_payload()
    misconception = payload["misconception_candidates"][0]  # type: ignore[index]
    misconception["observed_error_message_ids"] = ["message:assistant-001"]  # type: ignore[index]
    with pytest.raises(ValidationError, match="observed errors must be owned by user"):
        CaptureDraft.model_validate(payload)


def test_correction_terms_drive_resolution_and_authoritative_claim_matching() -> None:
    payload = draft_payload()
    misconception = payload["misconception_candidates"][0]  # type: ignore[index]
    misconception["concept_terms"] = ["COPD"]  # type: ignore[index]
    value = CaptureDraft.model_validate(payload)
    proposal = build_capture_proposal(ContractBundle.from_directory(Path("examples/copd")), value)
    observation = proposal.learning_capture_candidate.capture.misconception_observations[0]
    assert len(observation.concept_ids) == 1
    assert observation.correction_claim_ids == ("claim_22222222222222222222222222222222",)
    assert {item.surface_text for item in proposal.concept_resolutions} >= {"COPD", "吸烟"}


@pytest.mark.parametrize(
    ("evidence_type", "expected_observation"),
    [
        ("correct_independent", "correct_recall"),
        ("correct_after_hint", "correct_recall"),
        ("partial", "uncertain"),
        ("incorrect", "incorrect_recall"),
        ("high_confidence_incorrect", "incorrect_recall"),
    ],
)
def test_explicit_learning_outcomes_materialize_to_persistent_taxonomy(
    evidence_type: str, expected_observation: str
) -> None:
    payload = draft_payload()
    payload["learner_evidence_candidates"] = [
        {
            "concept_terms": ["COPD"],
            "evidence_message_ids": ["message:user-001"],
            "evidence_type": evidence_type,
            "confidence": 0.8,
            "rationale": "explicit observed outcome",
        }
    ]
    proposal = build_capture_proposal(
        ContractBundle.from_directory(Path("examples/copd")), CaptureDraft.model_validate(payload)
    )
    capture = materialize_learning_capture(
        ContractBundle.from_directory(Path("examples/copd")), proposal
    )
    assert capture.learner_evidence[0].evidence_type == evidence_type
    assert capture.learner_evidence[0].observed_at.isoformat() == "2026-07-11T09:55:00+08:00"
    assert any(
        item.observation_type == expected_observation
        for item in proposal.learning_capture_candidate.observations
    )


def test_correctness_is_not_inferred_from_matching_user_and_assistant_text() -> None:
    payload = draft_payload()
    payload["learner_evidence_candidates"] = []
    proposal = build_capture_proposal(
        ContractBundle.from_directory(Path("examples/copd")), CaptureDraft.model_validate(payload)
    )
    assert proposal.learning_capture_candidate.capture.learner_evidence == ()


def test_user_question_materializes_as_open_question() -> None:
    payload = draft_payload()
    payload["claim_candidates"] = [
        {
            "statement": "COPD 如何诊断？",
            "claim_type": "question",
            "concept_terms": ["COPD"],
            "evidence_message_ids": ["message:user-001"],
            "question_priority": "high",
        }
    ]
    proposal = build_capture_proposal(
        ContractBundle.from_directory(Path("examples/copd")), CaptureDraft.model_validate(payload)
    )
    question = materialize_learning_capture(
        ContractBundle.from_directory(Path("examples/copd")), proposal
    ).open_questions[0]
    assert question.text == "COPD 如何诊断？"
    assert question.priority == "high"
    assert question.concept_ids == ("concept_11111111111111111111111111111111",)


def test_materialization_refuses_blocked_ambiguous_stale_tampered_and_unresolved() -> None:
    copd = ContractBundle.from_directory(Path("examples/copd"))
    ready = build_capture_proposal(copd, draft())
    ambiguous_bundle = ContractBundle.from_directory(Path("examples/capture/ambiguous-ms/bundle"))
    ambiguous = build_capture_proposal(ambiguous_bundle, draft("ambiguous-ms"))
    with pytest.raises(ValueError, match="BLOCKED_PROPOSAL"):
        materialize_learning_capture(ambiguous_bundle, ambiguous)
    missing_source_payload = draft_payload()
    missing_source_payload["context"]["source_id"] = "source_ffffffffffffffffffffffffffffffff"  # type: ignore[index]
    blocked = build_capture_proposal(copd, CaptureDraft.model_validate(missing_source_payload))
    with pytest.raises(ValueError, match="BLOCKED_PROPOSAL"):
        materialize_learning_capture(copd, blocked)
    with pytest.raises(ValueError, match="STALE_BASE_BUNDLE"):
        materialize_learning_capture(ContractBundle.from_directory(Path("examples/gerd")), ready)
    changed_capture = ready.learning_capture_candidate.capture.model_copy(
        update={"session_id": "session:tampered"}
    )
    tampered = ready.model_copy(
        update={
            "learning_capture_candidate": ready.learning_capture_candidate.model_copy(
                update={"capture": changed_capture}
            )
        }
    )
    with pytest.raises(ValueError, match="PROPOSAL_DIGEST_MISMATCH"):
        materialize_learning_capture(copd, tampered)
    unresolved = build_capture_proposal(copd, draft("new-concept"))
    with pytest.raises(ValueError, match="BLOCKED_PROPOSAL"):
        materialize_learning_capture(copd, unresolved)


def test_materialization_rejects_invalid_correction_claim_even_with_recomputed_digest() -> None:
    bundle = ContractBundle.from_directory(Path("examples/copd"))
    proposal = build_capture_proposal(bundle, draft())
    capture = proposal.learning_capture_candidate.capture
    misconception = capture.misconception_observations[0].model_copy(
        update={"correction_claim_ids": ("claim_ffffffffffffffffffffffffffffffff",)}
    )
    changed_capture = capture.model_copy(update={"misconception_observations": (misconception,)})
    changed_candidate = proposal.learning_capture_candidate.model_copy(
        update={"capture": changed_capture}
    )
    changed = proposal.model_copy(update={"learning_capture_candidate": changed_candidate})
    resigned = changed.model_copy(update={"proposal_digest": capture_proposal_digest(changed)})
    with pytest.raises(ValueError, match="INVALID_CORRECTION_CLAIM"):
        materialize_learning_capture(bundle, resigned)


def test_materialization_rejects_invalid_concept_even_with_recomputed_digest() -> None:
    bundle = ContractBundle.from_directory(Path("examples/copd"))
    proposal = build_capture_proposal(bundle, draft())
    capture = proposal.learning_capture_candidate.capture
    evidence = LearnerEvidence(
        evidence_id="evidence_invalid_concept",
        concept_id="concept_ffffffffffffffffffffffffffffffff",
        evidence_type="unknown",
        confidence=0,
        rationale="invalid reference test",
        message_id="message:user-001",
        observed_at=datetime.fromisoformat("2026-07-11T09:55:00+08:00"),
    )
    changed_capture = capture.model_copy(update={"learner_evidence": (evidence,)})
    changed_candidate = proposal.learning_capture_candidate.model_copy(
        update={"capture": changed_capture}
    )
    changed = proposal.model_copy(update={"learning_capture_candidate": changed_candidate})
    resigned = changed.model_copy(update={"proposal_digest": capture_proposal_digest(changed)})
    with pytest.raises(ValueError, match="INVALID_CAPTURE_CONCEPT"):
        materialize_learning_capture(bundle, resigned)


def test_materialization_is_deterministic_and_does_not_mutate_inputs() -> None:
    bundle = ContractBundle.from_directory(Path("examples/copd"))
    proposal = build_capture_proposal(bundle, draft())
    before_bundle, before_proposal = bundle.model_dump(), proposal.model_dump()
    assert materialize_learning_capture(bundle, proposal) == materialize_learning_capture(
        bundle, proposal
    )
    assert bundle.model_dump() == before_bundle
    assert proposal.model_dump() == before_proposal


def test_message_order_affects_digest_but_set_like_concept_order_does_not() -> None:
    original = draft()
    reversed_messages = original.model_copy(
        update={"evidence_messages": tuple(reversed(original.evidence_messages))}
    )
    assert capture_draft_digest(original) != capture_draft_digest(reversed_messages)
    payload = draft_payload()
    payload["claim_candidates"][0]["concept_terms"].reverse()  # type: ignore[index,union-attr]
    reordered = CaptureDraft.model_validate(payload)
    assert capture_draft_digest(original) == capture_draft_digest(reordered)


def test_capture_cli_rejects_tampering_and_stale_bundle(tmp_path: Path) -> None:
    runner = CliRunner()
    proposal_path = tmp_path / "proposal.json"
    review_path = tmp_path / "review.md"
    proposed = runner.invoke(
        app,
        [
            "capture",
            "propose",
            "examples/copd",
            "examples/capture/copd-session/draft.json",
            str(proposal_path),
        ],
    )
    assert proposed.exit_code == 0
    payload = json.loads(proposal_path.read_text(encoding="utf-8"))
    payload["claim_proposals"][0]["statement"] = "tampered"
    proposal_path.write_text(json.dumps(payload), encoding="utf-8")
    reviewed = runner.invoke(
        app, ["capture", "review", "examples/copd", str(proposal_path), str(review_path)]
    )
    assert reviewed.exit_code == 1
    assert "PROPOSAL_DIGEST_MISMATCH" in reviewed.stderr
    assert not review_path.exists()


def test_workflow_schema_is_independent() -> None:
    restored = CaptureDraft.model_validate_json(draft().model_dump_json())
    assert restored.draft_version == "0.3.0"
    assert "schema_version" not in CaptureProposal.model_fields


def test_ambiguous_ms_is_blocked_without_a_concept_reference() -> None:
    bundle = ContractBundle.from_directory(Path("examples/capture/ambiguous-ms/bundle"))
    proposal = build_capture_proposal(bundle, draft("ambiguous-ms"))
    resolution = next(item for item in proposal.concept_resolutions if item.surface_text == "MS")
    assert proposal.status == "blocked"
    assert resolution.status == "ambiguous"
    assert len(resolution.candidate_concept_ids) == 2
    assert proposal.learning_capture_candidate.observations[0].concept_refs == ()


def test_complete_unknown_term_gets_only_a_stable_candidate_id() -> None:
    bundle = ContractBundle.from_directory(Path("examples/copd"))
    value = draft("new-concept")
    proposal = build_capture_proposal(bundle, value)
    candidate = proposal.new_concept_candidates[0]
    assert proposal.status == "blocked"
    assert candidate.candidate_id.startswith("candidate_concept_")
    assert "concept_id" not in type(candidate).model_fields
    assert proposal.claim_proposals[0].concept_refs[0].candidate_id == candidate.candidate_id
    assert any(item.code == "CATALOG_UPDATE_REQUIRED" for item in proposal.issues)
    assert build_capture_proposal(bundle, value) == proposal
