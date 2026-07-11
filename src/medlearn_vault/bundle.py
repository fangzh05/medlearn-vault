"""Load and validate a directory of contract records."""

import json
from pathlib import Path
from typing import Literal, TypeVar

from pydantic import BaseModel

from medlearn_vault.domain import (
    ChapterDossier,
    ConceptEntity,
    ConceptRelation,
    DisciplineLens,
    LearningCapture,
    MedicalClaim,
    SourceDocument,
)
from medlearn_vault.domain.base import DomainModel

Record = TypeVar("Record", bound=BaseModel)


class ValidationIssue(DomainModel):
    code: str
    severity: Literal["error", "warning"] = "error"
    record_type: str
    record_id: str
    field: str
    target_id: str | None = None
    message: str


class ContractBundle(DomainModel):
    sources: tuple[SourceDocument, ...]
    concepts: tuple[ConceptEntity, ...]
    claims: tuple[MedicalClaim, ...]
    relations: tuple[ConceptRelation, ...]
    discipline_lenses: tuple[DisciplineLens, ...]
    chapters: tuple[ChapterDossier, ...]
    learning_captures: tuple[LearningCapture, ...]

    @classmethod
    def from_directory(cls, directory: Path) -> "ContractBundle":
        def records(filename: str, model: type[Record]) -> tuple[Record, ...]:
            values = json.loads((directory / filename).read_text(encoding="utf-8"))
            if not isinstance(values, list):
                values = [values]
            return tuple(model.model_validate(value) for value in values)

        return cls(
            sources=records("sources.json", SourceDocument),
            concepts=records("concepts.json", ConceptEntity),
            claims=records("claims.json", MedicalClaim),
            relations=records("relations.json", ConceptRelation),
            discipline_lenses=records("discipline_lenses.json", DisciplineLens),
            chapters=records("chapters.json", ChapterDossier),
            learning_captures=records("learning_capture.json", LearningCapture),
        )

    def validate_integrity(self) -> tuple[ValidationIssue, ...]:
        issues: list[ValidationIssue] = []

        def issue(
            code: str,
            record_type: str,
            record_id: str,
            field: str,
            target_id: str | None,
            message: str,
        ) -> None:
            issues.append(
                ValidationIssue(
                    code=code,
                    record_type=record_type,
                    record_id=record_id,
                    field=field,
                    target_id=target_id,
                    message=message,
                )
            )

        groups = (
            ("source", self.sources, "source_id"),
            ("concept", self.concepts, "concept_id"),
            ("claim", self.claims, "claim_id"),
            ("relation", self.relations, "relation_id"),
            ("lens", self.discipline_lenses, "lens_id"),
            ("chapter", self.chapters, "chapter_id"),
            ("capture", self.learning_captures, "session_id"),
        )
        for record_type, records, field in groups:
            ids = [str(getattr(record, field)) for record in records]
            # ponytail: bundles are small; replace this scan only when profiling says otherwise.
            for duplicate in sorted({record_id for record_id in ids if ids.count(record_id) > 1}):
                issue("DUPLICATE_ID", record_type, duplicate, field, duplicate, "duplicate ID")

        sources = {item.source_id: item for item in self.sources}
        concepts = {item.concept_id: item for item in self.concepts}
        claims = {item.claim_id: item for item in self.claims}

        def require(
            targets: tuple[str, ...],
            known: set[str],
            code: str,
            record_type: str,
            record_id: str,
            field: str,
        ) -> None:
            for target in targets:
                if target not in known:
                    issue(code, record_type, record_id, field, target, f"missing target {target}")

        for claim in self.claims:
            require(
                claim.concept_ids,
                set(concepts),
                "MISSING_CONCEPT",
                "claim",
                claim.claim_id,
                "concept_ids",
            )
            require(
                tuple(item.source_id for item in claim.citations),
                set(sources),
                "MISSING_SOURCE",
                "claim",
                claim.claim_id,
                "citations",
            )
            require(
                claim.superseded_by_claim_ids,
                set(claims),
                "MISSING_CLAIM",
                "claim",
                claim.claim_id,
                "superseded_by_claim_ids",
            )
            if claim.verification_status == "verified_reference":
                for citation in claim.citations:
                    source = sources.get(citation.source_id)
                    if source is not None and (
                        source.source_type == "learning_chat" or source.authority <= 0
                    ):
                        issue(
                            "INVALID_VERIFIED_SOURCE",
                            "claim",
                            claim.claim_id,
                            "citations",
                            citation.source_id,
                            "verified claim requires an authoritative non-chat source",
                        )

        for relation in self.relations:
            require(
                (relation.source_concept_id, relation.target_concept_id),
                set(concepts),
                "MISSING_CONCEPT",
                "relation",
                relation.relation_id,
                "concept",
            )
            require(
                relation.supporting_claim_ids,
                set(claims),
                "MISSING_SUPPORTING_CLAIM",
                "relation",
                relation.relation_id,
                "supporting_claim_ids",
            )
            for claim_id in relation.supporting_claim_ids:
                if claim_id in claims and claims[claim_id].claim_status != "active":
                    issue(
                        "DEPRECATED_CLAIM_REFERENCE",
                        "relation",
                        relation.relation_id,
                        "supporting_claim_ids",
                        claim_id,
                        "relation references inactive claim",
                    )

        for lens in self.discipline_lenses:
            require(
                (lens.concept_id,),
                set(concepts),
                "MISSING_CONCEPT",
                "lens",
                lens.lens_id,
                "concept_id",
            )
        for chapter in self.chapters:
            require(
                chapter.concept_ids,
                set(concepts),
                "MISSING_CONCEPT",
                "chapter",
                chapter.chapter_id,
                "concept_ids",
            )
            for unit in chapter.knowledge_units:
                require(
                    unit.concept_ids,
                    set(concepts),
                    "MISSING_CONCEPT",
                    "knowledge_unit",
                    unit.unit_id,
                    "concept_ids",
                )
                require(
                    unit.claim_ids,
                    set(claims),
                    "MISSING_CLAIM",
                    "knowledge_unit",
                    unit.unit_id,
                    "claim_ids",
                )
                require(
                    unit.source_refs,
                    set(sources),
                    "MISSING_SOURCE",
                    "knowledge_unit",
                    unit.unit_id,
                    "source_refs",
                )
        for capture in self.learning_captures:
            if capture.source_id not in sources:
                issue(
                    "MISSING_SOURCE",
                    "capture",
                    capture.session_id,
                    "source_id",
                    capture.source_id,
                    "capture source is missing",
                )
            elif sources[capture.source_id].source_type != "learning_chat":
                issue(
                    "INVALID_CAPTURE_SOURCE_TYPE",
                    "capture",
                    capture.session_id,
                    "source_id",
                    capture.source_id,
                    "capture source must be learning_chat",
                )
            concept_refs = tuple(
                target
                for mention in capture.concept_mentions
                for target in mention.candidate_concept_ids
            ) + tuple(
                target
                for observation in capture.misconception_observations
                for target in observation.concept_ids
            )
            require(
                concept_refs,
                set(concepts),
                "MISSING_CONCEPT",
                "capture",
                capture.session_id,
                "concept_ids",
            )
            correction_refs = tuple(
                target
                for observation in capture.misconception_observations
                for target in observation.correction_claim_ids
            )
            require(
                correction_refs,
                set(claims),
                "MISSING_CLAIM",
                "capture",
                capture.session_id,
                "correction_claim_ids",
            )

        redirects = {
            item.concept_id: item.merged_into
            for item in self.concepts
            if item.status == "merged" and item.merged_into is not None
        }
        for start in redirects:
            seen: set[str] = set()
            current: str | None = start
            while current in redirects:
                if current in seen:
                    issue(
                        "MERGE_REDIRECT_CYCLE",
                        "concept",
                        start,
                        "merged_into",
                        current,
                        "merge redirect cycle",
                    )
                    break
                seen.add(current)
                current = redirects[current]
            if current is not None and current not in concepts:
                issue(
                    "MISSING_CONCEPT",
                    "concept",
                    start,
                    "merged_into",
                    current,
                    "merge target is missing",
                )

        return tuple(issues)
