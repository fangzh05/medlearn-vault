"""Strict, deterministic Chat Project Source handoff import."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, model_validator

from medlearn_vault.capture import (
    CaptureContext,
    CaptureDraft,
    EvidenceMessage,
    ExtractedClaimCandidate,
    ExtractedConceptMention,
    ExtractedLearnerEvidenceCandidate,
    ExtractedMisconceptionCandidate,
    IntakeEnvelope,
    learning_chat_source_id,
)
from medlearn_vault.domain.base import AwareDatetime, DomainModel
from medlearn_vault.domain.concepts import ConceptType

HANDOFF_VERSION: Literal["0.1.0"] = "0.1.0"
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
        )
        if any(not set(group) <= known for group in references):
            raise ValueError("all evidence_local_ids must exist in evidence_messages")
        roles = {item.local_id: item.role for item in self.evidence_messages}
        assertion_groups = (
            *(item.evidence_local_ids for item in self.claims),
            *(item.evidence_local_ids for item in self.learner_evidence),
            *(item.evidence_local_ids for item in self.unresolved_questions),
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
        return self


def canonical_handoff_json(handoff: MedLearnHandoff) -> bytes:
    return json.dumps(
        handoff.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def handoff_digest(handoff: MedLearnHandoff) -> str:
    return "sha256:" + hashlib.sha256(canonical_handoff_json(handoff)).hexdigest()


def handoff_idempotency_key(handoff: MedLearnHandoff) -> str:
    return "medlearn-handoff-" + handoff_digest(handoff)[7:]


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
                concept_terms=item.concept_terms,
                evidence_type=item.evidence_type,
                confidence=item.confidence,
                rationale=item.rationale,
                evidence_message_ids=ref(item.evidence_local_ids),
            )
            for item in handoff.learner_evidence
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
    )
    return IntakeEnvelope(client_kind="chatgpt_work", draft=draft)


def handoff_submission(handoff: MedLearnHandoff) -> tuple[bytes, str]:
    """Return canonical envelope bytes and its stable HTTP idempotency key."""
    envelope = handoff_to_intake(handoff)
    payload: Any = envelope.model_dump(mode="json")
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    ), handoff_idempotency_key(handoff)
