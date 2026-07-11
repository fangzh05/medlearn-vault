from typing import Literal

from pydantic import Field

from medlearn_vault.domain.base import DomainModel
from medlearn_vault.domain.sources import SourceCitation


class MedicalClaim(DomainModel):
    claim_id: str = Field(pattern=r"^cl_[a-f0-9]{12,64}$")
    claim_type: str
    statement: str = Field(min_length=1)
    truth_value: Literal["supported", "refuted", "uncertain"] = "supported"
    concept_ids: list[str] = Field(min_length=1)
    discipline_ids: list[str] = Field(default_factory=list)
    citations: list[SourceCitation] = Field(default_factory=list)
    verification_status: Literal["unverified", "source_backed", "conflict", "verified"]
    medical_authority: int = Field(ge=0, le=5)
    course_relevance: dict[str, int] = Field(default_factory=dict)
