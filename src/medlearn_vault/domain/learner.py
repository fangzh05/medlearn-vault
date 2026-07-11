from typing import Literal

from pydantic import Field, model_validator

from medlearn_vault.domain.base import AwareDatetime, DomainModel, EventModel


class ConceptMention(EventModel):
    surface_text: str
    candidate_concept_ids: tuple[str, ...] = ()
    resolved_concept_id: str | None = None
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
        elif self.resolved_concept_id is not None:
            raise ValueError("only resolved mentions may define resolved_concept_id")
        return self


class LearnerEvidence(EventModel):
    evidence_id: str
    concept_id: str
    knowledge_unit_id: str | None = None
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
    observed_at: AwareDatetime


class MisconceptionObservation(EventModel):
    observation_id: str
    concept_ids: tuple[str, ...]
    discipline_ids: tuple[str, ...] = ()
    error_logic: str
    correct_logic: str
    severity: Literal["low", "medium", "high"]
    evidence_message_ids: tuple[str, ...]
    observed_at: AwareDatetime


class MisconceptionState(DomainModel):
    misconception_id: str
    concept_ids: list[str]
    first_seen_at: AwareDatetime
    last_seen_at: AwareDatetime
    current_status: Literal["active", "improving", "resolved", "relapsed"]
    resolution_evidence_ids: list[str] = Field(default_factory=list)
    relapse_count: int = Field(default=0, ge=0)


class LearnerState(DomainModel):
    schema_version: Literal["1.1.0"] = "1.1.0"
    learner_id: str
    computed_at: AwareDatetime
    misconception_states: list[MisconceptionState] = Field(default_factory=list)


class OpenQuestion(EventModel):
    question_id: str
    text: str
    concept_ids: tuple[str, ...]
    discipline_id: str | None = None
    priority: Literal["low", "medium", "high"]


class LearningCapture(EventModel):
    schema_version: Literal["1.1.0"] = "1.1.0"
    session_id: str
    source_id: str
    captured_at: AwareDatetime
    discipline_id: str
    course_id: str | None = None
    chapter_id: str | None = None
    concept_mentions: tuple[ConceptMention, ...] = ()
    learner_evidence: tuple[LearnerEvidence, ...] = ()
    misconception_observations: tuple[MisconceptionObservation, ...] = ()
    open_questions: tuple[OpenQuestion, ...] = ()
