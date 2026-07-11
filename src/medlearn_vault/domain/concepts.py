from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from medlearn_vault.domain.base import DomainModel
from medlearn_vault.identifiers import (
    concept_fingerprint,
    normalize_text,
    relation_fingerprint,
)
from medlearn_vault.identifiers import (
    content_hash as compute_content_hash,
)

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


class ExternalIdentifiers(DomainModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mesh: tuple[str, ...] = ()
    snomed_ct: tuple[str, ...] = ()
    icd10: tuple[str, ...] = ()
    loinc: tuple[str, ...] = ()
    rxnorm: tuple[str, ...] = ()


class ConceptAlias(DomainModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

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
    schema_version: Literal["1.1.1"] = "1.1.1"
    relation_id: str = Field(pattern=r"^relation_[a-f0-9]{32}$")
    source_concept_id: str = Field(min_length=1)
    relation_type: str = Field(min_length=1)
    target_concept_id: str = Field(min_length=1)
    supporting_claim_ids: tuple[str, ...] = ()
    confidence: float | None = Field(default=None, ge=0, le=1)
    match_fingerprint: str = Field(default="", pattern=r"^relfp_[a-f0-9]{16}$")
    content_hash: str = Field(default="", pattern=r"^content_[a-f0-9]{64}$")

    @model_validator(mode="after")
    def refresh_fingerprint(self) -> "ConceptRelation":
        object.__setattr__(
            self,
            "match_fingerprint",
            relation_fingerprint(
                self.source_concept_id, self.relation_type, self.target_concept_id
            ),
        )
        object.__setattr__(
            self,
            "content_hash",
            compute_content_hash(
                self.source_concept_id,
                self.relation_type,
                self.target_concept_id,
                self.supporting_claim_ids,
                self.confidence,
            ),
        )
        return self


class DisciplineLens(DomainModel):
    schema_version: Literal["1.1.1"] = "1.1.1"
    lens_id: str = Field(pattern=r"^lens_[a-f0-9]{32}$")
    concept_id: str = Field(min_length=1)
    discipline_id: str
    course_id: str | None = None
    focus_questions: tuple[str, ...] = ()
    discipline_summary: str | None = None


class ConceptEntity(DomainModel):
    schema_version: Literal["1.1.1"] = "1.1.1"
    concept_id: str = Field(pattern=r"^concept_[a-f0-9]{32}$")
    canonical_name: str = Field(min_length=1)
    preferred_english: str | None = None
    concept_type: ConceptType
    scope_note: str = Field(min_length=1)
    definition: str | None = None
    inclusion_terms: tuple[str, ...] = ()
    exclusion_terms: tuple[str, ...] = ()
    broader_concept_ids: tuple[str, ...] = ()
    external_identifiers: ExternalIdentifiers = Field(default_factory=ExternalIdentifiers)
    aliases: tuple[ConceptAlias, ...] = ()
    status: Literal["active", "deprecated", "merged", "split_pending"] = "active"
    merged_into: str | None = None
    match_fingerprint: str = Field(default="", pattern=r"^cfp_[a-f0-9]{16}$")
    content_hash: str = Field(default="", pattern=r"^content_[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_references(self) -> "ConceptEntity":
        object.__setattr__(
            self,
            "match_fingerprint",
            concept_fingerprint(
                self.concept_type, self.canonical_name, [alias.text for alias in self.aliases]
            ),
        )
        object.__setattr__(
            self,
            "content_hash",
            compute_content_hash(
                self.canonical_name,
                self.preferred_english,
                self.concept_type,
                self.scope_note,
                self.definition,
                self.inclusion_terms,
                self.exclusion_terms,
                self.broader_concept_ids,
                self.external_identifiers.model_dump(mode="json"),
                [alias.model_dump(mode="json") for alias in self.aliases],
            ),
        )
        if self.status == "merged" and not self.merged_into:
            raise ValueError("merged concepts require merged_into")
        if self.status != "merged" and self.merged_into is not None:
            raise ValueError("only merged concepts may define merged_into")
        if self.merged_into == self.concept_id:
            raise ValueError("a concept cannot be merged into itself")
        return self
