"""Deterministic, review-only reconciliation of untrusted Work capture drafts."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any, Literal

from pydantic import Field, model_validator

from medlearn_vault.bundle import ContractBundle
from medlearn_vault.domain.base import AwareDatetime, DomainModel
from medlearn_vault.domain.concepts import ConceptAlias, ConceptType
from medlearn_vault.domain.ids import ClaimId, ConceptId, ScopedExternalId, SourceId
from medlearn_vault.domain.learner import (
    ConceptMention,
    LearnerEvidence,
    LearningCapture,
    MisconceptionObservation,
    OpenQuestion,
)
from medlearn_vault.identifiers import normalize_text
from medlearn_vault.registry import resolve_alias
from medlearn_vault.terminology import english_abbreviations, format_concept_label

WORKFLOW_VERSION: Literal["0.2.0"] = "0.2.0"
MAX_EVIDENCE_MESSAGES = 200
MAX_CANDIDATES_PER_KIND = 200
MAX_EXCERPT_LENGTH = 1000
MAX_STATEMENT_LENGTH = 4000


class CaptureContext(DomainModel):
    source_id: SourceId
    session_id: ScopedExternalId
    discipline_id: str = Field(min_length=1, max_length=128)
    course_id: str | None = Field(default=None, min_length=1, max_length=128)
    chapter_id: str | None = Field(default=None, min_length=1, max_length=128)
    locale: Literal["zh-CN"] = "zh-CN"
    origin: Literal["chatgpt_work"] = "chatgpt_work"
    session_started_at: AwareDatetime
    captured_at: AwareDatetime

    @model_validator(mode="after")
    def validate_interval(self) -> CaptureContext:
        if self.session_started_at > self.captured_at:
            raise ValueError("session_started_at must not be after captured_at")
        return self


class EvidenceMessage(DomainModel):
    message_id: ScopedExternalId
    role: Literal["user", "assistant"]
    observed_at: AwareDatetime
    excerpt: str | None = Field(default=None, max_length=MAX_EXCERPT_LENGTH)


class ExtractedConceptMention(DomainModel):
    surface_text: str = Field(min_length=1, max_length=MAX_STATEMENT_LENGTH)
    evidence_message_ids: tuple[ScopedExternalId, ...]
    suggested_canonical_name: str | None = Field(default=None, min_length=1)
    suggested_preferred_english: str | None = Field(default=None, min_length=1)
    suggested_concept_type: ConceptType | None = None
    suggested_scope_note: str | None = Field(default=None, min_length=1)


class ExtractedClaimCandidate(DomainModel):
    statement: str = Field(min_length=1, max_length=MAX_STATEMENT_LENGTH)
    claim_type: str = Field(min_length=1, max_length=128)
    concept_terms: tuple[str, ...]
    evidence_message_ids: tuple[ScopedExternalId, ...]
    question_priority: Literal["low", "medium", "high"] | None = None

    @model_validator(mode="after")
    def validate_question_priority(self) -> ExtractedClaimCandidate:
        if (self.claim_type == "question") != (self.question_priority is not None):
            raise ValueError(
                "question claims require question_priority and only questions may set it"
            )
        return self


class ExtractedLearnerEvidenceCandidate(DomainModel):
    concept_terms: tuple[str, ...]
    evidence_message_ids: tuple[ScopedExternalId, ...]
    evidence_type: Literal[
        "correct_independent",
        "correct_after_hint",
        "guessed_correct",
        "partial",
        "unknown",
        "incorrect",
        "high_confidence_incorrect",
        "self_report_only",
    ]
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(min_length=1, max_length=MAX_STATEMENT_LENGTH)


class ExtractedMisconceptionCandidate(DomainModel):
    observed_error_logic: str = Field(min_length=1, max_length=MAX_STATEMENT_LENGTH)
    concept_terms: tuple[str, ...]
    evidence_message_ids: tuple[ScopedExternalId, ...]
    proposed_correction: str | None = Field(default=None, max_length=MAX_STATEMENT_LENGTH)
    correction_terms: tuple[str, ...] = ()
    severity: Literal["low", "medium", "high"]


class CaptureDraft(DomainModel):
    draft_version: Literal["0.2.0"] = WORKFLOW_VERSION
    context: CaptureContext
    evidence_messages: tuple[EvidenceMessage, ...] = Field(max_length=MAX_EVIDENCE_MESSAGES)
    concept_mentions: tuple[ExtractedConceptMention, ...] = Field(
        max_length=MAX_CANDIDATES_PER_KIND
    )
    claim_candidates: tuple[ExtractedClaimCandidate, ...] = Field(
        max_length=MAX_CANDIDATES_PER_KIND
    )
    learner_evidence_candidates: tuple[ExtractedLearnerEvidenceCandidate, ...] = Field(
        default=(), max_length=MAX_CANDIDATES_PER_KIND
    )
    misconception_candidates: tuple[ExtractedMisconceptionCandidate, ...] = Field(
        max_length=MAX_CANDIDATES_PER_KIND
    )

    @model_validator(mode="after")
    def validate_evidence(self) -> CaptureDraft:
        ids = [item.message_id for item in self.evidence_messages]
        if len(ids) != len(set(ids)):
            raise ValueError("evidence message IDs must be unique")
        known = set(ids)
        references = (
            *(item.evidence_message_ids for item in self.concept_mentions),
            *(item.evidence_message_ids for item in self.claim_candidates),
            *(item.evidence_message_ids for item in self.learner_evidence_candidates),
            *(item.evidence_message_ids for item in self.misconception_candidates),
        )
        if any(not set(group) <= known for group in references):
            raise ValueError("all evidence_message_ids must exist in evidence_messages")
        if any(not group for group in references):
            raise ValueError("all extracted candidates require evidence_message_ids")
        roles = {item.message_id: item.role for item in self.evidence_messages}
        assertion_groups = (
            *(item.evidence_message_ids for item in self.claim_candidates),
            *(item.evidence_message_ids for item in self.learner_evidence_candidates),
        )
        if any(len({roles[mid] for mid in group}) != 1 for group in assertion_groups):
            raise ValueError("assertion evidence must have exactly one derived speaker role")
        if any(
            {roles[mid] for mid in item.evidence_message_ids} != {"user"}
            for item in self.learner_evidence_candidates
        ):
            raise ValueError("learner evidence must be owned by user evidence messages")
        if any(
            item.observed_at < self.context.session_started_at
            or item.observed_at > self.context.captured_at
            for item in self.evidence_messages
        ):
            raise ValueError("evidence times must fall within the capture interval")
        return self


class ProposalIssue(DomainModel):
    code: str
    severity: Literal["error", "warning", "review"]
    field: str
    message: str
    candidate_id: str | None = None
    target_ids: tuple[str, ...] = ()


class ProposalConceptRef(DomainModel):
    concept_id: ConceptId | None = None
    candidate_id: str | None = Field(default=None, pattern=r"^candidate_concept_[a-f0-9]{32}$")

    @model_validator(mode="after")
    def exactly_one(self) -> ProposalConceptRef:
        if (self.concept_id is None) == (self.candidate_id is None):
            raise ValueError("exactly one concept reference target is required")
        return self


class ConceptResolutionProposal(DomainModel):
    resolution_id: str
    surface_text: str
    status: Literal[
        "matched", "redirected", "ambiguous", "new_candidate", "review_required", "rejected"
    ]
    matched_concept_id: ConceptId | None = None
    candidate_concept_ids: tuple[ConceptId, ...] = ()
    new_candidate_id: str | None = None
    evidence_message_ids: tuple[ScopedExternalId, ...]

    @model_validator(mode="after")
    def invariant(self) -> ConceptResolutionProposal:
        if self.status in {"matched", "redirected"} and self.matched_concept_id is None:
            raise ValueError("matched resolutions require matched_concept_id")
        if self.status == "ambiguous" and len(self.candidate_concept_ids) < 2:
            raise ValueError("ambiguous resolutions require at least two candidates")
        if self.status == "new_candidate" and self.new_candidate_id is None:
            raise ValueError("new_candidate resolutions require new_candidate_id")
        if self.status in {"review_required", "rejected"} and self.matched_concept_id is not None:
            raise ValueError("unresolved concepts cannot be automatically selected")
        return self


class NewConceptCandidate(DomainModel):
    candidate_id: str = Field(pattern=r"^candidate_concept_[a-f0-9]{32}$")
    canonical_name: str
    preferred_english: str | None = None
    concept_type: ConceptType
    scope_note: str
    aliases: tuple[ConceptAlias, ...] = ()
    evidence_message_ids: tuple[ScopedExternalId, ...]


class ClaimProposal(DomainModel):
    candidate_id: str = Field(pattern=r"^candidate_claim_[a-f0-9]{32}$")
    statement: str
    claim_type: str
    concept_refs: tuple[ProposalConceptRef, ...]
    evidence_message_ids: tuple[ScopedExternalId, ...]
    proposed_verification_status: Literal["unverified_chat"] = "unverified_chat"
    proposed_evidence_state: Literal["unassessed"] = "unassessed"
    matching_existing_claim_ids: tuple[ClaimId, ...] = ()


class LearningObservationCandidate(DomainModel):
    candidate_id: str = Field(pattern=r"^candidate_observation_[a-f0-9]{32}$")
    observation_type: Literal[
        "correct_recall", "incorrect_recall", "uncertain", "misconception", "question"
    ]
    concept_refs: tuple[ProposalConceptRef, ...]
    evidence_message_ids: tuple[ScopedExternalId, ...]
    observed_text: str
    proposed_correction: str | None = None
    correction_claim_ids: tuple[ClaimId, ...] = ()


class LearningCaptureCandidate(DomainModel):
    capture: LearningCapture
    observations: tuple[LearningObservationCandidate, ...]


class CaptureProposal(DomainModel):
    proposal_version: Literal["0.2.0"] = WORKFLOW_VERSION
    proposal_id: str = Field(pattern=r"^proposal_[a-f0-9]{32}$")
    status: Literal["ready_for_review", "blocked"]
    draft_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    base_bundle_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    proposal_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    context: CaptureContext
    concept_resolutions: tuple[ConceptResolutionProposal, ...]
    new_concept_candidates: tuple[NewConceptCandidate, ...]
    claim_proposals: tuple[ClaimProposal, ...]
    learning_capture_candidate: LearningCaptureCandidate
    issues: tuple[ProposalIssue, ...]

    @model_validator(mode="after")
    def validate_references_and_status(self) -> CaptureProposal:
        existing = {
            item.matched_concept_id
            for item in self.concept_resolutions
            if item.status in {"matched", "redirected"}
        }
        candidates = {item.candidate_id for item in self.new_concept_candidates}
        refs = (
            *(ref for item in self.claim_proposals for ref in item.concept_refs),
            *(
                ref
                for item in self.learning_capture_candidate.observations
                for ref in item.concept_refs
            ),
        )
        if any(
            (ref.concept_id is not None and ref.concept_id not in existing)
            or (ref.candidate_id is not None and ref.candidate_id not in candidates)
            for ref in refs
        ):
            raise ValueError("proposal concept reference does not resolve within the proposal")
        blocked = any(item.severity in {"error", "review"} for item in self.issues)
        if (self.status == "blocked") != blocked:
            raise ValueError("proposal status must reflect error and review issues")
        return self


SET_LIKE_FIELDS = frozenset(
    {
        "candidate_concept_ids",
        "concept_ids",
        "concept_refs",
        "concept_terms",
        "correction_claim_ids",
        "correction_terms",
        "discipline_ids",
        "matching_existing_claim_ids",
        "target_ids",
    }
)


def _canonical(
    value: Any, *, exclude_proposal_digest: bool = False, field_name: str | None = None
) -> Any:
    if isinstance(value, DomainModel):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return {
            key: _canonical(item, exclude_proposal_digest=exclude_proposal_digest, field_name=key)
            for key, item in sorted(value.items())
            if not (exclude_proposal_digest and key == "proposal_digest")
        }
    if isinstance(value, (list, tuple)):
        items = [
            _canonical(item, exclude_proposal_digest=exclude_proposal_digest) for item in value
        ]
        if field_name in SET_LIKE_FIELDS:
            return sorted(
                items, key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True)
            )
        return items
    return value


def _bytes(value: Any, *, exclude_proposal_digest: bool = False) -> bytes:
    return json.dumps(
        _canonical(value, exclude_proposal_digest=exclude_proposal_digest),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(value: Any, *, exclude_proposal_digest: bool = False) -> str:
    return (
        "sha256:"
        + hashlib.sha256(_bytes(value, exclude_proposal_digest=exclude_proposal_digest)).hexdigest()
    )


def canonical_capture_draft_json(draft: CaptureDraft) -> bytes:
    return _bytes(draft)


def capture_draft_digest(draft: CaptureDraft) -> str:
    return _digest(draft)


def contract_bundle_digest(bundle: ContractBundle) -> str:
    return _digest(bundle)


def capture_proposal_digest(proposal: CaptureProposal) -> str:
    return _digest(proposal, exclude_proposal_digest=True)


def _id(prefix: str, *parts: Any) -> str:
    return f"{prefix}_{hashlib.sha256(_bytes(parts)).hexdigest()[:32]}"


def _statement(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).strip()
    value = re.sub(r"\s+", " ", value)
    return value.translate(str.maketrans("，。；：", ",.;:")).rstrip(".")


def _ordered_unique(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def build_capture_proposal(bundle: ContractBundle, draft: CaptureDraft) -> CaptureProposal:
    """Build a proposal without I/O, mutation, environment access, or probabilistic behavior."""
    issues: list[ProposalIssue] = []
    source = next(
        (item for item in bundle.sources if item.source_id == draft.context.source_id), None
    )
    if source is None:
        issues.append(
            ProposalIssue(
                code="MISSING_SOURCE",
                severity="error",
                field="context.source_id",
                message="capture source does not exist",
            )
        )
    elif source.source_type != "learning_chat":
        issues.append(
            ProposalIssue(
                code="INVALID_CAPTURE_SOURCE_TYPE",
                severity="error",
                field="context.source_id",
                message="capture source must be learning_chat",
            )
        )

    concepts = {item.concept_id: item for item in bundle.concepts}
    resolutions: list[ConceptResolutionProposal] = []
    new_candidates: list[NewConceptCandidate] = []
    by_term: dict[str, ProposalConceptRef | None] = {}
    mentions = {normalize_text(item.surface_text): item for item in draft.concept_mentions}
    all_terms = sorted(
        {item.surface_text for item in draft.concept_mentions}
        | {term for item in draft.claim_candidates for term in item.concept_terms}
        | {term for item in draft.learner_evidence_candidates for term in item.concept_terms}
        | {term for item in draft.misconception_candidates for term in item.concept_terms}
        | {term for item in draft.misconception_candidates for term in item.correction_terms},
        key=normalize_text,
    )
    for term in all_terms:
        mention = mentions.get(normalize_text(term))
        evidence = _ordered_unique(mention.evidence_message_ids if mention else ())
        result = resolve_alias(term, bundle.concepts)
        rid = _id("resolution", normalize_text(term), evidence)
        if result.status in {"resolved", "redirected"} and result.resolved_concept_id:
            target = result.resolved_concept_id
            seen: set[str] = set()
            redirected = result.status == "redirected"
            while target in concepts and concepts[target].status == "merged":
                if target in seen:
                    issues.append(
                        ProposalIssue(
                            code="REDIRECT_CYCLE",
                            severity="error",
                            field="concept_resolutions",
                            message="concept redirect cycle",
                            candidate_id=rid,
                            target_ids=tuple(sorted(seen | {target})),
                        )
                    )
                    target = ""
                    break
                seen.add(target)
                target = concepts[target].merged_into or ""
                redirected = True
            if not target or target not in concepts or concepts[target].status != "active":
                issues.append(
                    ProposalIssue(
                        code="INVALID_REDIRECT_TARGET",
                        severity="error",
                        field="concept_resolutions",
                        message="redirect target is missing or inactive",
                        candidate_id=rid,
                    )
                )
                resolutions.append(
                    ConceptResolutionProposal(
                        resolution_id=rid,
                        surface_text=term,
                        status="review_required",
                        evidence_message_ids=evidence,
                    )
                )
                by_term[normalize_text(term)] = None
            else:
                status = "redirected" if redirected else "matched"
                resolutions.append(
                    ConceptResolutionProposal(
                        resolution_id=rid,
                        surface_text=term,
                        status=status,
                        matched_concept_id=target,
                        evidence_message_ids=evidence,
                    )
                )
                by_term[normalize_text(term)] = ProposalConceptRef(concept_id=target)
        elif result.status == "ambiguous":
            resolutions.append(
                ConceptResolutionProposal(
                    resolution_id=rid,
                    surface_text=term,
                    status="ambiguous",
                    candidate_concept_ids=result.candidate_concept_ids,
                    evidence_message_ids=evidence,
                )
            )
            issues.append(
                ProposalIssue(
                    code="AMBIGUOUS_CONCEPT",
                    severity="review",
                    field="concept_resolutions",
                    message="concept term has multiple active matches",
                    candidate_id=rid,
                    target_ids=result.candidate_concept_ids,
                )
            )
            by_term[normalize_text(term)] = None
        elif result.status == "review_required":
            resolutions.append(
                ConceptResolutionProposal(
                    resolution_id=rid,
                    surface_text=term,
                    status="review_required",
                    candidate_concept_ids=result.candidate_concept_ids,
                    evidence_message_ids=evidence,
                )
            )
            issues.append(
                ProposalIssue(
                    code="CONCEPT_REVIEW_REQUIRED",
                    severity="review",
                    field="concept_resolutions",
                    message="concept lifecycle requires review",
                    candidate_id=rid,
                    target_ids=result.candidate_concept_ids,
                )
            )
            by_term[normalize_text(term)] = None
        elif (
            mention
            and mention.suggested_canonical_name
            and mention.suggested_concept_type
            and mention.suggested_scope_note
        ):
            aliases = (
                (ConceptAlias(text=mention.surface_text, language="zh-CN", alias_type="other"),)
                if normalize_text(mention.surface_text)
                != normalize_text(mention.suggested_canonical_name)
                else ()
            )
            cid = _id(
                "candidate_concept",
                mention.suggested_canonical_name,
                mention.suggested_concept_type,
                mention.suggested_scope_note,
                aliases,
                evidence,
            )
            new_candidates.append(
                NewConceptCandidate(
                    candidate_id=cid,
                    canonical_name=mention.suggested_canonical_name,
                    preferred_english=mention.suggested_preferred_english,
                    concept_type=mention.suggested_concept_type,
                    scope_note=mention.suggested_scope_note,
                    aliases=aliases,
                    evidence_message_ids=evidence,
                )
            )
            resolutions.append(
                ConceptResolutionProposal(
                    resolution_id=rid,
                    surface_text=term,
                    status="new_candidate",
                    new_candidate_id=cid,
                    evidence_message_ids=evidence,
                )
            )
            by_term[normalize_text(term)] = ProposalConceptRef(candidate_id=cid)
        else:
            resolutions.append(
                ConceptResolutionProposal(
                    resolution_id=rid,
                    surface_text=term,
                    status="review_required",
                    evidence_message_ids=evidence,
                )
            )
            issues.append(
                ProposalIssue(
                    code="UNKNOWN_CONCEPT",
                    severity="review",
                    field="concept_resolutions",
                    message="unknown concept lacks complete candidate metadata",
                    candidate_id=rid,
                )
            )
            by_term[normalize_text(term)] = None

    def refs(terms: tuple[str, ...], field: str) -> tuple[ProposalConceptRef, ...]:
        values = [by_term.get(normalize_text(term)) for term in terms]
        if any(item is None for item in values):
            issues.append(
                ProposalIssue(
                    code="UNRESOLVED_CONCEPT_REFERENCE",
                    severity="review",
                    field=field,
                    message="candidate references an unresolved concept",
                )
            )
        return tuple(
            sorted(
                {item.model_dump_json(): item for item in values if item is not None}.values(),
                key=lambda item: item.model_dump_json(),
            )
        )

    claim_proposals: list[ClaimProposal] = []
    observations: list[LearningObservationCandidate] = []
    learner_evidence: list[LearnerEvidence] = []
    misconception_observations: list[MisconceptionObservation] = []
    open_questions: list[OpenQuestion] = []
    messages = {item.message_id: item for item in draft.evidence_messages}

    def observed_at(message_ids: tuple[str, ...]) -> AwareDatetime:
        return max(messages[mid].observed_at for mid in message_ids)

    for item in draft.claim_candidates:
        item_refs = refs(item.concept_terms, "claim_candidates.concept_terms")
        role = messages[item.evidence_message_ids[0]].role
        if role == "user":
            kind: Literal["uncertain", "question"] = (
                "question" if item.claim_type == "question" else "uncertain"
            )
            observations.append(
                LearningObservationCandidate(
                    candidate_id=_id(
                        "candidate_observation", item.statement, item.evidence_message_ids
                    ),
                    observation_type=kind,
                    concept_refs=item_refs,
                    evidence_message_ids=_ordered_unique(item.evidence_message_ids),
                    observed_text=item.statement,
                )
            )
            if item.claim_type == "question":
                open_questions.append(
                    OpenQuestion(
                        question_id=_id(
                            "question",
                            draft.context.session_id,
                            item.statement,
                            item.evidence_message_ids,
                        ),
                        text=item.statement,
                        concept_ids=tuple(ref.concept_id for ref in item_refs if ref.concept_id),
                        discipline_id=draft.context.discipline_id,
                        priority=item.question_priority or "medium",
                    )
                )
            continue
        existing_ids = tuple(ref.concept_id for ref in item_refs if ref.concept_id)
        matching = (
            tuple(
                sorted(
                    claim.claim_id
                    for claim in bundle.claims
                    if claim.claim_status == "active"
                    and _statement(claim.statement) == _statement(item.statement)
                    and set(claim.concept_ids) == set(existing_ids)
                )
            )
            if len(existing_ids) == len(item_refs)
            else ()
        )
        claim_proposals.append(
            ClaimProposal(
                candidate_id=_id(
                    "candidate_claim",
                    item.statement,
                    item.claim_type,
                    item_refs,
                    item.evidence_message_ids,
                ),
                statement=item.statement,
                claim_type=item.claim_type,
                concept_refs=item_refs,
                evidence_message_ids=_ordered_unique(item.evidence_message_ids),
                matching_existing_claim_ids=matching,
            )
        )

    evidence_observation_type: dict[
        str, Literal["correct_recall", "incorrect_recall", "uncertain"]
    ] = {
        "correct_independent": "correct_recall",
        "correct_after_hint": "correct_recall",
        "guessed_correct": "correct_recall",
        "partial": "uncertain",
        "unknown": "uncertain",
        "incorrect": "incorrect_recall",
        "high_confidence_incorrect": "incorrect_recall",
        "self_report_only": "uncertain",
    }
    for learner_item in draft.learner_evidence_candidates:
        item_refs = refs(learner_item.concept_terms, "learner_evidence_candidates.concept_terms")
        concept_ids = tuple(ref.concept_id for ref in item_refs if ref.concept_id)
        if len(concept_ids) != 1 or len(item_refs) != 1:
            issues.append(
                ProposalIssue(
                    code="INVALID_LEARNER_EVIDENCE_CONCEPT",
                    severity="review",
                    field="learner_evidence_candidates.concept_terms",
                    message="learner evidence requires exactly one resolved persistent concept",
                )
            )
            continue
        evidence_id = _id(
            "evidence",
            draft.context.session_id,
            learner_item.evidence_type,
            learner_item.evidence_message_ids,
        )
        learner_evidence.append(
            LearnerEvidence(
                evidence_id=evidence_id,
                concept_id=concept_ids[0],
                evidence_type=learner_item.evidence_type,
                confidence=learner_item.confidence,
                rationale=learner_item.rationale,
                message_id=learner_item.evidence_message_ids[-1],
                observed_at=observed_at(learner_item.evidence_message_ids),
            )
        )
        observations.append(
            LearningObservationCandidate(
                candidate_id=_id("candidate_observation", evidence_id),
                observation_type=evidence_observation_type[learner_item.evidence_type],
                concept_refs=item_refs,
                evidence_message_ids=_ordered_unique(learner_item.evidence_message_ids),
                observed_text=learner_item.rationale,
            )
        )

    for misconception in draft.misconception_candidates:
        item_refs = refs(misconception.concept_terms, "misconception_candidates.concept_terms")
        existing_ids = tuple(ref.concept_id for ref in item_refs if ref.concept_id)
        correction_refs = refs(
            misconception.correction_terms, "misconception_candidates.correction_terms"
        )
        correction_ids = tuple(ref.concept_id for ref in correction_refs if ref.concept_id)
        corrections = tuple(
            sorted(
                claim.claim_id
                for claim in bundle.claims
                if claim.claim_status == "active"
                and claim.verification_status in {"source_backed", "verified_reference"}
                and claim.evidence_state == "supported"
                and misconception.proposed_correction is not None
                and _statement(claim.statement) == _statement(misconception.proposed_correction)
                and len(correction_ids) == len(correction_refs)
                and set(claim.concept_ids) == set(correction_ids)
            )
        )
        if misconception.proposed_correction and not corrections:
            issues.append(
                ProposalIssue(
                    code="UNVERIFIED_CORRECTION",
                    severity="warning",
                    field="misconception_candidates.proposed_correction",
                    message="proposed correction does not match an active supported claim",
                )
            )
        observations.append(
            LearningObservationCandidate(
                candidate_id=_id(
                    "candidate_observation",
                    misconception.observed_error_logic,
                    misconception.evidence_message_ids,
                ),
                observation_type="misconception",
                concept_refs=item_refs,
                evidence_message_ids=_ordered_unique(misconception.evidence_message_ids),
                observed_text=misconception.observed_error_logic,
                proposed_correction=misconception.proposed_correction,
                correction_claim_ids=corrections,
            )
        )
        misconception_observations.append(
            MisconceptionObservation(
                observation_id=_id(
                    "observation",
                    draft.context.session_id,
                    misconception.observed_error_logic,
                    misconception.evidence_message_ids,
                ),
                concept_ids=existing_ids,
                discipline_ids=(draft.context.discipline_id,),
                observed_error_logic=misconception.observed_error_logic,
                proposed_correction=misconception.proposed_correction,
                correction_claim_ids=corrections,
                severity=misconception.severity,
                evidence_message_ids=_ordered_unique(misconception.evidence_message_ids),
                observed_at=observed_at(misconception.evidence_message_ids),
            )
        )

    issues = sorted(
        issues, key=lambda item: (item.severity, item.code, item.field, item.candidate_id or "")
    )
    status = (
        "blocked"
        if any(item.severity in {"error", "review"} for item in issues)
        else "ready_for_review"
    )
    draft_hash = capture_draft_digest(draft)
    bundle_hash = contract_bundle_digest(bundle)
    proposal_id = _id("proposal", WORKFLOW_VERSION, draft_hash, bundle_hash)
    resolution_by_term = {normalize_text(item.surface_text): item for item in resolutions}
    capture_mentions: list[ConceptMention] = []
    for mention in draft.concept_mentions:
        resolution = resolution_by_term[normalize_text(mention.surface_text)]
        candidate_ids: tuple[ConceptId, ...]
        if resolution.status in {"matched", "redirected"} and resolution.matched_concept_id:
            mention_status: Literal["resolved", "ambiguous", "new_candidate", "rejected"] = (
                "resolved"
            )
            candidate_ids = (resolution.matched_concept_id,)
            resolved_id = resolution.matched_concept_id
            confidence = 1.0
        elif resolution.status == "ambiguous":
            mention_status = "ambiguous"
            candidate_ids = resolution.candidate_concept_ids
            resolved_id = None
            confidence = 0.0
        elif resolution.status == "new_candidate":
            mention_status = "new_candidate"
            candidate_ids = ()
            resolved_id = None
            confidence = 0.0
        else:
            mention_status = "rejected"
            candidate_ids = ()
            resolved_id = None
            confidence = 0.0
        capture_mentions.append(
            ConceptMention(
                surface_text=mention.surface_text,
                candidate_concept_ids=candidate_ids,
                resolved_concept_id=resolved_id,
                resolution_status=mention_status,
                confidence=confidence,
                message_ids=_ordered_unique(mention.evidence_message_ids),
            )
        )
    persistent_capture = LearningCapture(
        session_id=draft.context.session_id,
        source_id=draft.context.source_id,
        session_started_at=draft.context.session_started_at,
        captured_at=draft.context.captured_at,
        discipline_id=draft.context.discipline_id,
        course_id=draft.context.course_id,
        chapter_id=draft.context.chapter_id,
        concept_mentions=tuple(capture_mentions),
        learner_evidence=tuple(learner_evidence),
        misconception_observations=tuple(misconception_observations),
        open_questions=tuple(open_questions),
    )
    data = dict(
        proposal_id=proposal_id,
        status=status,
        draft_digest=draft_hash,
        base_bundle_digest=bundle_hash,
        proposal_digest="sha256:" + "0" * 64,
        context=draft.context,
        concept_resolutions=tuple(sorted(resolutions, key=lambda item: item.resolution_id)),
        new_concept_candidates=tuple(sorted(new_candidates, key=lambda item: item.candidate_id)),
        claim_proposals=tuple(sorted(claim_proposals, key=lambda item: item.candidate_id)),
        learning_capture_candidate=LearningCaptureCandidate(
            capture=persistent_capture,
            observations=tuple(observations),
        ),
        issues=tuple(issues),
    )
    proposal = CaptureProposal(**data)
    return CaptureProposal.model_validate(
        {**proposal.model_dump(), "proposal_digest": capture_proposal_digest(proposal)}
    )


def materialize_learning_capture(
    bundle: ContractBundle, proposal: CaptureProposal
) -> LearningCapture:
    """Validate and return the exact persistent capture carried by a review proposal."""
    if capture_proposal_digest(proposal) != proposal.proposal_digest:
        raise ValueError("PROPOSAL_DIGEST_MISMATCH")
    if contract_bundle_digest(bundle) != proposal.base_bundle_digest:
        raise ValueError("STALE_BASE_BUNDLE")
    if proposal.status == "blocked":
        raise ValueError("BLOCKED_PROPOSAL")
    if any(item.status not in {"matched", "redirected"} for item in proposal.concept_resolutions):
        raise ValueError("UNRESOLVED_CONCEPT")
    active_concepts = {item.concept_id for item in bundle.concepts if item.status == "active"}
    capture = proposal.learning_capture_candidate.capture
    referenced_concepts = {
        *(
            item.resolved_concept_id
            for item in capture.concept_mentions
            if item.resolved_concept_id
        ),
        *(item.concept_id for item in capture.learner_evidence),
        *(cid for item in capture.misconception_observations for cid in item.concept_ids),
        *(cid for item in capture.open_questions for cid in item.concept_ids),
    }
    if not referenced_concepts <= active_concepts:
        raise ValueError("INVALID_CAPTURE_CONCEPT")
    valid_corrections = {
        item.claim_id
        for item in bundle.claims
        if item.claim_status == "active"
        and item.verification_status in {"source_backed", "verified_reference"}
        and item.evidence_state == "supported"
    }
    correction_claim_ids = {
        cid for item in capture.misconception_observations for cid in item.correction_claim_ids
    }
    if not correction_claim_ids <= valid_corrections:
        raise ValueError("INVALID_CORRECTION_CLAIM")
    return LearningCapture.model_validate(capture.model_dump())


def render_capture_proposal_markdown(proposal: CaptureProposal, *, bundle: ContractBundle) -> str:
    concepts = {item.concept_id: item for item in bundle.concepts}

    def label(concept_id: str) -> str:
        concept = concepts.get(concept_id)
        return (
            format_concept_label(
                concept,
                surface_text=next(iter(english_abbreviations(concept)), None),
            )
            if concept
            else concept_id
        )

    matched = [
        f"- {item.surface_text} → {label(item.matched_concept_id)}"
        for item in proposal.concept_resolutions
        if item.matched_concept_id
    ]
    unresolved = [
        f"- {item.surface_text}: {item.status} "
        f"({', '.join(label(cid) for cid in item.candidate_concept_ids)})"
        for item in proposal.concept_resolutions
        if item.status in {"ambiguous", "review_required", "rejected"}
    ]
    new = [
        f"- {item.canonical_name} [{item.candidate_id}]" for item in proposal.new_concept_candidates
    ]
    claims = [
        f"- {item.statement}（unverified_chat / unassessed）" for item in proposal.claim_proposals
    ]
    observations = [
        f"- {item.observation_type}: {item.observed_text}"
        for item in proposal.learning_capture_candidate.observations
    ]
    errors = [
        f"- {item.observed_text}"
        for item in proposal.learning_capture_candidate.observations
        if item.observation_type in {"incorrect_recall", "misconception"}
    ]
    corrections = [
        f"- {item.proposed_correction or '无'}；"
        f"权威 claim: {', '.join(item.correction_claim_ids) or '无'}"
        for item in proposal.learning_capture_candidate.observations
        if item.observation_type in {"incorrect_recall", "misconception"}
    ]
    issue_lines = [
        f"- [{item.severity.upper()}] {item.code} · {item.field}: {item.message}"
        for item in proposal.issues
    ]
    blocked = (
        "⛔ BLOCKED（必须消歧或修正后才能继续）"
        if proposal.status == "blocked"
        else "✅ READY_FOR_REVIEW"
    )
    sections = [
        "# 学习记录写入提案",
        "## 状态\n" + blocked,
        "## 提案身份\n"
        + "\n".join(
            (
                f"- Proposal ID: `{proposal.proposal_id}`",
                f"- Draft digest: `{proposal.draft_digest}`",
                f"- Base bundle digest: `{proposal.base_bundle_digest}`",
                f"- Proposal digest: `{proposal.proposal_digest}`",
            )
        ),
        "## 已匹配概念\n" + "\n".join(matched or ["- 无"]),
        "## 歧义或待确认概念\n" + "\n".join(unresolved or ["- 无"]),
        "## 新概念候选\n" + "\n".join(new or ["- 无"]),
        "## 医学陈述候选\n" + "\n".join(claims or ["- 无"]),
        "## 已识别的学习表现\n" + "\n".join(observations or ["- 无"]),
        "## 已识别的错误逻辑\n" + "\n".join(errors or ["- 无"]),
        "## 建议纠正与证据状态\n" + "\n".join(corrections or ["- 无"]),
        "## 拟生成的学习记录\n"
        + "\n".join(
            (
                f"- source_id: `{proposal.learning_capture_candidate.capture.source_id}`",
                f"- session_id: `{proposal.learning_capture_candidate.capture.session_id}`",
                f"- observations: {len(proposal.learning_capture_candidate.observations)}",
            )
        ),
        "## 阻断项与警告\n" + "\n".join(issue_lines or ["- 无"]),
    ]
    return "\n\n".join(sections) + "\n"
