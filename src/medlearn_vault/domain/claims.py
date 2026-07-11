from typing import Annotated, Literal

from pydantic import Field, model_validator

from medlearn_vault.domain.base import DomainModel
from medlearn_vault.domain.sources import SourceCitation

CourseRelevanceScore = Annotated[int, Field(ge=0, le=5)]


class MedicalClaim(DomainModel):
    claim_id: str = Field(pattern=r"^cl_[a-f0-9]{12,64}$")
    claim_type: str
    statement: str = Field(min_length=1)
    evidence_state: Literal["unassessed", "supported", "refuted", "conflicting"] = "unassessed"
    concept_ids: list[str] = Field(min_length=1)
    discipline_ids: list[str] = Field(default_factory=list)
    citations: list[SourceCitation] = Field(default_factory=list)
    verification_status: Literal[
        "unverified_chat",
        "source_backed",
        "verified_reference",
        "conflicted",
        "deprecated",
    ] = "unverified_chat"
    medical_authority: int = Field(ge=0, le=5)
    course_relevance: dict[str, CourseRelevanceScore] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_evidence(self) -> "MedicalClaim":
        if (
            self.verification_status in {"source_backed", "verified_reference"}
            and not self.citations
        ):
            raise ValueError("source-backed and verified claims require at least one citation")
        if self.verification_status == "verified_reference" and self.medical_authority < 3:
            raise ValueError("verified claims require medical_authority >= 3")
        if self.verification_status == "unverified_chat" and self.evidence_state == "supported":
            raise ValueError("unverified chat claims cannot be marked supported")
        return self
