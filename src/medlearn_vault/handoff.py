"""Strict, deterministic Chat Project Source handoff import."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, model_validator

from medlearn_vault.capture import (
    CaptureContext,
    CaptureDraft,
    EvidenceMessage,
    ExtractedAssessmentAttempt,
    ExtractedClaimCandidate,
    ExtractedConceptMention,
    ExtractedGeneratedExplanation,
    ExtractedLearnerEvidenceCandidate,
    ExtractedMisconceptionCandidate,
    IntakeEnvelope,
    learning_chat_source_id,
)
from medlearn_vault.domain.base import AwareDatetime, DomainModel
from medlearn_vault.domain.concepts import ConceptType

HANDOFF_VERSION: Literal["0.1.0"] = "0.1.0"
HANDOFF_CONVERSION_VERSION = "medlearn.handoff_to_intake.v5"
MAX_HANDOFF_BYTES = 256 * 1024
MAX_HANDOFF_ITEMS = 100
MAX_TEXT = 4000
MAX_EXCERPT = 1000
HandoffText = Annotated[str, StringConstraints(max_length=MAX_TEXT)]
LocalReference = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]


class HandoffSession(DomainModel):
    title: str = Field(min_length=1, max_length=256)
    discipline_id: str = Field(min_length=1, max_length=128)
    course_id: str | None = Field(default=None, min_length=1, max_length=128)
    chapter_id: str | None = Field(default=None, min_length=1, max_length=128)
    session_started_at: AwareDatetime
    captured_at: AwareDatetime

    @model_validator(mode="after")
    def validate_interval(self) -> HandoffSession:
        if self.session_started_at > self.captured_at:
            raise ValueError("session_started_at must not be after captured_at")
        return self


class HandoffEvidenceMessage(DomainModel):
    local_id: LocalReference
    role: Literal["user", "assistant"]
    observed_at: AwareDatetime | None = None
    excerpt: str = Field(min_length=1, max_length=MAX_EXCERPT)
    purpose: str = Field(min_length=1, max_length=128)


class HandoffConcept(DomainModel):
    name: str = Field(min_length=1, max_length=MAX_TEXT)
    preferred_english: str | None = Field(default=None, min_length=1, max_length=MAX_TEXT)
    concept_type: ConceptType
    scope_note: str | None = Field(default=None, min_length=1, max_length=MAX_TEXT)
    evidence_local_ids: tuple[LocalReference, ...] = Field(
        min_length=1, max_length=MAX_HANDOFF_ITEMS
    )


class HandoffClaim(DomainModel):
    statement: str = Field(min_length=1, max_length=MAX_TEXT)
    claim_type: str = Field(min_length=1, max_length=128)
    concept_terms: tuple[HandoffText, ...] = Field(max_length=MAX_HANDOFF_ITEMS)
    evidence_local_ids: tuple[LocalReference, ...] = Field(
        min_length=1, max_length=MAX_HANDOFF_ITEMS
    )
    question_priority: Literal["low", "medium", "high"] | None = None


class HandoffLearnerEvidence(DomainModel):
    concept_terms: tuple[HandoffText, ...] = Field(max_length=MAX_HANDOFF_ITEMS)
    evidence_type: Literal[
        "correct_independent",
        "correct_after_hint",
        "guessed_correct",
        "partial",
        "unknown",
        "incorrect",
        "high_confidence_incorrect",
        "self_report_only",
    ]
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(min_length=1, max_length=MAX_TEXT)
    evidence_local_ids: tuple[LocalReference, ...] = Field(
        min_length=1, max_length=MAX_HANDOFF_ITEMS
    )


class HandoffMisconception(DomainModel):
    observed_error_logic: str = Field(min_length=1, max_length=MAX_TEXT)
    concept_terms: tuple[HandoffText, ...] = Field(max_length=MAX_HANDOFF_ITEMS)
    observed_error_local_ids: tuple[LocalReference, ...] = Field(
        min_length=1, max_length=MAX_HANDOFF_ITEMS
    )
    correction_local_ids: tuple[LocalReference, ...] = Field(max_length=MAX_HANDOFF_ITEMS)
    proposed_correction: str | None = Field(default=None, min_length=1, max_length=MAX_TEXT)
    correction_terms: tuple[HandoffText, ...] = Field(max_length=MAX_HANDOFF_ITEMS)
    severity: Literal["low", "medium", "high"]


class HandoffQuestion(DomainModel):
    statement: str = Field(min_length=1, max_length=MAX_TEXT)
    concept_terms: tuple[HandoffText, ...] = Field(max_length=MAX_HANDOFF_ITEMS)
    evidence_local_ids: tuple[LocalReference, ...] = Field(
        min_length=1, max_length=MAX_HANDOFF_ITEMS
    )
    question_priority: Literal["low", "medium", "high"]


class HandoffAssessmentOption(DomainModel):
    label: str = Field(min_length=1, max_length=64)
    text: HandoffText


class HandoffAssessmentAttempt(DomainModel):
    attempt_id: str = Field(min_length=1, max_length=128)
    question_type: Literal["single_choice", "multiple_choice", "true_false", "short_answer"]
    question_text: HandoffText | None = None
    options: tuple[HandoffAssessmentOption, ...] = Field(max_length=MAX_HANDOFF_ITEMS)
    learner_answer: HandoffText
    assistant_judged_answer: HandoffText | None = None
    verdict: Literal["correct", "partial", "incorrect", "unresolved"]
    assistant_explanation: HandoffText | None = None
    concept_terms: tuple[HandoffText, ...] = Field(max_length=MAX_HANDOFF_ITEMS)
    question_local_ids: tuple[LocalReference, ...] = Field(max_length=MAX_HANDOFF_ITEMS)
    learner_answer_local_ids: tuple[LocalReference, ...] = Field(
        min_length=1, max_length=MAX_HANDOFF_ITEMS
    )
    feedback_local_ids: tuple[LocalReference, ...] = Field(max_length=MAX_HANDOFF_ITEMS)

    @model_validator(mode="after")
    def question_context_is_not_reconstructed(self) -> HandoffAssessmentAttempt:
        if self.question_text is None and self.options:
            raise ValueError("assessment options require question_text")
        return self


class HandoffGeneratedExplanation(DomainModel):
    """GPT-generated prose supplied once by ChatGPT and persisted with the Capture."""

    concept_term: HandoffText
    explanation_text: HandoffText
    origin: Literal["gpt_generated"] = "gpt_generated"
    verification_status: Literal["unverified"] = "unverified"
    generated_at: AwareDatetime
    generator_id: str = Field(min_length=1, max_length=256)
    generation_context_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class HandoffTopic(DomainModel):
    title: str = Field(min_length=1, max_length=MAX_TEXT)
    evidence_local_ids: tuple[LocalReference, ...] = Field(
        min_length=1, max_length=MAX_HANDOFF_ITEMS
    )


class MedLearnHandoff(DomainModel):
    handoff_version: Literal["0.1.0"]
    session: HandoffSession
    learning_goals: tuple[HandoffText, ...] = Field(max_length=MAX_HANDOFF_ITEMS)
    evidence_messages: tuple[HandoffEvidenceMessage, ...] = Field(max_length=MAX_HANDOFF_ITEMS)
    concepts: tuple[HandoffConcept, ...] = Field(max_length=MAX_HANDOFF_ITEMS)
    claims: tuple[HandoffClaim, ...] = Field(max_length=MAX_HANDOFF_ITEMS)
    learner_evidence: tuple[HandoffLearnerEvidence, ...] = Field(max_length=MAX_HANDOFF_ITEMS)
    misconceptions: tuple[HandoffMisconception, ...] = Field(max_length=MAX_HANDOFF_ITEMS)
    unresolved_questions: tuple[HandoffQuestion, ...] = Field(max_length=MAX_HANDOFF_ITEMS)
    unfinished_topics: tuple[HandoffTopic, ...] = Field(max_length=MAX_HANDOFF_ITEMS)
    assessment_attempts: tuple[HandoffAssessmentAttempt, ...] = Field(
        default=(), max_length=MAX_HANDOFF_ITEMS
    )
    generated_explanations: tuple[HandoffGeneratedExplanation, ...] = Field(
        default=(), max_length=MAX_HANDOFF_ITEMS
    )

    @model_validator(mode="after")
    def validate_evidence_references(self) -> MedLearnHandoff:
        ids = [item.local_id for item in self.evidence_messages]
        if len(ids) != len(set(ids)):
            raise ValueError("evidence local_id values must be unique")
        known = set(ids)
        references = (
            *(item.evidence_local_ids for item in self.concepts),
            *(item.evidence_local_ids for item in self.claims),
            *(item.evidence_local_ids for item in self.learner_evidence),
            *(item.observed_error_local_ids for item in self.misconceptions),
            *(item.correction_local_ids for item in self.misconceptions),
            *(item.evidence_local_ids for item in self.unresolved_questions),
            *(item.evidence_local_ids for item in self.unfinished_topics),
            *(item.question_local_ids for item in self.assessment_attempts),
            *(item.learner_answer_local_ids for item in self.assessment_attempts),
            *(item.feedback_local_ids for item in self.assessment_attempts),
        )
        if any(not set(group) <= known for group in references):
            raise ValueError("all evidence_local_ids must exist in evidence_messages")
        roles = {item.local_id: item.role for item in self.evidence_messages}
        assertion_groups = (
            *(item.evidence_local_ids for item in self.claims),
            *(item.evidence_local_ids for item in self.learner_evidence),
            *(item.evidence_local_ids for item in self.unresolved_questions),
            *(item.learner_answer_local_ids for item in self.assessment_attempts),
        )
        if any(len({roles[item] for item in group}) != 1 for group in assertion_groups):
            raise ValueError("assertion evidence must have exactly one derived speaker role")
        if any(
            {roles[item] for item in candidate.evidence_local_ids} != {"user"}
            for candidate in self.learner_evidence
        ):
            raise ValueError("learner evidence must be owned by user evidence messages")
        if any(
            {roles[item] for item in candidate.observed_error_local_ids} != {"user"}
            for candidate in self.misconceptions
        ):
            raise ValueError("observed errors must be owned by user evidence messages")
        if any(
            observed_at < self.session.session_started_at or observed_at > self.session.captured_at
            for item in self.evidence_messages
            for observed_at in (item.observed_at or self.session.captured_at,)
        ):
            raise ValueError("evidence times must fall within the capture interval")
        if any(
            {roles[item] for item in candidate.learner_answer_local_ids} != {"user"}
            for candidate in self.assessment_attempts
        ):
            raise ValueError("assessment learner answers must be owned by user evidence messages")
        if any(
            any(roles[item] != "assistant" for item in candidate.feedback_local_ids)
            for candidate in self.assessment_attempts
        ):
            raise ValueError("assessment feedback must be owned by assistant evidence messages")
        return self


class LearningSegment(DomainModel):
    """Immutable 0.2.0 metadata around one compatible 0.1.0 handoff."""

    segment_version: Literal["0.2.0"] = "0.2.0"
    learning_session_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
    segment_index: int = Field(ge=0)
    previous_segment_digest: str | None = Field(default=None, pattern=r"^sha256:[a-f0-9]{64}$")
    first_evidence_marker: str = Field(min_length=1, max_length=256)
    last_evidence_marker: str = Field(min_length=1, max_length=256)
    segment_message_count: int = Field(ge=1)
    coverage_status: Literal["complete", "partial", "unknown"]
    coverage_note: str | None = Field(default=None, max_length=1000)
    finalized: bool = False
    handoff: MedLearnHandoff

    @model_validator(mode="after")
    def validate_segment(self) -> LearningSegment:
        if self.segment_message_count != len(self.handoff.evidence_messages):
            raise ValueError("segment_message_count must match evidence_messages")
        if (self.segment_index == 0) != (self.previous_segment_digest is None):
            raise ValueError("first segment has no predecessor; later segments require one")
        if self.coverage_status != "complete" and not self.coverage_note:
            raise ValueError("partial or unknown coverage requires coverage_note")
        markers = [item.local_id for item in self.handoff.evidence_messages]
        if self.coverage_status == "complete":
            try:
                first = markers.index(self.first_evidence_marker)
                last = markers.index(self.last_evidence_marker)
            except ValueError as exc:
                raise ValueError(
                    "complete coverage requires visible start and end evidence markers"
                ) from exc
            if first > last:
                raise ValueError("complete coverage markers must be ordered")
        return self


class SegmentPreflightError(ValueError):
    """A sanitized local diagnostic; it intentionally contains no chat excerpts."""

    def __init__(self, collection: str, item: int, roles: set[str], error_code: str) -> None:
        self.diagnostic = {
            "collection": collection,
            "item": item,
            "observed_roles": sorted(roles),
            "error_code": error_code,
        }
        super().__init__(json.dumps(self.diagnostic, ensure_ascii=False, separators=(",", ":")))


def preflight_learning_segment(payload: dict[str, Any]) -> LearningSegment:
    """Repair role-isolatable references before schema validation and submission.

    This is the submission adapter boundary.  It never examines message excerpts:
    role ownership is decided solely from ``local_id -> role``.  Mixed claims and
    unresolved questions become separate role-owned records; fields with an
    intrinsic owner are filtered to that owner.  A field that cannot retain its
    required owner fails closed with a sanitized locator diagnostic.
    """
    value = deepcopy(payload)
    handoff = value.get("handoff")
    if not isinstance(handoff, dict):
        raise SegmentPreflightError("segment", 0, set(), "SEGMENT_HANDOFF_MISSING")
    messages = handoff.get("evidence_messages")
    if not isinstance(messages, list):
        raise SegmentPreflightError("evidence_messages", 0, set(), "EVIDENCE_MESSAGES_MISSING")
    roles = {
        item.get("local_id"): item.get("role")
        for item in messages
        if isinstance(item, dict) and isinstance(item.get("local_id"), str)
    }

    def group_roles(ids: object) -> set[str]:
        if not isinstance(ids, list):
            return set()
        return {roles.get(item) for item in ids if roles.get(item) in {"user", "assistant"}}

    def owned(ids: object, owner: str, collection: str, index: int, *, required: bool) -> list[str]:
        if not isinstance(ids, list):
            raise SegmentPreflightError(collection, index, set(), "ROLE_REFERENCE_INVALID")
        retained = [item for item in ids if roles.get(item) == owner]
        observed = group_roles(ids)
        if required and not retained:
            raise SegmentPreflightError(collection, index, observed, "ROLE_OWNER_REQUIRED")
        return retained

    for collection in ("claims", "unresolved_questions"):
        records = handoff.get(collection, [])
        if not isinstance(records, list):
            raise SegmentPreflightError(collection, 0, set(), "ROLE_COLLECTION_INVALID")
        repaired: list[dict[str, Any]] = []
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                raise SegmentPreflightError(collection, index, set(), "ROLE_ITEM_INVALID")
            ids = record.get("evidence_local_ids")
            observed = group_roles(ids)
            if not observed:
                raise SegmentPreflightError(collection, index, observed, "ROLE_OWNER_REQUIRED")
            # One semantic item with mixed evidence is represented as two
            # provenance-isolated records, never as an invalid mixed record.
            for role in sorted(observed):
                item = deepcopy(record)
                item["evidence_local_ids"] = owned(ids, role, collection, index, required=True)
                repaired.append(item)
        handoff[collection] = repaired

    for index, record in enumerate(handoff.get("learner_evidence", [])):
        if not isinstance(record, dict):
            raise SegmentPreflightError("learner_evidence", index, set(), "ROLE_ITEM_INVALID")
        record["evidence_local_ids"] = owned(
            record.get("evidence_local_ids"), "user", "learner_evidence", index, required=True
        )
    for index, record in enumerate(handoff.get("misconceptions", [])):
        if not isinstance(record, dict):
            raise SegmentPreflightError("misconceptions", index, set(), "ROLE_ITEM_INVALID")
        record["observed_error_local_ids"] = owned(
            record.get("observed_error_local_ids"), "user", "misconceptions", index, required=True
        )
        record["correction_local_ids"] = owned(
            record.get("correction_local_ids"), "assistant", "misconceptions", index, required=False
        )
    for index, record in enumerate(handoff.get("assessment_attempts", [])):
        if not isinstance(record, dict):
            raise SegmentPreflightError("assessment_attempts", index, set(), "ROLE_ITEM_INVALID")
        question_roles = group_roles(record.get("question_local_ids"))
        if len(question_roles) > 1:
            raise SegmentPreflightError(
                "assessment_attempts.question_local_ids", index, question_roles, "QUESTION_ROLE_AMBIGUOUS"
            )
        record["learner_answer_local_ids"] = owned(
            record.get("learner_answer_local_ids"), "user", "assessment_attempts", index, required=True
        )
        record["feedback_local_ids"] = owned(
            record.get("feedback_local_ids"), "assistant", "assessment_attempts", index, required=False
        )

    local_ids = [item.get("local_id") for item in messages if isinstance(item, dict)]
    if local_ids:
        value["first_evidence_marker"] = local_ids[0]
        value["last_evidence_marker"] = local_ids[-1]
        value["segment_message_count"] = len(local_ids)
    segment = LearningSegment.model_validate(value)
    encoded = json.dumps(
        segment.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > MAX_HANDOFF_BYTES:
        raise SegmentPreflightError("segment", 0, set(), "HANDOFF_BYTES_EXCEEDED")
    return segment


def segment_digest(segment: LearningSegment) -> str:
    payload = json.dumps(
        segment.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


class AggregatedLearningSession(DomainModel):
    """Verified session summary. It never claims recovery of unsubmitted chat."""

    protocol_version: Literal["0.2.0"] = "0.2.0"
    learning_session_id: str
    coverage_status: Literal["complete", "partial", "unknown"]
    coverage_note: str | None = None
    finalized: bool
    segment_digests: tuple[str, ...]
    evidence_message_count: int
    learner_evidence_count: int
    misconception_count: int
    unresolved_count: int


def aggregate_segments(segments: tuple[LearningSegment, ...]) -> AggregatedLearningSession:
    """Verify ordering and content-addressed linkage before finalization.

    Replayed segment bytes have the same digest and are deliberately ignored.
    A gap or an explicitly partial input can only produce partial coverage.
    """
    if not segments:
        raise ValueError("SEGMENT_SESSION_EMPTY")
    unique = {segment_digest(item): item for item in segments}
    ordered = tuple(sorted(unique.values(), key=lambda item: item.segment_index))
    if len({item.learning_session_id for item in ordered}) != 1:
        raise ValueError("SEGMENT_SESSION_MISMATCH")
    digests = tuple(segment_digest(item) for item in ordered)
    contiguous = [item.segment_index for item in ordered] == list(range(len(ordered)))
    chain = contiguous and all(
        index == 0 or item.previous_segment_digest == digests[index - 1]
        for index, item in enumerate(ordered)
    )
    if contiguous and not chain:
        raise ValueError("SEGMENT_CHAIN_INVALID")
    partial = not contiguous or any(item.coverage_status != "complete" for item in ordered)
    notes = [item.coverage_note for item in ordered if item.coverage_status != "complete"]
    if not contiguous:
        notes.append("segment gap detected")
    evidence_ids = {
        (message.local_id, message.excerpt)
        for item in ordered
        for message in item.handoff.evidence_messages
    }
    learner_ids = {
        (item.evidence_type, item.concept_terms, item.evidence_local_ids)
        for segment in ordered
        for item in segment.handoff.learner_evidence
    }
    misconception_ids = {
        (item.observed_error_logic, item.observed_error_local_ids)
        for segment in ordered
        for item in segment.handoff.misconceptions
    }
    question_ids = {
        (item.statement, item.evidence_local_ids)
        for segment in ordered
        for item in segment.handoff.unresolved_questions
    }
    return AggregatedLearningSession(
        learning_session_id=ordered[0].learning_session_id,
        coverage_status="partial" if partial else "complete",
        coverage_note="; ".join(note for note in notes if note) or None,
        finalized=ordered[-1].finalized,
        segment_digests=digests,
        evidence_message_count=len(evidence_ids),
        learner_evidence_count=len(learner_ids),
        misconception_count=len(misconception_ids),
        unresolved_count=len(question_ids),
    )


def canonical_handoff_json(handoff: MedLearnHandoff) -> bytes:
    payload = handoff.model_dump(mode="json")
    # Optional 0.1.0 extensions must not change identities of legacy handoffs.
    if not payload["assessment_attempts"]:
        payload.pop("assessment_attempts")
    if not payload["generated_explanations"]:
        payload.pop("generated_explanations")
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def handoff_digest(handoff: MedLearnHandoff) -> str:
    return "sha256:" + hashlib.sha256(canonical_handoff_json(handoff)).hexdigest()


def handoff_idempotency_key(handoff: MedLearnHandoff) -> str:
    """Stable, versioned idempotency key.  Changing HANDOFF_CONVERSION_VERSION
    creates a separate idempotency namespace so old records are never touched."""
    return f"medlearn-handoff-v5-{handoff_digest(handoff)[7:]}"


def _single_concept_term_groups(terms: tuple[HandoffText, ...]) -> tuple[tuple[str, ...], ...]:
    groups = tuple((term,) for term in terms)
    return groups or ((),)


def handoff_to_intake(handoff: MedLearnHandoff) -> IntakeEnvelope:
    """Convert only supplied evidence; this performs no medical inference."""
    if handoff.learning_goals or handoff.unfinished_topics:
        raise ValueError("HANDOFF_CONVERSION_FAILURE")
    digest = handoff_digest(handoff)[7:]
    evidence_ids = {
        item.local_id: (
            f"message_{hashlib.sha256(f'{digest}:{item.local_id}'.encode()).hexdigest()[:32]}"
        )
        for item in handoff.evidence_messages
    }
    evidence = tuple(
        EvidenceMessage(
            message_id=evidence_ids[item.local_id],
            role=item.role,
            observed_at=item.observed_at or handoff.session.captured_at,
            excerpt=item.excerpt,
        )
        for item in handoff.evidence_messages
    )

    def ref(local_ids: tuple[LocalReference, ...]) -> tuple[str, ...]:
        return tuple(evidence_ids[item] for item in local_ids)

    claims = [
        ExtractedClaimCandidate(
            statement=item.statement,
            claim_type=item.claim_type,
            concept_terms=item.concept_terms,
            evidence_message_ids=ref(item.evidence_local_ids),
            question_priority=item.question_priority if item.claim_type == "question" else None,
        )
        for item in handoff.claims
    ]
    claims.extend(
        ExtractedClaimCandidate(
            statement=item.statement,
            claim_type="question",
            concept_terms=item.concept_terms,
            evidence_message_ids=ref(item.evidence_local_ids),
            question_priority=item.question_priority,
        )
        for item in handoff.unresolved_questions
    )
    context = CaptureContext(
        source_id="source_00000000000000000000000000000000",
        session_id=f"session_{digest[:32]}",
        discipline_id=handoff.session.discipline_id,
        course_id=handoff.session.course_id,
        chapter_id=handoff.session.chapter_id,
        session_started_at=handoff.session.session_started_at,
        captured_at=handoff.session.captured_at,
    )
    context = context.model_copy(update={"source_id": learning_chat_source_id(context)})
    draft = CaptureDraft(
        context=context,
        evidence_messages=evidence,
        concept_mentions=tuple(
            ExtractedConceptMention(
                surface_text=item.name,
                evidence_message_ids=ref(item.evidence_local_ids),
                suggested_canonical_name=item.name,
                suggested_preferred_english=item.preferred_english,
                suggested_concept_type=item.concept_type,
                suggested_scope_note=item.scope_note,
            )
            for item in handoff.concepts
        ),
        claim_candidates=tuple(claims),
        learner_evidence_candidates=tuple(
            ExtractedLearnerEvidenceCandidate(
                concept_terms=term_group,
                evidence_type=item.evidence_type,
                confidence=item.confidence,
                rationale=item.rationale,
                evidence_message_ids=ref(item.evidence_local_ids),
            )
            for item in handoff.learner_evidence
            for term_group in _single_concept_term_groups(item.concept_terms)
        ),
        misconception_candidates=tuple(
            ExtractedMisconceptionCandidate(
                observed_error_logic=item.observed_error_logic,
                concept_terms=item.concept_terms,
                observed_error_message_ids=ref(item.observed_error_local_ids),
                correction_message_ids=ref(item.correction_local_ids),
                proposed_correction=item.proposed_correction,
                correction_terms=item.correction_terms,
                severity=item.severity,
            )
            for item in handoff.misconceptions
        ),
        assessment_attempts=tuple(
            ExtractedAssessmentAttempt(
                attempt_id=item.attempt_id,
                question_type=item.question_type,
                question_text=item.question_text,
                options=tuple((option.label, option.text) for option in item.options),
                learner_answer=item.learner_answer,
                assistant_judged_answer=item.assistant_judged_answer,
                verdict=item.verdict,
                assistant_explanation=item.assistant_explanation,
                concept_terms=item.concept_terms,
                question_message_ids=ref(item.question_local_ids),
                learner_answer_message_ids=ref(item.learner_answer_local_ids),
                feedback_message_ids=ref(item.feedback_local_ids),
            )
            for item in handoff.assessment_attempts
        ),
        generated_explanations=tuple(
            ExtractedGeneratedExplanation(
                concept_term=item.concept_term,
                explanation_text=item.explanation_text,
                generated_at=item.generated_at,
                generator_id=item.generator_id,
                generation_context_digest=item.generation_context_digest,
            )
            for item in handoff.generated_explanations
        ),
    )
    return IntakeEnvelope(client_kind="chatgpt_work", draft=draft)


def handoff_submission(handoff: MedLearnHandoff) -> tuple[bytes, str]:
    """Return canonical envelope bytes and its stable HTTP idempotency key."""
    envelope = handoff_to_intake(handoff)
    payload: Any = envelope.model_dump(mode="json")
    if not payload["draft"]["assessment_attempts"]:
        payload["draft"].pop("assessment_attempts")
    if not payload["draft"]["generated_explanations"]:
        payload["draft"].pop("generated_explanations")
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    ), handoff_idempotency_key(handoff)
