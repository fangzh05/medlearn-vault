from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from medlearn_vault.bundle import ContractBundle
from medlearn_vault.capture import (
    CaptureProposal,
    backfill_learning_capture,
    capture_proposal_digest,
)
from medlearn_vault.domain.learner import (
    AssessmentAttempt,
    AssessmentOption,
    ConceptMention,
    ConversationExplanation,
    GeneratedExplanation,
    LearnerEvidence,
    LearningCapture,
)
from medlearn_vault.handoff import MedLearnHandoff, handoff_to_intake
from medlearn_vault.publication import (
    canonical_learning_capture_json,
    render_learning_capture_markdown,
)

ROOT = Path(__file__).resolve().parents[1]
COPD = "concept_11111111111111111111111111111111"
ENGLISH_SCOPE = "concept_09a8e0de3eb33b3b8720f0b1a993bc3d"
UNKNOWN = "concept_ffffffffffffffffffffffffffffffff"
DIGEST = "sha256:" + "a" * 64


def base() -> tuple[ContractBundle, LearningCapture]:
    bundle = ContractBundle.from_directory(ROOT / "examples" / "copd")
    capture = CaptureProposal.model_validate_json(
        (ROOT / "examples" / "capture" / "copd-session" / "expected_proposal.json").read_bytes()
    ).learning_capture_candidate.capture
    return bundle, capture


def generated(
    concept_id: str = ENGLISH_SCOPE, text: str = "GPT 解释文本。"
) -> GeneratedExplanation:
    return GeneratedExplanation(
        concept_id=concept_id,
        explanation_text=text,
        generated_at=datetime.fromisoformat("2026-07-11T09:56:00+08:00"),
        generator_id="gpt-5/content-quality-v1",
        learning_session_id="session_test",
        generation_context_digest=DIGEST,
    )


def chat(concept_id: str = ENGLISH_SCOPE) -> ConversationExplanation:
    return ConversationExplanation(
        concept_id=concept_id,
        explanation_text="助手在对话中明确给出的解释。",
        evidence_message_ids=("message_assistant",),
    )


def render(capture: LearningCapture, bundle: ContractBundle) -> str:
    return render_learning_capture_markdown(
        bundle,
        capture,
        capture_id="capture_test",
        approval_id="approval_test",
        proposal_id="proposal_test",
    )


def with_concept(capture: LearningCapture, concept_id: str) -> LearningCapture:
    mention = ConceptMention(
        surface_text="测试概念",
        candidate_concept_ids=(concept_id,),
        resolved_concept_id=concept_id,
        resolution_status="resolved",
        confidence=1,
        message_ids=("message_user",),
    )
    return capture.model_copy(update={"concept_mentions": (mention,)})


def test_verified_definition_overrides_chat_and_gpt() -> None:
    bundle, capture = base()
    capture = capture.model_copy(
        update={
            "conversation_explanations": (chat(COPD),),
            "generated_explanations": (generated(COPD),),
        }
    )
    markdown = render(capture, bundle)
    assert "COPD 以持续气流受限为特征。" in markdown
    assert "助手在对话中明确给出的解释。" not in markdown
    assert "GPT 解释文本。" not in markdown


def test_reviewed_chinese_scope_note_overrides_gpt() -> None:
    bundle, capture = base()
    capture = capture.model_copy(update={"generated_explanations": (generated(COPD),)})
    markdown = render(capture, bundle)
    assert "以持续气流受限为特征的慢性呼吸系统疾病。" not in markdown  # verified claim wins
    smoking = "concept_33333333333333333333333333333333"
    capture = with_concept(capture, smoking).model_copy(
        update={"generated_explanations": (generated(smoking),)}
    )
    markdown = render(capture, bundle)
    assert "烟草暴露行为及其疾病风险语境。" in markdown
    assert "GPT 解释文本。" not in markdown


def test_conversation_explanation_overrides_gpt_and_is_visibly_unverified() -> None:
    bundle, capture = base()
    capture = with_concept(capture, ENGLISH_SCOPE).model_copy(
        update={
            "conversation_explanations": (chat(),),
            "generated_explanations": (generated(),),
        }
    )
    markdown = render(capture, bundle)
    assert "#### 对话内解释" in markdown
    assert "助手在对话中明确给出的解释。" in markdown
    assert "来自本次学习对话，未经外部核验。" in markdown
    assert "GPT 解释文本。" not in markdown


def test_gpt_explanation_is_persisted_labeled_and_not_evidence() -> None:
    bundle, capture = base()
    item = generated(text="这是概念的定义、核心机制及考试意义。")
    capture = with_concept(capture, ENGLISH_SCOPE).model_copy(
        update={"generated_explanations": (item,)}
    )
    first = render(capture, bundle)
    second = render(LearningCapture.model_validate_json(capture.model_dump_json()), bundle)
    assert first == second
    assert "#### GPT 生成解释" in first
    assert "〔GPT 生成，未核验〕" in first
    assert "由 GPT 根据概念名称和当前学习上下文生成，未经教材或指南核验。" in first
    assert all(evidence.rationale != item.explanation_text for evidence in capture.learner_evidence)
    assert all(item.explanation_text not in value for value in chat_message_ids(capture))


def chat_message_ids(capture: LearningCapture) -> tuple[str, ...]:
    return tuple(
        message_id
        for explanation in capture.conversation_explanations
        for message_id in explanation.evidence_message_ids
    )


def test_generated_explanation_rejects_fabricated_citation_markers() -> None:
    with pytest.raises(ValidationError, match="citation markers"):
        generated(text="详见 https://example.test/fake [1]")


def test_assessment_attempts_render_full_context_once_for_multiple_concepts() -> None:
    bundle, capture = base()
    attempt = AssessmentAttempt(
        attempt_id="attempt_1",
        question_type="multiple_choice",
        question_text="关于 COPD，以下哪些说法正确？",
        options=(
            AssessmentOption(label="A", text="选项甲"),
            AssessmentOption(label="B", text="选项乙"),
        ),
        learner_answer="A、B",
        assistant_judged_answer="B",
        verdict="partial",
        assistant_explanation="当时的助手解析。",
        concept_ids=(COPD, "concept_33333333333333333333333333333333"),
        question_message_ids=("message_question",),
        learner_answer_message_ids=("message_answer",),
        feedback_message_ids=("message_feedback",),
        observed_at=datetime.fromisoformat("2026-07-11T09:56:00+08:00"),
    )
    capture = capture.model_copy(update={"assessment_attempts": (attempt,)})
    markdown = render(capture, bundle)
    assert markdown.count("关于 COPD，以下哪些说法正确？") == 1
    assert "A. 选项甲" in markdown and "B. 选项乙" in markdown
    assert "我的回答：A、B" in markdown
    assert "当时判定：部分正确（助手当时判定：B）" in markdown
    assert "正确答案" not in markdown


def test_handoff_preserves_single_choice_question_options_and_roles() -> None:
    payload = {
        "handoff_version": "0.1.0",
        "session": {
            "title": "assessment",
            "discipline_id": "medicine",
            "session_started_at": "2026-07-11T09:00:00+08:00",
            "captured_at": "2026-07-11T09:10:00+08:00",
        },
        "learning_goals": [],
        "evidence_messages": [
            {"local_id": "q", "role": "assistant", "excerpt": "题目和选项", "purpose": "question"},
            {"local_id": "a", "role": "user", "excerpt": "B", "purpose": "answer"},
            {"local_id": "f", "role": "assistant", "excerpt": "判定和解析", "purpose": "feedback"},
        ],
        "concepts": [],
        "claims": [],
        "learner_evidence": [],
        "misconceptions": [],
        "unresolved_questions": [],
        "unfinished_topics": [],
        "assessment_attempts": [
            {
                "attempt_id": "attempt_single",
                "question_type": "single_choice",
                "question_text": "下列哪项正确？",
                "options": [{"label": "A", "text": "甲"}, {"label": "B", "text": "乙"}],
                "learner_answer": "B",
                "assistant_judged_answer": "B",
                "verdict": "correct",
                "assistant_explanation": "当时解析。",
                "concept_terms": [],
                "question_local_ids": ["q"],
                "learner_answer_local_ids": ["a"],
                "feedback_local_ids": ["f"],
            }
        ],
    }
    draft = handoff_to_intake(MedLearnHandoff.model_validate(payload)).draft
    attempt = draft.assessment_attempts[0]
    assert attempt.question_text == "下列哪项正确？"
    assert attempt.options == (("A", "甲"), ("B", "乙"))
    assert attempt.learner_answer == "B"


def test_answer_without_question_is_not_reconstructed() -> None:
    _, capture = base()
    attempt = AssessmentAttempt(
        attempt_id="attempt_missing_question",
        question_type="single_choice",
        question_text=None,
        options=(),
        learner_answer="B",
        verdict="unresolved",
        learner_answer_message_ids=("message_answer",),
        observed_at=datetime.fromisoformat("2026-07-11T09:56:00+08:00"),
    )
    assert attempt.question_text is None and attempt.options == ()


def test_legacy_learner_evidence_concept_is_visible_and_unknown_is_safe() -> None:
    bundle, capture = base()
    smoking = "concept_33333333333333333333333333333333"
    evidence = LearnerEvidence(
        evidence_id="evidence_legacy",
        concept_id=smoking,
        evidence_type="incorrect",
        confidence=0.8,
        rationale="legacy evidence",
        message_id="message_legacy",
        observed_at=datetime.fromisoformat("2026-07-11T09:56:00+08:00"),
    )
    legacy = capture.model_copy(update={"concept_mentions": (), "learner_evidence": (evidence,)})
    assert "[[吸烟]]" in render(legacy, bundle)
    unknown = evidence.model_copy(update={"concept_id": UNKNOWN})
    assert "未收录概念" in render(
        legacy.model_copy(update={"learner_evidence": (unknown,)}), bundle
    )


def test_old_120_capture_without_optional_fields_remains_readable() -> None:
    _, capture = base()
    payload = capture.model_dump(mode="json")
    for field in ("assessment_attempts", "generated_explanations", "conversation_explanations"):
        payload.pop(field, None)
    restored = LearningCapture.model_validate(json.loads(json.dumps(payload)))
    assert restored.schema_version == "1.2.0"
    assert restored.assessment_attempts == restored.generated_explanations == ()


def test_backfill_creates_new_capture_without_mutating_old_capture() -> None:
    bundle, _ = base()
    proposal = CaptureProposal.model_validate_json(
        (ROOT / "examples" / "capture" / "copd-session" / "expected_proposal.json").read_bytes()
    )
    old_capture = proposal.learning_capture_candidate.capture.model_copy(
        update={"conversation_explanations": ()}
    )
    candidate = proposal.learning_capture_candidate.model_copy(update={"capture": old_capture})
    claim = proposal.claim_proposals[0].model_copy(
        update={
            "claim_type": "assistant_explanation",
            "concept_refs": proposal.claim_proposals[0].concept_refs[:1],
        }
    )
    proposal = proposal.model_copy(
        update={"learning_capture_candidate": candidate, "claim_proposals": (claim,)}
    )
    proposal = proposal.model_copy(update={"proposal_digest": capture_proposal_digest(proposal)})
    old_bytes = canonical_learning_capture_json(old_capture)
    backfilled = backfill_learning_capture(bundle, proposal)
    assert canonical_learning_capture_json(old_capture) == old_bytes
    assert backfilled != old_capture
    assert backfilled.conversation_explanations
    assert backfilled.assessment_attempts == old_capture.assessment_attempts
