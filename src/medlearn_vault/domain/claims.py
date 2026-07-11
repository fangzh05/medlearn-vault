from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from medlearn_vault.domain.base import DomainModel
from medlearn_vault.domain.sources import SourceCitation
from medlearn_vault.identifiers import claim_fingerprint
from medlearn_vault.identifiers import content_hash as compute_content_hash


class CourseRelevance(DomainModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    course_id: str = Field(min_length=1)
    score: int = Field(ge=0, le=5)


class MedicalClaim(DomainModel):
    schema_version: Literal["1.1.1"] = "1.1.1"
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
    ] = "unverified_chat"
    claim_status: Literal["active", "deprecated", "superseded"] = "active"
    course_relevance: tuple[CourseRelevance, ...] = ()
    match_fingerprint: str = Field(default="", pattern=r"^clfp_[a-f0-9]{16}$")
    content_hash: str = Field(default="", pattern=r"^content_[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_evidence(self) -> "MedicalClaim":
        object.__setattr__(
            self, "match_fingerprint", claim_fingerprint(self.statement, self.concept_ids)
        )
        object.__setattr__(
            self,
            "content_hash",
            compute_content_hash(
                self.claim_type,
                self.statement,
                self.evidence_state,
                self.concept_ids,
                self.discipline_ids,
                [citation.model_dump(mode="json") for citation in self.citations],
                self.verification_status,
                self.claim_status,
                [item.model_dump(mode="json") for item in self.course_relevance],
            ),
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
            "conflicted": {"conflicting"},
        }
        if self.evidence_state not in allowed[self.verification_status]:
            raise ValueError("evidence_state is incompatible with verification_status")
        course_ids = [item.course_id for item in self.course_relevance]
        if len(course_ids) != len(set(course_ids)):
            raise ValueError("course relevance contains duplicate course IDs")
        return self
