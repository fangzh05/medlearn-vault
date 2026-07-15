from datetime import UTC, datetime, timedelta

import pytest

from medlearn_vault.bundle import ContractBundle
from medlearn_vault.capture import CaptureProposal, concept_candidate_blocker
from medlearn_vault.handoff import (
    HandoffEvidenceMessage,
    HandoffSession,
    LearningSegment,
    MedLearnHandoff,
    aggregate_segments,
    segment_digest,
)
from medlearn_vault.publication import render_learning_capture_markdown


def _segment(
    index: int,
    previous: str | None,
    *,
    finalized: bool = False,
    message_count: int = 50,
    complete: bool = True,
) -> LearningSegment:
    start = datetime(2026, 7, 15, tzinfo=UTC) + timedelta(minutes=index)
    messages = tuple(
        HandoffEvidenceMessage(
            local_id=f"m{number}",
            role="user",
            observed_at=start,
            excerpt=f"用户回答 {index}-{number}",
            purpose="selected_learning_evidence",
        )
        for number in range(message_count)
    )
    handoff = MedLearnHandoff(
        handoff_version="0.1.0",
        session=HandoffSession(
            title="long", discipline_id="internal", session_started_at=start, captured_at=start
        ),
        learning_goals=(),
        evidence_messages=messages,
        concepts=(),
        claims=(),
        learner_evidence=(),
        misconceptions=(),
        unresolved_questions=(),
        unfinished_topics=(),
    )
    return LearningSegment(
        learning_session_id="session_long_001",
        segment_index=index,
        previous_segment_digest=previous,
        first_evidence_marker="m0" if complete else "missing-start",
        last_evidence_marker=f"m{message_count - 1}",
        segment_message_count=message_count,
        coverage_status="complete" if complete else "partial",
        coverage_note=None if complete else "requested start marker is not visible",
        finalized=finalized,
        handoff=handoff,
    )


def test_150_messages_are_received_as_three_verified_segments_and_retry_is_idempotent() -> None:
    first = _segment(0, None)
    second = _segment(1, segment_digest(first))
    third = _segment(2, segment_digest(second), finalized=True)
    result = aggregate_segments((first, second, third, second))
    assert result.finalized and result.coverage_status == "complete"
    assert len(result.segment_digests) == 3
    assert result.evidence_message_count == 150


def test_segment_accepts_49_50_51_56_and_100_messages() -> None:
    for count in (49, 50, 51, 56, 100):
        segment = _segment(0, None, message_count=count)
        assert segment.segment_message_count == count
        assert len(segment.handoff.evidence_messages) == count


def test_complete_coverage_is_semantic_and_partial_when_start_marker_is_absent() -> None:
    assert _segment(0, None, message_count=56).coverage_status == "complete"
    partial = _segment(0, None, message_count=56, complete=False)
    assert partial.coverage_status == "partial"


def test_complete_coverage_rejects_invisible_start_marker() -> None:
    segment = _segment(0, None, message_count=51).model_copy(
        update={"first_evidence_marker": "not-visible"}
    )
    with pytest.raises(ValueError, match="visible start and end"):
        LearningSegment.model_validate(segment.model_dump())


def test_missing_middle_segment_is_explicitly_partial() -> None:
    first = _segment(0, None)
    missing = _segment(2, "sha256:" + "a" * 64, finalized=True)
    result = aggregate_segments((first, missing))
    assert result.coverage_status == "partial"
    assert "gap" in (result.coverage_note or "")


def test_concept_quality_gate_rejects_topics_and_numeric_results() -> None:
    assert concept_candidate_blocker("8分") == "NUMERIC_RESULT_NOT_CONCEPT"
    assert concept_candidate_blocker("逐项计算") == "CONTEXT_DEPENDENT_CONCEPT"
    assert concept_candidate_blocker("DIC治疗") == "LEARNING_TOPIC_NOT_CONCEPT"
    assert concept_candidate_blocker("甲氨蝶呤与Felty综合征") == "COMPOSITE_CONCEPT_CANDIDATE"


def test_renderer_v2_has_chinese_labels_and_no_internal_ids() -> None:
    bundle = ContractBundle.from_directory(__import__("pathlib").Path("examples/copd"))
    capture = CaptureProposal.model_validate_json(
        __import__("pathlib")
        .Path("examples/capture/copd-session/expected_proposal.json")
        .read_bytes()
    ).learning_capture_candidate.capture
    markdown = render_learning_capture_markdown(
        bundle,
        capture,
        capture_id="capture_test",
        approval_id="approval_test",
        proposal_id="proposal_test",
    )
    body = markdown.split("---\n", 2)[2]
    assert "## 明确错误" in body and "部分掌握" in body and "错误逻辑" in body
    assert "concept_" not in body and "claim_" not in body and "proposal_" not in body
