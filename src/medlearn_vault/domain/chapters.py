from typing import Any, Literal

from pydantic import Field, model_validator

from medlearn_vault.domain.base import DomainModel
from medlearn_vault.identifiers import knowledge_unit_fingerprint


class LearningObjective(DomainModel):
    objective_id: str
    text: str
    mastery_level: Literal["know", "understand", "apply", "reason"]
    source_refs: list[str] = Field(default_factory=list)
    coverage_status: Literal["missing", "partial", "covered", "verified"]


class KnowledgeUnit(DomainModel):
    unit_id: str = Field(pattern=r"^unit_[a-f0-9]{32}$")
    unit_type: str
    title: str
    concept_ids: tuple[str, ...] = ()
    claim_ids: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    content: Any
    figure_spec_refs: list[str] = Field(default_factory=list)
    table_spec_refs: list[str] = Field(default_factory=list)
    fingerprint: str = Field(default="", pattern=r"^kufp_[a-f0-9]{16}$")

    @model_validator(mode="after")
    def refresh_fingerprint(self) -> "KnowledgeUnit":
        object.__setattr__(
            self,
            "fingerprint",
            knowledge_unit_fingerprint(self.unit_type, self.title, self.concept_ids),
        )
        return self


class ExamSummary(DomainModel):
    must_memorize: list[str] = Field(default_factory=list)
    must_understand: list[str] = Field(default_factory=list)
    case_likely: list[str] = Field(default_factory=list)
    mcq_traps: list[str] = Field(default_factory=list)
    awareness_only: list[str] = Field(default_factory=list)


class ChapterDossier(DomainModel):
    schema_version: Literal["1.1.0"] = "1.1.0"
    chapter_id: str
    course_id: str
    discipline_id: str
    title: str
    topic_archetype: Literal[
        "disease", "procedure", "drug", "investigation", "mechanism", "syndrome", "other"
    ]
    concept_ids: list[str] = Field(min_length=1)
    learning_objectives: list[LearningObjective] = Field(default_factory=list)
    knowledge_units: list[KnowledgeUnit] = Field(default_factory=list)
    exam_summary: ExamSummary
    quality_status: Literal[
        "draft", "source_gap", "conflict_review", "content_review", "publishable", "published"
    ] = "draft"
