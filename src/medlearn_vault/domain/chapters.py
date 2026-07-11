from typing import Any, Literal

from pydantic import Field

from medlearn_vault.domain.base import DomainModel


class LearningObjective(DomainModel):
    objective_id: str
    text: str
    mastery_level: Literal["know", "understand", "apply", "reason"]
    source_refs: list[str] = Field(default_factory=list)
    coverage_status: Literal["missing", "partial", "covered", "verified"]


class KnowledgeUnit(DomainModel):
    unit_id: str = Field(pattern=r"^ku_[a-f0-9]{12,64}$")
    unit_type: str
    title: str
    concept_ids: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    content: Any
    figure_spec_refs: list[str] = Field(default_factory=list)
    table_spec_refs: list[str] = Field(default_factory=list)


class ExamSummary(DomainModel):
    must_memorize: list[str] = Field(default_factory=list)
    must_understand: list[str] = Field(default_factory=list)
    case_likely: list[str] = Field(default_factory=list)
    mcq_traps: list[str] = Field(default_factory=list)
    awareness_only: list[str] = Field(default_factory=list)


class CrossDisciplineLink(DomainModel):
    concept_id: str = Field(min_length=1)
    discipline_id: str = Field(min_length=1)
    chapter_ref: str = Field(min_length=1)
    relationship: str = Field(min_length=1)


class ChapterDossier(DomainModel):
    schema_version: Literal["1.1.0"] = "1.1.0"
    chapter_id: str
    course_id: str
    discipline_id: str
    title: str
    topic_archetype: Literal[
        "disease", "procedure", "drug", "investigation", "mechanism", "syndrome", "other"
    ]
    primary_concept_ids: list[str] = Field(min_length=1)
    related_concept_ids: list[str] = Field(default_factory=list)
    learning_objectives: list[LearningObjective] = Field(default_factory=list)
    knowledge_units: list[KnowledgeUnit] = Field(default_factory=list)
    exam_summary: ExamSummary
    cross_discipline_links: list[CrossDisciplineLink] = Field(default_factory=list)
    quality_status: Literal[
        "draft", "source_gap", "conflict_review", "content_review", "publishable", "published"
    ] = "draft"
