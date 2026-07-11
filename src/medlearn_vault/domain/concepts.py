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

RelationType = Literal[
    "is_a",
    "part_of",
    "causes",
    "risk_factor_for",
    "leads_to",
    "manifests_as",
    "diagnosed_by",
    "differentiated_from",
    "treated_by",
    "contraindicates",
    "complicated_by",
    "associated_with",
    "progresses_to",
    "measured_by",
    "located_in",
    "acts_on",
    "exam_confused_with",
    "discipline_view_of",
    "taught_in",
    "examined_in",
    "prerequisite_for",
    "clinically_connects_to",
    "pathology_basis_of",
    "pharmacologic_target_of",
    "imaging_correlate_of",
    "surgical_management_of",
]


class ExternalIdentifiers(DomainModel):
    mesh: list[str] = Field(default_factory=list)
    snomed_ct: list[str] = Field(default_factory=list)
    icd10: list[str] = Field(default_factory=list)
    loinc: list[str] = Field(default_factory=list)
    rxnorm: list[str] = Field(default_factory=list)


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
    relation_type: RelationType
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
    schema_version: Literal["1.1.0"] = "1.1.0"
    concept_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,127}$")
    canonical_name: str = Field(min_length=1)
    preferred_english: str | None = None
    concept_type: ConceptType
    external_identifiers: ExternalIdentifiers = Field(default_factory=ExternalIdentifiers)
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
        lens_keys = [(lens.discipline_id, lens.course_id) for lens in self.discipline_lenses]
        if len(lens_keys) != len(set(lens_keys)):
            raise ValueError("duplicate discipline lens for the same discipline and course")
        unique_relations: dict[tuple[str, str, str], ConceptRelation] = {}
        for relation in self.relations:
            key = (
                relation.source_concept_id,
                relation.relation_type,
                relation.target_concept_id,
            )
            unique_relations.setdefault(key, relation)
        object.__setattr__(self, "relations", list(unique_relations.values()))
        if self.status == "merged" and not self.merged_into:
            raise ValueError("merged concepts require merged_into")
        if self.status != "merged" and self.merged_into is not None:
            raise ValueError("only merged concepts may define merged_into")
        if self.merged_into == self.concept_id:
            raise ValueError("a concept cannot be merged into itself")
        return self
