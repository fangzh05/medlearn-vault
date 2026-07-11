from typing import Literal

from pydantic import Field, model_validator

from medlearn_vault.domain.base import AwareDatetime, DomainModel


class ConceptMention(DomainModel):
    surface_text: str
    candidate_concept_ids: list[str] = Field(default_factory=list)
    resolved_concept_id: str | None = None
    resolution_status: Literal["resolved", "ambiguous", "new_candidate", "rejected"]
    confidence: float = Field(ge=0, le=1)
    message_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_resolution(self) -> "ConceptMention":
        if self.resolution_status == "resolved":
            if self.resolved_concept_id is None:
                raise ValueError("resolved mentions require resolved_concept_id")
            if self.candidate_concept_ids != [self.resolved_concept_id]:
                raise ValueError("resolved mentions require exactly one matching candidate")
        elif self.resolved_concept_id is not None:
            raise ValueError("only resolved mentions may define resolved_concept_id")
        return self


class LearnerEvidence(DomainModel):
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


class Misconception(DomainModel):
    misconception_id: str
    concept_ids: list[str]
    discipline_ids: list[str] = Field(default_factory=list)
    error_logic: str
    correct_logic: str
    severity: Literal["low", "medium", "high"]
    status: Literal["active", "improving", "resolved", "relapsed"]
    source_message_ids: list[str]


class OpenQuestion(DomainModel):
    question_id: str
    text: str
    concept_ids: list[str]
    discipline_id: str | None = None
    priority: Literal["low", "medium", "high"]


class LearningCapture(DomainModel):
    schema_version: Literal["1.1.0"] = "1.1.0"
    session_id: str
    source_id: str
    captured_at: AwareDatetime
    discipline_id: str
    course_id: str | None = None
    chapter_id: str | None = None
    concept_mentions: list[ConceptMention] = Field(default_factory=list)
    learner_evidence: list[LearnerEvidence] = Field(default_factory=list)
    misconceptions: list[Misconception] = Field(default_factory=list)
    open_questions: list[OpenQuestion] = Field(default_factory=list)
