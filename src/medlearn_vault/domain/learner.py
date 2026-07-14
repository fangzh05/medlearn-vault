from typing import Literal

from pydantic import Field, model_validator

from medlearn_vault.domain.base import AwareDatetime, DomainModel, EventModel
from medlearn_vault.domain.ids import ClaimId, ConceptId, ScopedExternalId, SourceId, UnitId


class ConceptMention(EventModel):
    surface_text: str
    candidate_concept_ids: tuple[ConceptId, ...] = ()
    resolved_concept_id: ConceptId | None = None
    resolution_status: Literal["resolved", "ambiguous", "new_candidate", "rejected"]
    confidence: float = Field(ge=0, le=1)
    message_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_resolution(self) -> "ConceptMention":
        if self.resolution_status == "resolved":
            if self.resolved_concept_id is None:
                raise ValueError("resolved mentions require resolved_concept_id")
            if self.candidate_concept_ids != (self.resolved_concept_id,):
                raise ValueError("resolved mentions require exactly one matching candidate")
            if self.confidence <= 0:
                raise ValueError("resolved mentions require positive confidence")
        elif self.resolved_concept_id is not None:
            raise ValueError("only resolved mentions may define resolved_concept_id")
        if self.resolution_status == "ambiguous" and len(self.candidate_concept_ids) < 2:
            raise ValueError("ambiguous mentions require at least two candidates")
        if self.resolution_status in {"new_candidate", "rejected"} and self.candidate_concept_ids:
            raise ValueError("new or rejected mentions cannot retain existing candidates")
        return self


class LearnerEvidence(EventModel):
    evidence_id: str
    concept_id: ConceptId
    knowledge_unit_id: UnitId | None = None
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
    rationale: str
    message_id: str
    user_excerpt: str | None = None
    observed_at: AwareDatetime


class MisconceptionObservation(EventModel):
    observation_id: str
    concept_ids: tuple[ConceptId, ...]
    discipline_ids: tuple[str, ...] = ()
    observed_error_logic: str
    proposed_correction: str | None = None
    correction_claim_ids: tuple[ClaimId, ...] = ()
    severity: Literal["low", "medium", "high"]
    evidence_message_ids: tuple[str, ...]
    user_excerpt: str | None = None
    observed_at: AwareDatetime


class MisconceptionState(DomainModel):
    misconception_id: str
    concept_ids: tuple[ConceptId, ...]
    first_seen_at: AwareDatetime
    last_seen_at: AwareDatetime
    current_status: Literal["active", "improving", "resolved", "relapsed"]
    resolution_evidence_ids: tuple[str, ...] = ()
    relapse_count: int = Field(default=0, ge=0)


class LearnerState(DomainModel):
    schema_version: Literal["1.2.0"] = "1.2.0"
    learner_id: str
    computed_at: AwareDatetime
    misconception_states: tuple[MisconceptionState, ...] = ()


class OpenQuestion(EventModel):
    question_id: str
    text: str
    concept_ids: tuple[ConceptId, ...]
    discipline_id: str | None = None
    priority: Literal["low", "medium", "high"]


class LearningCapture(EventModel):
    schema_version: Literal["1.2.0"] = "1.2.0"
    session_id: ScopedExternalId
    source_id: SourceId
    session_started_at: AwareDatetime
    captured_at: AwareDatetime
    discipline_id: ScopedExternalId
    course_id: ScopedExternalId | None = None
    chapter_id: ScopedExternalId | None = None
    concept_mentions: tuple[ConceptMention, ...] = ()
    learner_evidence: tuple[LearnerEvidence, ...] = ()
    misconception_observations: tuple[MisconceptionObservation, ...] = ()
    open_questions: tuple[OpenQuestion, ...] = ()

    @model_validator(mode="after")
    def validate_timeline(self) -> "LearningCapture":
        if self.session_started_at > self.captured_at:
            raise ValueError("session_started_at must not be after captured_at")
        observed = [item.observed_at for item in self.learner_evidence]
        observed.extend(item.observed_at for item in self.misconception_observations)
        if any(item < self.session_started_at or item > self.captured_at for item in observed):
            raise ValueError("nested observation times must fall within the capture interval")
        return self
