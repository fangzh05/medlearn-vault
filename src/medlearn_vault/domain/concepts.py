from typing import Literal

from pydantic import Field, model_validator

from medlearn_vault.domain.base import DomainModel
from medlearn_vault.identifiers import normalize_text

ConceptType = Literal[
    "disease",
    "syndrome",
    "symptom",
    "sign",
    "anatomy",
    "physiology",
    "pathology",
    "mechanism",
    "investigation",
    "imaging_sign",
    "drug",
    "procedure",
    "complication",
    "score",
    "guideline",
    "organism",
    "gene",
    "biomarker",
    "other",
]


class ConceptAlias(DomainModel):
    text: str = Field(min_length=1)
    normalized: str | None = None
    language: str
    alias_type: Literal["abbreviation", "translation", "synonym", "legacy", "trade_name", "other"]
    source_id: str | None = None

    @model_validator(mode="after")
    def fill_normalized(self) -> "ConceptAlias":
        object.__setattr__(self, "normalized", normalize_text(self.text))
        return self


class ConceptRelation(DomainModel):
    relation_id: str = Field(pattern=r"^rel_[a-f0-9]{12,64}$")
    source_concept_id: str = Field(min_length=1)
    relation_type: str = Field(min_length=1)
    target_concept_id: str = Field(min_length=1)
    citations: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)


class DisciplineLens(DomainModel):
    lens_id: str
    concept_id: str = Field(min_length=1)
    discipline_id: str
    course_id: str | None = None
    focus_questions: list[str] = Field(default_factory=list)
    chapter_refs: list[str] = Field(default_factory=list)
    knowledge_unit_refs: list[str] = Field(default_factory=list)
    exam_point_refs: list[str] = Field(default_factory=list)
    discipline_summary: str | None = None


class ConceptEntity(DomainModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    concept_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,127}$")
    canonical_name: str = Field(min_length=1)
    preferred_english: str | None = None
    concept_type: ConceptType
    aliases: list[ConceptAlias] = Field(default_factory=list)
    relations: list[ConceptRelation] = Field(default_factory=list)
    discipline_lenses: list[DisciplineLens] = Field(default_factory=list)
    status: Literal["active", "deprecated", "merged", "split_pending"] = "active"
    merged_into: str | None = None

    @model_validator(mode="after")
    def validate_references(self) -> "ConceptEntity":
        if any(lens.concept_id != self.concept_id for lens in self.discipline_lenses):
            raise ValueError("every discipline lens must reference the enclosing concept_id")
        if any(rel.source_concept_id != self.concept_id for rel in self.relations):
            raise ValueError("every relation source must reference the enclosing concept_id")
        return self
