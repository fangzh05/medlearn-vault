from typing import Any, Literal

from pydantic import Field, model_validator

from medlearn_vault.domain.base import DomainModel
from medlearn_vault.identifiers import content_hash as compute_content_hash
from medlearn_vault.identifiers import knowledge_unit_fingerprint


class LearningObjective(DomainModel):
    objective_id: str
    text: str
    mastery_level: Literal["know", "understand", "apply", "reason"]
    source_refs: tuple[str, ...] = ()
    coverage_status: Literal["missing", "partial", "covered", "verified"]


class KnowledgeUnit(DomainModel):
    unit_id: str = Field(pattern=r"^unit_[a-f0-9]{32}$")
    unit_type: str
    title: str
    concept_ids: tuple[str, ...] = ()
    claim_ids: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()
    content: Any
    figure_spec_refs: tuple[str, ...] = ()
    table_spec_refs: tuple[str, ...] = ()
    match_fingerprint: str = Field(default="", pattern=r"^kufp_[a-f0-9]{16}$")
    content_hash: str = Field(default="", pattern=r"^content_[a-f0-9]{64}$")

    @model_validator(mode="after")
    def refresh_fingerprint(self) -> "KnowledgeUnit":
        object.__setattr__(
            self,
            "match_fingerprint",
            knowledge_unit_fingerprint(self.unit_type, self.title, self.concept_ids),
        )
        object.__setattr__(
            self,
            "content_hash",
            compute_content_hash(
                self.unit_type,
                self.title,
                self.concept_ids,
                self.claim_ids,
                self.source_refs,
                self.content,
                self.figure_spec_refs,
                self.table_spec_refs,
            ),
        )
        return self


class ExamSummary(DomainModel):
    must_memorize: tuple[str, ...] = ()
    must_understand: tuple[str, ...] = ()
    case_likely: tuple[str, ...] = ()
    mcq_traps: tuple[str, ...] = ()
    awareness_only: tuple[str, ...] = ()


class ChapterDossier(DomainModel):
    schema_version: Literal["1.1.1"] = "1.1.1"
    chapter_id: str
    course_id: str
    discipline_id: str
    title: str
    topic_archetype: Literal[
        "disease", "procedure", "drug", "investigation", "mechanism", "syndrome", "other"
    ]
    concept_ids: tuple[str, ...] = Field(min_length=1)
    learning_objectives: tuple[LearningObjective, ...] = ()
    knowledge_units: tuple[KnowledgeUnit, ...] = ()
    exam_summary: ExamSummary
    quality_status: Literal[
        "draft", "source_gap", "conflict_review", "content_review", "publishable", "published"
    ] = "draft"

    @model_validator(mode="after")
    def validate_unit_scope(self) -> "ChapterDossier":
        chapter_concepts = set(self.concept_ids)
        if any(not set(unit.concept_ids) <= chapter_concepts for unit in self.knowledge_units):
            raise ValueError("knowledge unit concept IDs must be within chapter concept IDs")
        return self
