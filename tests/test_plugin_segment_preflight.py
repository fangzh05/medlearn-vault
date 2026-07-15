from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from medlearn_vault.handoff import SegmentPreflightError, preflight_learning_segment


def payload() -> dict[str, object]:
    now = datetime(2026, 7, 15, tzinfo=UTC).isoformat()
    return {
        "segment_version": "0.2.0",
        "learning_session_id": "session_plugin_preflight",
        "segment_index": 0,
        "previous_segment_digest": None,
        "first_evidence_marker": "u1",
        "last_evidence_marker": "a2",
        "segment_message_count": 4,
        "coverage_status": "complete",
        "coverage_note": None,
        "finalized": True,
        "handoff": {
            "handoff_version": "0.1.0",
            "session": {
                "title": "学习对话", "discipline_id": "unclassified", "course_id": None,
                "chapter_id": None, "session_started_at": now, "captured_at": now,
            },
            "learning_goals": [],
            "evidence_messages": [
                {"local_id": "u1", "role": "user", "excerpt": "B", "purpose": "answer"},
                {"local_id": "a1", "role": "assistant", "excerpt": "题干 A B", "purpose": "question"},
                {"local_id": "u2", "role": "user", "excerpt": "错误答案", "purpose": "answer"},
                {"local_id": "a2", "role": "assistant", "excerpt": "纠正", "purpose": "feedback"},
            ],
            "concepts": [],
            "claims": [{"statement": "教学结论", "claim_type": "teaching", "concept_terms": [], "evidence_local_ids": ["u1", "a2"]}],
            "learner_evidence": [{"concept_terms": [], "evidence_type": "correct_independent", "confidence": 1, "rationale": "作答", "evidence_local_ids": ["u1", "a2"]}],
            "misconceptions": [{"observed_error_logic": "错误", "concept_terms": [], "observed_error_local_ids": ["u2", "a2"], "correction_local_ids": ["u2", "a2"], "proposed_correction": None, "correction_terms": [], "severity": "low"}],
            "unresolved_questions": [], "unfinished_topics": [],
            "assessment_attempts": [{"attempt_id": "attempt_1", "question_type": "single_choice", "question_text": "题干", "options": [{"label": "A", "text": "甲"}, {"label": "B", "text": "乙"}], "learner_answer": "B", "assistant_judged_answer": "B", "verdict": "correct", "assistant_explanation": "正确", "concept_terms": [], "question_local_ids": ["a1"], "learner_answer_local_ids": ["u1", "a2"], "feedback_local_ids": ["u1", "a2"]}],
            "generated_explanations": [],
        },
    }


def test_mixed_role_records_are_deterministically_isolated_before_submission() -> None:
    segment = preflight_learning_segment(payload())
    handoff = segment.handoff
    assert segment.first_evidence_marker == "u1" and segment.last_evidence_marker == "a2"
    assert segment.segment_message_count == 4
    assert len(handoff.claims) == 2
    assert {item.evidence_local_ids for item in handoff.claims} == {("u1",), ("a2",)}
    assert handoff.learner_evidence[0].evidence_local_ids == ("u1",)
    misconception = handoff.misconceptions[0]
    assert misconception.observed_error_local_ids == ("u2",)
    assert misconception.correction_local_ids == ("a2",)
    attempt = handoff.assessment_attempts[0]
    assert attempt.learner_answer_local_ids == ("u1",)
    assert attempt.feedback_local_ids == ("a2",)


def test_ambiguous_question_provenance_fails_closed_without_submission() -> None:
    value = payload()
    attempt = value["handoff"]["assessment_attempts"][0]  # type: ignore[index]
    attempt["question_local_ids"] = ["u1", "a1"]
    with pytest.raises(SegmentPreflightError) as error:
        preflight_learning_segment(value)
    assert error.value.diagnostic == {
        "collection": "assessment_attempts.question_local_ids",
        "item": 0,
        "observed_roles": ["assistant", "user"],
        "error_code": "QUESTION_ROLE_AMBIGUOUS",
    }


def test_one_line_command_requires_no_schema_guidance() -> None:
    manifest = json.loads(
        (Path(__file__).resolve().parents[1] / "plugins" / "medlearn" / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    prompt = " ".join(manifest["interface"]["defaultPrompt"])
    assert "@MedLearn 存档当前学习对话" in prompt
    assert "Never require the user to provide JSON" in prompt

