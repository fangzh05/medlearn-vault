from typing import Annotated, Literal

from pydantic import Field, model_validator

from medlearn_vault.domain.base import DomainModel
from medlearn_vault.domain.sources import SourceCitation
from medlearn_vault.identifiers import claim_fingerprint

CourseRelevanceScore = Annotated[int, Field(ge=0, le=5)]


class MedicalClaim(DomainModel):
    claim_id: str = Field(pattern=r"^claim_[a-f0-9]{32}$")
    claim_type: str
    statement: str = Field(min_length=1)
    evidence_state: Literal["unassessed", "supported", "refuted", "conflicting"] = "unassessed"
    concept_ids: tuple[str, ...] = Field(min_length=1)
    discipline_ids: tuple[str, ...] = ()
    citations: tuple[SourceCitation, ...] = ()
    verification_status: Literal[
        "unverified_chat",
        "source_backed",
        "verified_reference",
        "conflicted",
        "deprecated",
    ] = "unverified_chat"
    course_relevance: dict[str, CourseRelevanceScore] = Field(default_factory=dict)
    fingerprint: str = Field(default="", pattern=r"^clfp_[a-f0-9]{16}$")

    @model_validator(mode="after")
    def validate_evidence(self) -> "MedicalClaim":
        object.__setattr__(self, "fingerprint", claim_fingerprint(self.statement, self.concept_ids))
        if (
            self.verification_status in {"source_backed", "verified_reference"}
            and not self.citations
        ):
            raise ValueError("source-backed and verified claims require at least one citation")
        if self.verification_status == "unverified_chat" and self.evidence_state == "supported":
            raise ValueError("unverified chat claims cannot be marked supported")
        return self
