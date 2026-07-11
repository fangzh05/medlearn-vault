from typing import Literal

from pydantic import Field, model_validator

from medlearn_vault.domain.base import DomainModel
from medlearn_vault.domain.ids import ClaimId, ConceptId, ScopedExternalId, SourceId, UnitId
from medlearn_vault.identifiers import knowledge_unit_fingerprint


class LearningObjective(DomainModel):
    objective_id: str
    text: str
    mastery_level: Literal["know", "understand", "apply", "reason"]
    source_refs: tuple[SourceId, ...] = ()
    coverage_status: Literal["missing", "partial", "covered", "verified"]


class KnowledgeUnit(DomainModel):
    unit_id: UnitId
    unit_type: str
    title: str
    concept_ids: tuple[ConceptId, ...] = ()
    claim_ids: tuple[ClaimId, ...] = ()
    source_refs: tuple[SourceId, ...] = ()
    content: str
    figure_spec_refs: tuple[str, ...] = ()
    table_spec_refs: tuple[str, ...] = ()
    match_fingerprint: str = Field(default="", pattern=r"^kufp_[a-f0-9]{16}$")

    @model_validator(mode="after")
    def refresh_fingerprint(self) -> "KnowledgeUnit":
        object.__setattr__(
            self,
            "match_fingerprint",
            knowledge_unit_fingerprint(self.unit_type, self.title, self.concept_ids),
        )
        return self


class ExamSummary(DomainModel):
    must_memorize: tuple[str, ...] = ()
    must_understand: tuple[str, ...] = ()
    case_likely: tuple[str, ...] = ()
    mcq_traps: tuple[str, ...] = ()
    awareness_only: tuple[str, ...] = ()


class ChapterDossier(DomainModel):
    schema_version: Literal["1.2.0"] = "1.2.0"
    chapter_id: ScopedExternalId
    course_id: ScopedExternalId
    discipline_id: ScopedExternalId
    title: str
    topic_archetype: Literal[
        "disease", "procedure", "drug", "investigation", "mechanism", "syndrome", "other"
    ]
    concept_ids: tuple[ConceptId, ...] = Field(min_length=1)
    anchor_concept_ids: tuple[ConceptId, ...] = Field(min_length=1)
    learning_objectives: tuple[LearningObjective, ...] = ()
    knowledge_units: tuple[KnowledgeUnit, ...] = ()
    exam_summary: ExamSummary
    quality_status: Literal[
        "draft", "source_gap", "conflict_review", "content_review", "publishable", "published"
    ] = "draft"

    @model_validator(mode="after")
    def validate_unit_scope(self) -> "ChapterDossier":
        chapter_concepts = set(self.concept_ids)
        if not set(self.anchor_concept_ids) <= chapter_concepts:
            raise ValueError("anchor concept IDs must be within chapter concept IDs")
        if any(not set(unit.concept_ids) <= chapter_concepts for unit in self.knowledge_units):
            raise ValueError("knowledge unit concept IDs must be within chapter concept IDs")
        return self
