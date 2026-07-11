from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from medlearn_vault.domain.base import DomainModel
from medlearn_vault.domain.ids import ClaimId, ConceptId
from medlearn_vault.domain.sources import SourceCitation
from medlearn_vault.identifiers import claim_fingerprint


class CourseRelevance(DomainModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    course_id: str = Field(min_length=1)
    score: int = Field(ge=0, le=5)


class MedicalClaim(DomainModel):
    schema_version: Literal["1.2.0"] = "1.2.0"
    claim_id: ClaimId
    claim_type: str
    statement: str = Field(min_length=1)
    evidence_state: Literal["unassessed", "supported", "refuted", "conflicting"] = "unassessed"
    concept_ids: tuple[ConceptId, ...] = Field(min_length=1)
    discipline_ids: tuple[str, ...] = ()
    citations: tuple[SourceCitation, ...] = ()
    verification_status: Literal[
        "unverified_chat",
        "source_backed",
        "verified_reference",
    ] = "unverified_chat"
    claim_status: Literal["active", "deprecated", "superseded"] = "active"
    review_status: Literal["normal", "conflict_review", "resolved"] = "normal"
    superseded_by_claim_ids: tuple[ClaimId, ...] = ()
    course_relevance: tuple[CourseRelevance, ...] = ()
    match_fingerprint: str = Field(default="", pattern=r"^clfp_[a-f0-9]{16}$")

    @model_validator(mode="after")
    def validate_evidence(self) -> "MedicalClaim":
        object.__setattr__(
            self, "match_fingerprint", claim_fingerprint(self.statement, self.concept_ids)
        )
        if (
            self.verification_status in {"source_backed", "verified_reference"}
            and not self.citations
        ):
            raise ValueError("source-backed and verified claims require at least one citation")
        allowed = {
            "unverified_chat": {"unassessed"},
            "source_backed": {"supported", "refuted", "conflicting"},
            "verified_reference": {"supported", "refuted"},
        }
        if self.evidence_state not in allowed[self.verification_status]:
            raise ValueError("evidence_state is incompatible with verification_status")
        course_ids = [item.course_id for item in self.course_relevance]
        if len(course_ids) != len(set(course_ids)):
            raise ValueError("course relevance contains duplicate course IDs")
        if self.evidence_state == "conflicting" and self.review_status == "normal":
            raise ValueError("conflicting evidence requires conflict_review or resolved review")
        if self.claim_status == "superseded" and not self.superseded_by_claim_ids:
            raise ValueError("superseded claims require replacement claim IDs")
        if self.claim_status != "superseded" and self.superseded_by_claim_ids:
            raise ValueError("only superseded claims may define replacement claim IDs")
        return self
