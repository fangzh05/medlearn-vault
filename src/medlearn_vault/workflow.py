"""Idempotent control-plane orchestration for proposal generation."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal, Protocol

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from pydantic import ConfigDict, Field, model_validator

from medlearn_vault.bundle import ContractBundle
from medlearn_vault.capture import (
    CaptureDraft,
    CaptureProposal,
    IntakeDigestMismatch,
    IntakeEnvelope,
    InvalidIntakeEnvelope,
    build_capture_proposal,
    capture_proposal_digest,
    contract_bundle_digest,
    exact_capture_proposal_json,
    extract_capture_draft,
    materialize_learning_capture,
    render_capture_proposal_markdown,
)
from medlearn_vault.domain.base import AwareDatetime, DomainModel

if TYPE_CHECKING:
    from medlearn_vault.vault_writer import VaultObjectStore

CONTROL_BUCKET = "medlearn-control"
LEASE_DURATION = timedelta(minutes=10)
TERMINAL_JOB_RETRIES = 3
JOB_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"
DIGEST_PATTERN = r"^sha256:[a-f0-9]{64}$"
PROPOSAL_ID_PATTERN = r"^proposal_[a-f0-9]{32}$"
APPROVAL_ID_PATTERN = r"^approval_[a-f0-9]{32}$"
INTAKE_KEY_PATTERN = r"^v1/intakes/sha256/[a-f0-9]{64}\.json$"
BUNDLE_FILES = (
    "sources.json",
    "concepts.json",
    "claims.json",
    "relations.json",
    "discipline_lenses.json",
    "chapters.json",
    "learning_capture.json",
)


class WorkflowError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class StoredObject(DomainModel):
    body: bytes
    etag: str


class ObjectStore(Protocol):
    def get(self, key: str) -> StoredObject | None: ...

    def create(self, key: str, body: bytes, *, content_type: str) -> bool: ...

    def compare_and_swap(
        self, key: str, body: bytes, etag: str, *, content_type: str
    ) -> bool: ...


class ReadOnlyObjectStore(Protocol):
    """Minimal control-plane boundary for verification-only operations."""

    def get(self, key: str) -> StoredObject | None: ...


class WorkflowInputs(DomainModel):
    job_id: str = Field(pattern=JOB_ID_PATTERN)
    intake_object_key: str = Field(pattern=INTAKE_KEY_PATTERN)
    intake_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def key_matches_digest(self) -> WorkflowInputs:
        if self.intake_object_key != f"v1/intakes/sha256/{self.intake_digest[7:]}.json":
            raise ValueError("intake key must match digest")
        return self


JobStatus = Literal[
    "received", "dispatched", "running", "succeeded", "blocked", "failed", "expired"
]


class JobRecord(DomainModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "allOf": [
                {
                    "if": {"properties": {"status": {"enum": ["succeeded", "blocked"]}}},
                    "then": {"required": ["proposal_id", "workflow_run_id"]},
                },
                {
                    "if": {"properties": {"status": {"const": "failed"}}},
                    "then": {"required": ["error_code"]},
                },
                {
                    "if": {
                        "properties": {
                            "status": {
                                "enum": ["succeeded", "blocked", "failed", "expired"]
                            }
                        }
                    },
                    "then": {
                        "properties": {
                            "dispatch_lease_id": {"type": "null"},
                            "dispatch_lease_expires_at": {"type": "null"},
                        }
                    },
                },
            ]
        },
    )

    job_version: Literal["0.2.0"] = "0.2.0"
    job_id: str = Field(pattern=JOB_ID_PATTERN)
    status: JobStatus
    intake_digest: str = Field(pattern=DIGEST_PATTERN)
    intake_object_key: str = Field(pattern=INTAKE_KEY_PATTERN)
    proposal_id: str | None = Field(default=None, pattern=r"^proposal_[a-f0-9]{32}$")
    workflow_run_id: str | None = Field(default=None, pattern=JOB_ID_PATTERN)
    dispatch_attempt: int = Field(ge=0)
    dispatch_lease_id: str | None = Field(default=None, pattern=JOB_ID_PATTERN)
    dispatch_lease_expires_at: AwareDatetime | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    error_code: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,127}$")
    reproposal_of_job_id: str | None = Field(default=None, pattern=JOB_ID_PATTERN)
    reproposal_of_proposal_id: str | None = Field(default=None, pattern=PROPOSAL_ID_PATTERN)
    catalog_update_id: str | None = Field(
        default=None, pattern=r"^catalog_update_[a-f0-9]{32}$"
    )

    @model_validator(mode="after")
    def validate_status_fields(self) -> JobRecord:
        if self.status in {"succeeded", "blocked"} and (
            self.proposal_id is None or self.workflow_run_id is None
        ):
            raise ValueError("successful terminal jobs require proposal and workflow run IDs")
        if self.status == "failed" and self.error_code is None:
            raise ValueError("failed jobs require error_code")
        if self.status in {"succeeded", "blocked", "failed", "expired"} and (
            self.dispatch_lease_id is not None or self.dispatch_lease_expires_at is not None
        ):
            raise ValueError("terminal jobs cannot retain dispatch leases")
        return self


ExecutionStatus = Literal["running", "succeeded", "blocked", "failed"]


class ProposalExecutionRecord(DomainModel):
    execution_version: Literal["0.1.0"] = "0.1.0"
    job_id: str = Field(pattern=JOB_ID_PATTERN)
    status: ExecutionStatus
    lease_owner: str | None = Field(default=None, pattern=JOB_ID_PATTERN)
    lease_expires_at: AwareDatetime | None = None
    proposal_id: str | None = Field(default=None, pattern=r"^proposal_[a-f0-9]{32}$")
    proposal_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    review_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    workflow_run_id: str | None = Field(default=None, pattern=JOB_ID_PATTERN)
    error_code: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,127}$")
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def validate_state(self) -> ProposalExecutionRecord:
        terminal = self.status in {"succeeded", "blocked", "failed"}
        if terminal and (self.lease_owner is not None or self.lease_expires_at is not None):
            raise ValueError("terminal executions cannot retain leases")
        if self.status in {"succeeded", "blocked"} and any(
            value is None
            for value in (
                self.proposal_id,
                self.proposal_digest,
                self.review_digest,
                self.workflow_run_id,
            )
        ):
            raise ValueError("successful executions require output identities")
        if self.status == "failed" and self.error_code is None:
            raise ValueError("failed executions require error_code")
        if self.status == "running" and (
            self.lease_owner is None or self.lease_expires_at is None
        ):
            raise ValueError("running executions require a lease")
        return self


ApprovalDecision = Literal["approved", "rejected"]


class ProposalApprovalRecord(DomainModel):
    """Immutable control-plane decision bound to one exact proposal and base."""

    approval_version: Literal["0.1.0"] = "0.1.0"
    approval_id: str = Field(pattern=APPROVAL_ID_PATTERN)
    proposal_id: str = Field(pattern=PROPOSAL_ID_PATTERN)
    proposal_object_digest: str = Field(pattern=DIGEST_PATTERN)
    expected_base_bundle_digest: str = Field(pattern=DIGEST_PATTERN)
    decision: ApprovalDecision
    decided_at: AwareDatetime
    rejection_code: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,127}$")

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "allOf": [
                {
                    "if": {"properties": {"decision": {"const": "approved"}}},
                    "then": {"properties": {"rejection_code": {"type": "null"}}},
                },
                {
                    "if": {"properties": {"decision": {"const": "rejected"}}},
                    "then": {
                        "required": ["rejection_code"],
                        "properties": {
                            "rejection_code": {
                                "type": "string",
                                "pattern": r"^[A-Z][A-Z0-9_]{0,127}$",
                            }
                        },
                    },
                },
            ]
        },
    )

    @model_validator(mode="after")
    def validate_decision_fields(self) -> ProposalApprovalRecord:
        if self.decision == "approved" and self.rejection_code is not None:
            raise ValueError("approved decisions cannot have a rejection_code")
        if self.decision == "rejected" and self.rejection_code is None:
            raise ValueError("rejected decisions require a rejection_code")
        if self.approval_id != approval_identity(
            self.proposal_id,
            self.proposal_object_digest,
            self.expected_base_bundle_digest,
        ):
            raise ValueError("approval_id does not match bound proposal subject")
        return self


class ApprovalResult(DomainModel):
    approval_id: str = Field(pattern=APPROVAL_ID_PATTERN)
    decision: ApprovalDecision
    reused: bool = False


class ApprovalAttestationResult(DomainModel):
    """Sanitized, non-persistent proof that existing control outputs agree."""

    approval_id: str = Field(pattern=APPROVAL_ID_PATTERN)
    approval_object_digest: str = Field(pattern=DIGEST_PATTERN)
    proposal_id: str = Field(pattern=PROPOSAL_ID_PATTERN)
    proposal_object_digest: str = Field(pattern=DIGEST_PATTERN)
    proposal_semantic_digest: str = Field(pattern=DIGEST_PATTERN)
    source_job_id: str = Field(pattern=JOB_ID_PATTERN)
    workflow_run_id: str = Field(pattern=JOB_ID_PATTERN)
    review_digest: str = Field(pattern=DIGEST_PATTERN)
    decision: ApprovalDecision
    verified: Literal[True] = True


class PublicationPlanResult(DomainModel):
    """Sanitized result of a create-only publication-plan operation."""

    publication_plan_id: str = Field(pattern=r"^publication_plan_[a-f0-9]{32}$")
    publication_plan_object_digest: str = Field(pattern=DIGEST_PATTERN)
    capture_id: str = Field(pattern=r"^capture_[a-f0-9]{32}$")
    capture_object_digest: str = Field(pattern=DIGEST_PATTERN)
    markdown_digest: str = Field(pattern=DIGEST_PATTERN)
    reused: bool = False


class ProposalInspectionResult(DomainModel):
    """Sanitized, non-persistent identities for one completed Proposal job."""

    source_job_id: str = Field(pattern=JOB_ID_PATTERN)
    proposal_id: str = Field(pattern=PROPOSAL_ID_PATTERN)
    proposal_object_digest: str = Field(pattern=DIGEST_PATTERN)
    proposal_semantic_digest: str = Field(pattern=DIGEST_PATTERN)
    expected_base_bundle_digest: str = Field(pattern=DIGEST_PATTERN)
    review_digest: str = Field(pattern=DIGEST_PATTERN)
    workflow_run_id: str = Field(pattern=JOB_ID_PATTERN)
    verified: Literal[True] = True


class AutoPublicationResult(DomainModel):
    """Sanitized result of the source-job-only automatic publication path."""

    status: Literal["published", "manual_review_required"]
    source_job_id: str = Field(pattern=JOB_ID_PATTERN)
    proposal_id: str | None = Field(default=None, pattern=PROPOSAL_ID_PATTERN)
    proposal_status: str | None = None
    approval_id: str | None = Field(default=None, pattern=APPROVAL_ID_PATTERN)
    approval_object_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    publication_plan_id: str | None = Field(
        default=None, pattern=r"^publication_plan_[a-f0-9]{32}$"
    )
    publication_plan_object_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    capture_id: str | None = Field(default=None, pattern=r"^capture_[a-f0-9]{32}$")
    created_count: int = Field(default=0, ge=0)
    reused_count: int = Field(default=0, ge=0)
    receipt_status: str | None = None
    manual_review_reason: str | None = None


class ReproposalResult(DomainModel):
    """Sanitized result of an explicit catalog reproposal operation."""

    source_job_id: str = Field(pattern=JOB_ID_PATTERN)
    proposal_id: str = Field(pattern=PROPOSAL_ID_PATTERN)
    base_bundle_digest: str = Field(pattern=DIGEST_PATTERN)
    reused: bool = False


CATALOG_UPDATE_ID_PATTERN = r"^catalog_update_[a-f0-9]{32}$"


class OrchestrationResult(DomainModel):
    status: Literal["succeeded", "blocked", "lease_held"]
    proposal_id: str | None = None
    reused: bool = False


def _json_bytes(value: DomainModel) -> bytes:
    return (
        json.dumps(value.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
    ).encode()


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _canonical_json_bytes(value: DomainModel | dict[str, Any] | tuple[Any, ...]) -> bytes:
    if isinstance(value, DomainModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _id(prefix: str, *parts: Any) -> str:
    """Deterministic content-addressed identity from the canonical JSON of parts."""

    def _serialize(value: Any) -> Any:
        if isinstance(value, DomainModel):
            return _serialize(value.model_dump(mode="json"))
        if isinstance(value, dict):
            return {key: _serialize(val) for key, val in sorted(value.items())}
        if isinstance(value, (list, tuple)):
            return [_serialize(item) for item in value]
        return value

    payload = json.dumps(
        _serialize(parts), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(payload).hexdigest()[:32]}"


def approval_identity(
    proposal_id: str,
    proposal_object_digest: str,
    expected_base_bundle_digest: str,
) -> str:
    bound = {
        "expected_base_bundle_digest": expected_base_bundle_digest,
        "proposal_id": proposal_id,
        "proposal_object_digest": proposal_object_digest,
    }
    return "approval_" + hashlib.sha256(_canonical_json_bytes(bound)).hexdigest()[:32]


def canonical_approval_json(record: ProposalApprovalRecord) -> bytes:
    return _canonical_json_bytes(record) + b"\n"


class ApprovalOrchestrator:
    """Validate a stored proposal and create one immutable approval decision."""

    def __init__(self, store: ObjectStore) -> None:
        self.store = store

    def run(
        self,
        proposal_id: str,
        proposal_object_digest: str,
        expected_base_bundle_digest: str,
        *,
        decision: ApprovalDecision = "approved",
        rejection_code: str | None = None,
        now: datetime | None = None,
    ) -> ApprovalResult:
        decided_at = now or datetime.now(UTC)
        valid_input = (
            re.fullmatch(PROPOSAL_ID_PATTERN, proposal_id) is not None
            and re.fullmatch(DIGEST_PATTERN, proposal_object_digest) is not None
            and re.fullmatch(DIGEST_PATTERN, expected_base_bundle_digest) is not None
            and decision in {"approved", "rejected"}
            and (
                rejection_code is None
                or re.fullmatch(r"^[A-Z][A-Z0-9_]{0,127}$", rejection_code) is not None
            )
            and not (decision == "approved" and rejection_code is not None)
            and not (decision == "rejected" and rejection_code is None)
        )
        if decided_at.tzinfo is None or not valid_input:
            raise WorkflowError("INVALID_APPROVAL_INPUT")
        approval_id = approval_identity(
            proposal_id, proposal_object_digest, expected_base_bundle_digest
        )
        try:
            proposal_stored = self.store.get(f"v1/proposals/{proposal_id}.json")
        except WorkflowError:
            raise
        except Exception as exc:
            raise WorkflowError("CONTROL_STORE_FAILURE") from exc
        if proposal_stored is None:
            raise WorkflowError("PROPOSAL_NOT_FOUND")
        if _digest(proposal_stored.body) != proposal_object_digest:
            raise WorkflowError("PROPOSAL_OBJECT_DIGEST_MISMATCH")
        try:
            proposal = CaptureProposal.model_validate_json(proposal_stored.body)
        except (ValueError, TypeError) as exc:
            raise WorkflowError("INVALID_PROPOSAL") from exc
        if proposal.proposal_id != proposal_id:
            raise WorkflowError("INVALID_PROPOSAL")
        if capture_proposal_digest(proposal) != proposal.proposal_digest:
            raise WorkflowError("INVALID_PROPOSAL")
        if proposal.base_bundle_digest != expected_base_bundle_digest:
            raise WorkflowError("BASE_BUNDLE_DIGEST_MISMATCH")
        if proposal.status == "blocked":
            raise WorkflowError("PROPOSAL_BLOCKED")

        record = ProposalApprovalRecord(
            approval_id=approval_id,
            proposal_id=proposal_id,
            proposal_object_digest=proposal_object_digest,
            expected_base_bundle_digest=expected_base_bundle_digest,
            decision=decision,
            decided_at=decided_at,
            rejection_code=rejection_code if decision == "rejected" else None,
        )
        approval_key = f"v1/approvals/{approval_id}.json"
        body = canonical_approval_json(record)
        try:
            if self.store.create(approval_key, body, content_type="application/json"):
                return ApprovalResult(approval_id=approval_id, decision=decision)
            winner = self.store.get(approval_key)
        except WorkflowError:
            raise
        except Exception as exc:
            raise WorkflowError("CONTROL_STORE_FAILURE") from exc
        if winner is None:
            raise WorkflowError("CONTROL_STORE_FAILURE")
        try:
            existing = ProposalApprovalRecord.model_validate_json(winner.body)
        except (ValueError, TypeError) as exc:
            raise WorkflowError("APPROVAL_CONFLICT") from exc
        same_request = (
            existing.approval_id == approval_id
            and existing.proposal_id == proposal_id
            and existing.proposal_object_digest == proposal_object_digest
            and existing.expected_base_bundle_digest == expected_base_bundle_digest
            and existing.decision == decision
            and existing.rejection_code == (rejection_code if decision == "rejected" else None)
            and canonical_approval_json(existing) == winner.body
        )
        if not same_request:
            raise WorkflowError("APPROVAL_CONFLICT")
        return ApprovalResult(approval_id=approval_id, decision=decision, reused=True)


class ApprovalAttestor:
    """Read and verify one immutable approval and its proposal provenance."""

    def __init__(self, store: ReadOnlyObjectStore) -> None:
        self.store = store

    def run(
        self,
        approval_id: str,
        source_job_id: str,
        proposal_id: str,
        expected_proposal_object_digest: str,
        expected_base_bundle_digest: str,
        *,
        expected_decision: str,
        expected_rejection_code: str | None = None,
        expected_approval_object_digest: str | None = None,
    ) -> ApprovalAttestationResult:
        valid_input = (
            re.fullmatch(APPROVAL_ID_PATTERN, approval_id) is not None
            and re.fullmatch(JOB_ID_PATTERN, source_job_id) is not None
            and re.fullmatch(PROPOSAL_ID_PATTERN, proposal_id) is not None
            and re.fullmatch(DIGEST_PATTERN, expected_proposal_object_digest) is not None
            and re.fullmatch(DIGEST_PATTERN, expected_base_bundle_digest) is not None
            and expected_decision in {"approved", "rejected"}
            and (
                expected_approval_object_digest is None
                or re.fullmatch(DIGEST_PATTERN, expected_approval_object_digest) is not None
            )
            and (
                expected_rejection_code is None
                or re.fullmatch(r"^[A-Z][A-Z0-9_]{0,127}$", expected_rejection_code)
                is not None
            )
            and not (expected_decision == "approved" and expected_rejection_code is not None)
            and not (expected_decision == "rejected" and expected_rejection_code is None)
        )
        if not valid_input:
            raise WorkflowError("INVALID_ATTESTATION_INPUT")

        approval_stored = self._read(
            f"v1/approvals/{approval_id}.json", "APPROVAL_NOT_FOUND"
        )
        try:
            approval = ProposalApprovalRecord.model_validate_json(approval_stored.body)
        except (ValueError, TypeError) as exc:
            raise WorkflowError("INVALID_APPROVAL") from exc
        if canonical_approval_json(approval) != approval_stored.body:
            raise WorkflowError("INVALID_APPROVAL")
        approval_digest = _digest(approval_stored.body)
        if (
            expected_approval_object_digest is not None
            and approval_digest != expected_approval_object_digest
        ):
            raise WorkflowError("APPROVAL_OBJECT_DIGEST_MISMATCH")
        if (
            approval.approval_id != approval_id
            or approval.approval_id
            != approval_identity(
                approval.proposal_id,
                approval.proposal_object_digest,
                approval.expected_base_bundle_digest,
            )
            or approval.proposal_id != proposal_id
            or approval.proposal_object_digest != expected_proposal_object_digest
            or approval.expected_base_bundle_digest != expected_base_bundle_digest
            or approval.decision != expected_decision
            or approval.rejection_code != expected_rejection_code
        ):
            raise WorkflowError("APPROVAL_EXPECTATION_MISMATCH")

        proposal_stored = self._read(
            f"v1/proposals/{proposal_id}.json", "PROPOSAL_NOT_FOUND"
        )
        try:
            proposal = CaptureProposal.model_validate_json(proposal_stored.body)
        except (ValueError, TypeError) as exc:
            raise WorkflowError("INVALID_PROPOSAL") from exc
        if (
            proposal.proposal_id != proposal_id
            or capture_proposal_digest(proposal) != proposal.proposal_digest
            or proposal.status != "ready_for_review"
        ):
            raise WorkflowError("INVALID_PROPOSAL")
        if proposal.base_bundle_digest != expected_base_bundle_digest:
            raise WorkflowError("BASE_BUNDLE_DIGEST_MISMATCH")
        proposal_digest = _digest(proposal_stored.body)
        if proposal_digest != expected_proposal_object_digest:
            raise WorkflowError("PROPOSAL_OBJECT_DIGEST_MISMATCH")

        job_stored = self._read(f"v1/jobs/{source_job_id}.json", "JOB_NOT_FOUND")
        try:
            job = JobRecord.model_validate_json(job_stored.body)
        except (ValueError, TypeError) as exc:
            raise WorkflowError("INVALID_JOB") from exc
        if (
            job.job_id != source_job_id
            or job.intake_object_key != f"v1/intakes/sha256/{job.intake_digest[7:]}.json"
            or job.status != "succeeded"
            or job.workflow_run_id is None
        ):
            raise WorkflowError("INVALID_JOB")
        if job.proposal_id != proposal_id:
            raise WorkflowError("CONTROL_OUTPUT_MISMATCH")

        execution_stored = self._read(
            f"v1/executions/{source_job_id}.json", "EXECUTION_NOT_FOUND"
        )
        try:
            execution = ProposalExecutionRecord.model_validate_json(execution_stored.body)
        except (ValueError, TypeError) as exc:
            raise WorkflowError("INVALID_EXECUTION") from exc
        if (
            execution.job_id != source_job_id
            or execution.status != "succeeded"
            or execution.proposal_id is None
            or execution.proposal_digest is None
            or execution.review_digest is None
            or execution.workflow_run_id is None
        ):
            raise WorkflowError("INVALID_EXECUTION")
        if (
            execution.proposal_id != proposal_id
            or execution.proposal_digest != proposal_digest
            or execution.workflow_run_id != job.workflow_run_id
        ):
            raise WorkflowError("CONTROL_OUTPUT_MISMATCH")

        review_stored = self._read(f"v1/reviews/{proposal_id}.md", "REVIEW_NOT_FOUND")
        review_digest = _digest(review_stored.body)
        if review_digest != execution.review_digest:
            raise WorkflowError("REVIEW_DIGEST_MISMATCH")
        return ApprovalAttestationResult(
            approval_id=approval_id,
            approval_object_digest=approval_digest,
            proposal_id=proposal_id,
            proposal_object_digest=proposal_digest,
            proposal_semantic_digest=proposal.proposal_digest,
            source_job_id=source_job_id,
            workflow_run_id=job.workflow_run_id,
            review_digest=review_digest,
            decision=approval.decision,
        )

    def _read(self, key: str, missing_code: str) -> StoredObject:
        try:
            stored = self.store.get(key)
        except WorkflowError:
            raise
        except Exception as exc:
            raise WorkflowError("CONTROL_STORE_FAILURE") from exc
        if stored is None:
            raise WorkflowError(missing_code)
        return stored


_PUBLICATION_PASS_THROUGH_CODES = frozenset(
    {
        "PUBLICATION_NOT_APPROVED",
        "PROPOSAL_DIGEST_MISMATCH",
        "STALE_BASE_BUNDLE",
        "BLOCKED_PROPOSAL",
        "UNRESOLVED_CONCEPT",
        "INVALID_CAPTURE_CONCEPT",
        "INVALID_CORRECTION_CLAIM",
    }
)


class PublicationPlanOrchestrator:
    """Freshly attest an approved subject, then create exactly one control-plane plan."""

    def __init__(self, store: ObjectStore, repository_root: Path) -> None:
        self.store = store
        self.repository_root = repository_root.resolve()

    def run(
        self,
        approval_id: str,
        approval_object_digest: str,
        source_job_id: str,
        proposal_id: str,
        proposal_object_digest: str,
        expected_base_bundle_digest: str,
        *,
        bundle_path: str,
    ) -> PublicationPlanResult:
        from medlearn_vault.publication import (
            VaultPublicationPlan,
            build_vault_publication_plan,
            canonical_publication_plan_json,
            publication_plan_object_digest,
        )

        # Pre-read approval only to recover decision and rejection_code so
        # the attestor is always called regardless of outcome.
        try:
            approval_stored = self.store.get(f"v1/approvals/{approval_id}.json")
        except Exception as exc:
            raise WorkflowError("CONTROL_STORE_FAILURE") from exc
        if approval_stored is None:
            raise WorkflowError("APPROVAL_NOT_FOUND")
        try:
            approval_pre = ProposalApprovalRecord.model_validate_json(
                approval_stored.body
            )
        except (ValueError, TypeError) as exc:
            raise WorkflowError("INVALID_APPROVAL") from exc
        decision: str = approval_pre.decision
        rejection_code: str | None = (
            approval_pre.rejection_code if decision == "rejected" else None
        )

        # Always attest — digest, identity, proposal and bundle binding are
        # verified before we route on the decision.
        attestation = ApprovalAttestor(self.store).run(
            approval_id,
            source_job_id,
            proposal_id,
            proposal_object_digest,
            expected_base_bundle_digest,
            expected_decision=decision,
            expected_rejection_code=rejection_code,
            expected_approval_object_digest=approval_object_digest,
        )
        if attestation.decision == "rejected":
            raise WorkflowError("PUBLICATION_NOT_APPROVED")

        try:
            proposal = self.store.get(f"v1/proposals/{proposal_id}.json")
            if proposal is None:
                raise WorkflowError("CONTROL_STORE_FAILURE")
            bundle = ContractBundle.from_directory(
                resolve_bundle_path(self.repository_root, bundle_path)
            )
            plan = build_vault_publication_plan(
                bundle,
                proposal.body,
                approval_stored.body,
                attestation.review_digest,
            )
        except WorkflowError:
            raise
        except ValueError as exc:
            code = str(exc)
            if code in _PUBLICATION_PASS_THROUGH_CODES:
                raise WorkflowError(code) from exc
            raise WorkflowError("INVALID_PUBLICATION_INPUT") from exc
        except Exception as exc:
            raise WorkflowError("CONTROL_STORE_FAILURE") from exc
        body = canonical_publication_plan_json(plan)
        key = f"v1/publication-plans/{plan.publication_plan_id}.json"
        try:
            created = self.store.create(key, body, content_type="application/json")
            winner = None if created else self.store.get(key)
        except WorkflowError:
            raise
        except Exception as exc:
            raise WorkflowError("CONTROL_STORE_FAILURE") from exc
        if not created:
            if winner is None:
                raise WorkflowError("CONTROL_STORE_FAILURE")
            try:
                existing = VaultPublicationPlan.model_validate_json(winner.body)
            except ValueError as exc:
                raise WorkflowError("PUBLICATION_PLAN_CONFLICT") from exc
            if canonical_publication_plan_json(existing) != winner.body or winner.body != body:
                raise WorkflowError("PUBLICATION_PLAN_CONFLICT")
            plan = existing
        markdown = next(
            item for item in plan.artifacts if item.media_type == "text/markdown; charset=utf-8"
        )
        return PublicationPlanResult(
            publication_plan_id=plan.publication_plan_id,
            publication_plan_object_digest=publication_plan_object_digest(plan),
            capture_id=plan.capture_id,
            capture_object_digest=plan.capture_object_digest,
            markdown_digest=markdown.content_digest,
            reused=not created,
        )


class ProposalOutputInspector:
    """Read-only verification of one completed Proposal, Execution, and Review."""

    def __init__(self, store: ReadOnlyObjectStore) -> None:
        self.store = store

    def run(
        self, source_job_id: str, *, allow_blocked: bool = False
    ) -> ProposalInspectionResult:
        if re.fullmatch(JOB_ID_PATTERN, source_job_id) is None:
            raise WorkflowError("INVALID_ATTESTATION_INPUT")

        job_stored = self._read(f"v1/jobs/{source_job_id}.json", "JOB_NOT_FOUND")
        try:
            job = JobRecord.model_validate_json(job_stored.body)
        except (ValueError, TypeError) as exc:
            raise WorkflowError("INVALID_JOB") from exc
        allowed_terminal_statuses = {"succeeded", "blocked"} if allow_blocked else {"succeeded"}
        if (
            job.job_id != source_job_id
            or job.intake_object_key != f"v1/intakes/sha256/{job.intake_digest[7:]}.json"
            or job.status not in allowed_terminal_statuses
            or job.proposal_id is None
            or job.workflow_run_id is None
        ):
            raise WorkflowError("INVALID_JOB")

        execution_stored = self._read(
            f"v1/executions/{source_job_id}.json", "EXECUTION_NOT_FOUND"
        )
        try:
            execution = ProposalExecutionRecord.model_validate_json(execution_stored.body)
        except (ValueError, TypeError) as exc:
            raise WorkflowError("INVALID_EXECUTION") from exc
        if (
            execution.job_id != source_job_id
            or execution.status not in allowed_terminal_statuses
            or execution.proposal_id is None
            or execution.proposal_digest is None
            or execution.review_digest is None
            or execution.workflow_run_id is None
        ):
            raise WorkflowError("INVALID_EXECUTION")
        if (
            execution.proposal_id != job.proposal_id
            or execution.workflow_run_id != job.workflow_run_id
            or execution.status != job.status
        ):
            raise WorkflowError("CONTROL_OUTPUT_MISMATCH")

        proposal_stored = self._read(
            f"v1/proposals/{job.proposal_id}.json", "PROPOSAL_NOT_FOUND"
        )
        try:
            proposal = CaptureProposal.model_validate_json(proposal_stored.body)
        except (ValueError, TypeError) as exc:
            raise WorkflowError("INVALID_PROPOSAL") from exc
        proposal_object_digest = _digest(proposal_stored.body)
        if (
            proposal.proposal_id != job.proposal_id
            or capture_proposal_digest(proposal) != proposal.proposal_digest
            or proposal.status not in (
                {"ready_for_review", "blocked"} if allow_blocked else {"ready_for_review"}
            )
        ):
            raise WorkflowError("INVALID_PROPOSAL")
        if proposal_object_digest != execution.proposal_digest:
            raise WorkflowError("CONTROL_OUTPUT_MISMATCH")

        review_stored = self._read(
            f"v1/reviews/{job.proposal_id}.md", "REVIEW_NOT_FOUND"
        )
        review_digest = _digest(review_stored.body)
        if review_digest != execution.review_digest:
            raise WorkflowError("REVIEW_DIGEST_MISMATCH")
        return ProposalInspectionResult(
            source_job_id=source_job_id,
            proposal_id=job.proposal_id,
            proposal_object_digest=proposal_object_digest,
            proposal_semantic_digest=proposal.proposal_digest,
            expected_base_bundle_digest=proposal.base_bundle_digest,
            review_digest=review_digest,
            workflow_run_id=job.workflow_run_id,
        )

    def _read(self, key: str, missing_code: str) -> StoredObject:
        try:
            stored = self.store.get(key)
        except WorkflowError:
            raise
        except Exception as exc:
            raise WorkflowError("CONTROL_STORE_FAILURE") from exc
        if stored is None:
            raise WorkflowError(missing_code)
        return stored


AUTO_APPROVABLE_CLIENT_KINDS = frozenset({"chatgpt_work"})


class AutoPublicationOrchestrator:
    """Approve and publish one already-proposed Job without copied identities."""

    def __init__(
        self,
        control_store: ObjectStore,
        vault_store: VaultObjectStore,
        repository_root: Path,
    ) -> None:
        self.control_store = control_store
        self.vault_store = vault_store
        self.repository_root = repository_root.resolve()

    def run(self, source_job_id: str, *, bundle_path: str) -> AutoPublicationResult:
        if re.fullmatch(JOB_ID_PATTERN, source_job_id) is None:
            raise WorkflowError("INVALID_AUTO_PUBLICATION_INPUT")

        job_stored = self._read(f"v1/jobs/{source_job_id}.json", "JOB_NOT_FOUND")
        try:
            job = JobRecord.model_validate_json(job_stored.body)
        except (TypeError, ValueError) as exc:
            raise WorkflowError("INVALID_JOB") from exc
        if job.job_id != source_job_id or job.proposal_id is None:
            raise WorkflowError("INVALID_JOB")

        proposal_stored = self._read(
            f"v1/proposals/{job.proposal_id}.json", "PROPOSAL_NOT_FOUND"
        )
        try:
            proposal = CaptureProposal.model_validate_json(proposal_stored.body)
        except (TypeError, ValueError) as exc:
            raise WorkflowError("INVALID_PROPOSAL") from exc
        if (
            proposal.proposal_id != job.proposal_id
            or capture_proposal_digest(proposal) != proposal.proposal_digest
        ):
            raise WorkflowError("INVALID_PROPOSAL")

        # Verify Job, Execution, Proposal, and Review provenance before policy routing.
        inspected = ProposalOutputInspector(self.control_store).run(
            source_job_id, allow_blocked=True
        )
        reason = self._eligibility_reason(job, proposal)
        if reason is not None:
            return AutoPublicationResult(
                status="manual_review_required",
                source_job_id=source_job_id,
                proposal_id=proposal.proposal_id,
                proposal_status=proposal.status,
                manual_review_reason=reason,
            )

        intake_stored = self._read(job.intake_object_key, "INTAKE_NOT_FOUND")
        if _digest(intake_stored.body) != job.intake_digest:
            raise WorkflowError("INTAKE_DIGEST_MISMATCH")
        try:
            intake = IntakeEnvelope.model_validate_json(intake_stored.body)
        except (TypeError, ValueError) as exc:
            raise WorkflowError("INVALID_INTAKE_ENVELOPE") from exc
        if intake.client_kind not in AUTO_APPROVABLE_CLIENT_KINDS:
            return self._manual(inspected, proposal, "CLIENT_KIND_NOT_ALLOWED")

        bundle = ContractBundle.from_directory(
            resolve_bundle_path(self.repository_root, bundle_path)
        )
        if contract_bundle_digest(bundle) != inspected.expected_base_bundle_digest:
            raise WorkflowError("STALE_BASE_BUNDLE")
        try:
            materialize_learning_capture(bundle, proposal)
        except ValueError as exc:
            raise WorkflowError(str(exc)) from exc

        approval = ApprovalOrchestrator(self.control_store).run(
            inspected.proposal_id,
            inspected.proposal_object_digest,
            inspected.expected_base_bundle_digest,
            decision="approved",
        )
        attestation = ApprovalAttestor(self.control_store).run(
            approval.approval_id,
            source_job_id,
            inspected.proposal_id,
            inspected.proposal_object_digest,
            inspected.expected_base_bundle_digest,
            expected_decision="approved",
        )
        plan = PublicationPlanOrchestrator(self.control_store, self.repository_root).run(
            attestation.approval_id,
            attestation.approval_object_digest,
            source_job_id,
            attestation.proposal_id,
            attestation.proposal_object_digest,
            inspected.expected_base_bundle_digest,
            bundle_path=bundle_path,
        )
        from medlearn_vault.vault_writer import VaultPublicationWriter

        publication = VaultPublicationWriter(self.control_store, self.vault_store).run(
            plan.publication_plan_id,
            plan.publication_plan_object_digest,
            source_job_id,
        )
        return AutoPublicationResult(
            status="published",
            source_job_id=source_job_id,
            proposal_id=inspected.proposal_id,
            proposal_status=proposal.status,
            approval_id=attestation.approval_id,
            approval_object_digest=attestation.approval_object_digest,
            publication_plan_id=plan.publication_plan_id,
            publication_plan_object_digest=plan.publication_plan_object_digest,
            capture_id=publication.capture_id,
            created_count=len(publication.created_paths),
            reused_count=len(publication.reused_paths),
            receipt_status=publication.receipt_status,
        )

    def _eligibility_reason(self, job: JobRecord, proposal: CaptureProposal) -> str | None:
        if proposal.status != "ready_for_review":
            blocking_issues = sorted(
                item.code for item in proposal.issues if item.severity in {"error", "review"}
            )
            return (
                f"PROPOSAL_ISSUE_{blocking_issues[0]}"
                if blocking_issues
                else "PROPOSAL_NOT_READY_FOR_REVIEW"
            )
        if job.status != "succeeded" or job.workflow_run_id is None:
            raise WorkflowError("INVALID_JOB")
        if any(item.severity in {"error", "review"} for item in proposal.issues):
            return "PROPOSAL_HAS_BLOCKING_ISSUES"
        if proposal.source_candidate is not None:
            return "SOURCE_BOOTSTRAP_CANDIDATE"
        if proposal.new_concept_candidates:
            return "NEW_CONCEPT_CANDIDATE"
        if any(
            item.status not in {"matched", "redirected"}
            for item in proposal.concept_resolutions
        ):
            return "UNRESOLVED_CONCEPT"
        if any(
            item.proposed_verification_status != "unverified_chat"
            or item.proposed_evidence_state != "unassessed"
            for item in proposal.claim_proposals
        ):
            return "CLAIM_AUTHORITY_STATE_INVALID"
        return None

    def _manual(
        self,
        inspected: ProposalInspectionResult,
        proposal: CaptureProposal,
        reason: str,
    ) -> AutoPublicationResult:
        return AutoPublicationResult(
            status="manual_review_required",
            source_job_id=inspected.source_job_id,
            proposal_id=inspected.proposal_id,
            proposal_status=proposal.status,
            manual_review_reason=reason,
        )

    def _read(self, key: str, missing_code: str) -> StoredObject:
        try:
            stored = self.control_store.get(key)
        except WorkflowError:
            raise
        except Exception as exc:
            raise WorkflowError("CONTROL_STORE_FAILURE") from exc
        if stored is None:
            raise WorkflowError(missing_code)
        return stored


def resolve_bundle_path(repository_root: Path, configured_path: str) -> Path:
    if not configured_path or "\\" in configured_path:
        raise WorkflowError("INVALID_BUNDLE_PATH")
    relative = PurePosixPath(configured_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise WorkflowError("INVALID_BUNDLE_PATH")
    root = repository_root.resolve()
    candidate = (root / Path(*relative.parts)).resolve()
    if candidate == root or root not in candidate.parents or not candidate.is_dir():
        raise WorkflowError("INVALID_BUNDLE_PATH")
    if any(
        not (candidate / filename).is_file()
        or candidate not in (candidate / filename).resolve().parents
        for filename in BUNDLE_FILES
    ):
        raise WorkflowError("INVALID_BUNDLE")
    try:
        bundle = ContractBundle.from_directory(candidate)
        if any(item.severity == "error" for item in bundle.validate_integrity()):
            raise WorkflowError("INVALID_BUNDLE")
    except WorkflowError:
        raise
    except (OSError, ValueError) as exc:
        raise WorkflowError("INVALID_BUNDLE") from exc
    return candidate


class S3ReadOnlyObjectStore:
    """R2 S3 adapter fixed to the medlearn-control bucket."""

    def __init__(self, endpoint: str, access_key_id: str, secret_access_key: str) -> None:
        if not endpoint or not access_key_id or not secret_access_key:
            raise WorkflowError("SERVICE_MISCONFIGURED")
        self._client: Any = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name="auto",
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )

    def get(self, key: str) -> StoredObject | None:
        try:
            response = self._client.get_object(Bucket=CONTROL_BUCKET, Key=key)
            return StoredObject(body=response["Body"].read(), etag=response["ETag"])
        except ClientError as exc:
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
                return None
            raise WorkflowError("CONTROL_STORE_FAILURE") from exc
        except Exception as exc:
            raise WorkflowError("CONTROL_STORE_FAILURE") from exc


class S3ObjectStore(S3ReadOnlyObjectStore):
    """Writable R2 S3 adapter for existing proposal and approval workflows."""

    def create(self, key: str, body: bytes, *, content_type: str) -> bool:
        return self._put(key, body, content_type=content_type, if_none_match="*")

    def compare_and_swap(
        self, key: str, body: bytes, etag: str, *, content_type: str
    ) -> bool:
        return self._put(key, body, content_type=content_type, if_match=etag)

    def _put(
        self,
        key: str,
        body: bytes,
        *,
        content_type: str,
        if_none_match: str | None = None,
        if_match: str | None = None,
    ) -> bool:
        kwargs: dict[str, Any] = {
            "Bucket": CONTROL_BUCKET,
            "Key": key,
            "Body": body,
            "ContentType": content_type,
        }
        if if_none_match is not None:
            kwargs["IfNoneMatch"] = if_none_match
        if if_match is not None:
            kwargs["IfMatch"] = if_match
        try:
            self._client.put_object(**kwargs)
            return True
        except ClientError as exc:
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") in {409, 412}:
                return False
            raise WorkflowError("CONTROL_STORE_FAILURE") from exc
        except Exception as exc:
            raise WorkflowError("CONTROL_STORE_FAILURE") from exc


class ProposalOrchestrator:
    def __init__(self, store: ObjectStore, repository_root: Path) -> None:
        self.store = store
        self.repository_root = repository_root.resolve()

    def run(
        self,
        inputs: WorkflowInputs,
        *,
        bundle_path: str,
        workflow_run_id: str,
        now: datetime | None = None,
    ) -> OrchestrationResult:
        current_time = now or datetime.now(UTC)
        if current_time.tzinfo is None or not workflow_run_id:
            raise WorkflowError("INVALID_WORKFLOW_INPUT")
        job_key = f"v1/jobs/{inputs.job_id}.json"
        job_stored = self.store.get(job_key)
        if job_stored is None:
            raise WorkflowError("JOB_NOT_FOUND")
        try:
            job = JobRecord.model_validate_json(job_stored.body)
        except ValueError as exc:
            raise WorkflowError("INVALID_JOB_RECORD") from exc
        if (
            job.job_id != inputs.job_id
            or job.intake_object_key != inputs.intake_object_key
            or job.intake_digest != inputs.intake_digest
        ):
            raise WorkflowError("JOB_INPUT_MISMATCH")

        intake = self.store.get(inputs.intake_object_key)
        execution_key = f"v1/executions/{inputs.job_id}.json"
        if intake is None:
            self._record_failure(
                job_key, execution_key, workflow_run_id, "INTAKE_NOT_FOUND", current_time
            )
            raise WorkflowError("INTAKE_NOT_FOUND")
        try:
            draft_bytes, _ = extract_capture_draft(intake.body, inputs.intake_digest)
        except IntakeDigestMismatch as exc:
            self._record_failure(
                job_key,
                execution_key,
                workflow_run_id,
                "INTAKE_DIGEST_MISMATCH",
                current_time,
            )
            raise WorkflowError("INTAKE_DIGEST_MISMATCH") from exc
        except InvalidIntakeEnvelope as exc:
            self._record_failure(
                job_key,
                execution_key,
                workflow_run_id,
                "INVALID_INTAKE_ENVELOPE",
                current_time,
            )
            raise WorkflowError("INVALID_INTAKE_ENVELOPE") from exc
        except Exception as exc:
            self._record_failure(
                job_key,
                execution_key,
                workflow_run_id,
                "ORCHESTRATION_FAILED",
                current_time,
            )
            raise WorkflowError("ORCHESTRATION_FAILED") from exc
        try:
            bundle = ContractBundle.from_directory(
                resolve_bundle_path(self.repository_root, bundle_path)
            )
        except WorkflowError as exc:
            self._record_failure(
                job_key, execution_key, workflow_run_id, exc.code, current_time
            )
            raise

        execution_stored = self.store.get(execution_key)
        execution, execution_etag = self._acquire_execution(
            execution_key,
            execution_stored,
            inputs.job_id,
            workflow_run_id,
            current_time,
        )
        if execution is None:
            return OrchestrationResult(status="lease_held", reused=True)

        try:
            draft = CaptureDraft.model_validate_json(draft_bytes)
            proposal = build_capture_proposal(bundle, draft)
            proposal_bytes = exact_capture_proposal_json(proposal)
            review_bytes = render_capture_proposal_markdown(proposal, bundle=bundle).encode()
            if execution.status in {"succeeded", "blocked"}:
                self._verify_terminal_outputs(execution, proposal, proposal_bytes, review_bytes)
                self._reconcile_terminal_job(
                    job_key, inputs, execution, current_time
                )
                return OrchestrationResult(
                    status=execution.status, proposal_id=proposal.proposal_id, reused=True
                )

            job_stored, job = self._mark_running(job_key, job_stored, job, current_time)
            proposal_key = f"v1/proposals/{proposal.proposal_id}.json"
            review_key = f"v1/reviews/{proposal.proposal_id}.md"
            self._create_or_verify(proposal_key, proposal_bytes, "application/json")
            self._create_or_verify(review_key, review_bytes, "text/markdown; charset=utf-8")
            terminal_status: Literal["succeeded", "blocked"] = (
                "blocked" if proposal.status == "blocked" else "succeeded"
            )
            terminal_execution = ProposalExecutionRecord.model_validate(
                {
                    **execution.model_dump(),
                    "status": terminal_status,
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "proposal_id": proposal.proposal_id,
                    "proposal_digest": _digest(proposal_bytes),
                    "review_digest": _digest(review_bytes),
                    "workflow_run_id": workflow_run_id,
                    "updated_at": current_time,
                }
            )
            if not self.store.compare_and_swap(
                execution_key,
                _json_bytes(terminal_execution),
                execution_etag,
                content_type="application/json",
            ):
                raise WorkflowError("STALE_EXECUTION_UPDATE")
            terminal_job = JobRecord.model_validate(
                {
                    **job.model_dump(),
                    "status": terminal_status,
                    "proposal_id": proposal.proposal_id,
                    "workflow_run_id": workflow_run_id,
                    "dispatch_lease_id": None,
                    "dispatch_lease_expires_at": None,
                    "error_code": None,
                    "updated_at": current_time,
                }
            )
            if not self.store.compare_and_swap(
                job_key,
                _json_bytes(terminal_job),
                job_stored.etag,
                content_type="application/json",
            ):
                raise WorkflowError("STALE_JOB_UPDATE")
            return OrchestrationResult(
                status=terminal_status, proposal_id=proposal.proposal_id
            )
        except WorkflowError as exc:
            self._record_failure(job_key, execution_key, workflow_run_id, exc.code, current_time)
            raise
        except Exception as exc:
            self._record_failure(
                job_key, execution_key, workflow_run_id, "ORCHESTRATION_FAILED", current_time
            )
            raise WorkflowError("ORCHESTRATION_FAILED") from exc

    def _acquire_execution(
        self,
        key: str,
        stored: StoredObject | None,
        job_id: str,
        run_id: str,
        now: datetime,
    ) -> tuple[ProposalExecutionRecord | None, str]:
        if stored is None:
            proposed = ProposalExecutionRecord(
                job_id=job_id,
                status="running",
                lease_owner=run_id,
                lease_expires_at=now + LEASE_DURATION,
                created_at=now,
                updated_at=now,
            )
            if self.store.create(key, _json_bytes(proposed), content_type="application/json"):
                created = self.store.get(key)
                if created is None:
                    raise WorkflowError("CONTROL_STORE_FAILURE")
                return proposed, created.etag
            stored = self.store.get(key)
            if stored is None:
                raise WorkflowError("CONTROL_STORE_FAILURE")
        try:
            current = ProposalExecutionRecord.model_validate_json(stored.body)
        except ValueError as exc:
            raise WorkflowError("INVALID_EXECUTION_RECORD") from exc
        if current.status in {"succeeded", "blocked"}:
            return current, stored.etag
        if current.status == "failed":
            takeover = ProposalExecutionRecord.model_validate(
                {
                    **current.model_dump(),
                    "status": "running",
                    "lease_owner": run_id,
                    "lease_expires_at": now + LEASE_DURATION,
                    "error_code": None,
                    "updated_at": now,
                }
            )
        elif (
            current.lease_owner != run_id
            and current.lease_expires_at
            and current.lease_expires_at > now
        ):
            return None, stored.etag
        else:
            takeover = ProposalExecutionRecord.model_validate(
                {
                    **current.model_dump(),
                    "lease_owner": run_id,
                    "lease_expires_at": now + LEASE_DURATION,
                    "updated_at": now,
                }
            )
        if not self.store.compare_and_swap(
            key, _json_bytes(takeover), stored.etag, content_type="application/json"
        ):
            winner = self.store.get(key)
            if winner is None:
                raise WorkflowError("CONTROL_STORE_FAILURE")
            winner_record = ProposalExecutionRecord.model_validate_json(winner.body)
            if winner_record.status in {"succeeded", "blocked"}:
                return winner_record, winner.etag
            return None, winner.etag
        updated = self.store.get(key)
        if updated is None:
            raise WorkflowError("CONTROL_STORE_FAILURE")
        return takeover, updated.etag

    def _mark_running(
        self, key: str, stored: StoredObject, job: JobRecord, now: datetime
    ) -> tuple[StoredObject, JobRecord]:
        if job.status == "running":
            return stored, job
        if job.status != "dispatched":
            raise WorkflowError("INVALID_JOB_STATE")
        running = JobRecord.model_validate(
            {
                **job.model_dump(),
                "status": "running",
                "error_code": None,
                "updated_at": now,
            }
        )
        if not self.store.compare_and_swap(
            key, _json_bytes(running), stored.etag, content_type="application/json"
        ):
            raise WorkflowError("STALE_JOB_UPDATE")
        updated = self.store.get(key)
        if updated is None:
            raise WorkflowError("CONTROL_STORE_FAILURE")
        return updated, running

    def _create_or_verify(self, key: str, expected: bytes, content_type: str) -> None:
        if self.store.create(key, expected, content_type=content_type):
            return
        existing = self.store.get(key)
        if existing is None or existing.body != expected:
            raise WorkflowError("PROPOSAL_COLLISION")

    def _verify_terminal_outputs(
        self,
        execution: ProposalExecutionRecord,
        proposal: CaptureProposal,
        proposal_bytes: bytes,
        review_bytes: bytes,
    ) -> None:
        if execution.proposal_id != proposal.proposal_id:
            raise WorkflowError("PROPOSAL_COLLISION")
        proposal_object = self.store.get(f"v1/proposals/{proposal.proposal_id}.json")
        review_object = self.store.get(f"v1/reviews/{proposal.proposal_id}.md")
        if (
            proposal_object is None
            or review_object is None
            or proposal_object.body != proposal_bytes
            or review_object.body != review_bytes
            or execution.proposal_digest != _digest(proposal_bytes)
            or execution.review_digest != _digest(review_bytes)
        ):
            raise WorkflowError("PROPOSAL_COLLISION")

    def _reconcile_terminal_job(
        self,
        key: str,
        inputs: WorkflowInputs,
        execution: ProposalExecutionRecord,
        now: datetime,
    ) -> None:
        for _ in range(TERMINAL_JOB_RETRIES):
            stored = self.store.get(key)
            if stored is None:
                raise WorkflowError("CONTROL_STATE_CONFLICT")
            try:
                job = JobRecord.model_validate_json(stored.body)
            except ValueError as exc:
                raise WorkflowError("CONTROL_STATE_CONFLICT") from exc
            identity_matches = (
                job.job_id == inputs.job_id
                and job.intake_object_key == inputs.intake_object_key
                and job.intake_digest == inputs.intake_digest
            )
            if not identity_matches or job.status == "expired":
                raise WorkflowError("CONTROL_STATE_CONFLICT")
            if job.status in {"succeeded", "blocked"}:
                if (
                    job.status == execution.status
                    and job.proposal_id == execution.proposal_id
                    and job.workflow_run_id == execution.workflow_run_id
                ):
                    return
                raise WorkflowError("CONTROL_STATE_CONFLICT")
            if job.status not in {"dispatched", "running", "failed"}:
                raise WorkflowError("CONTROL_STATE_CONFLICT")
            repaired = JobRecord.model_validate(
                {
                    **job.model_dump(),
                    "status": execution.status,
                    "proposal_id": execution.proposal_id,
                    "workflow_run_id": execution.workflow_run_id,
                    "dispatch_lease_id": None,
                    "dispatch_lease_expires_at": None,
                    "error_code": None,
                    "updated_at": now,
                }
            )
            if self.store.compare_and_swap(
                key, _json_bytes(repaired), stored.etag, content_type="application/json"
            ):
                return
        raise WorkflowError("CONTROL_STATE_CONFLICT")

    def _record_failure(
        self,
        job_key: str,
        execution_key: str,
        run_id: str,
        code: str,
        now: datetime,
    ) -> None:
        try:
            execution_stored = self.store.get(execution_key)
            if execution_stored:
                execution = ProposalExecutionRecord.model_validate_json(execution_stored.body)
                if execution.status == "running" and execution.lease_owner == run_id:
                    failed_execution = ProposalExecutionRecord.model_validate(
                        {
                            **execution.model_dump(),
                            "status": "failed",
                            "lease_owner": None,
                            "lease_expires_at": None,
                            "error_code": code,
                            "updated_at": now,
                        }
                    )
                    self.store.compare_and_swap(
                        execution_key,
                        _json_bytes(failed_execution),
                        execution_stored.etag,
                        content_type="application/json",
                    )
            job_stored = self.store.get(job_key)
            if job_stored:
                job = JobRecord.model_validate_json(job_stored.body)
                if execution_stored is None:
                    self.store.create(
                        execution_key,
                        _json_bytes(
                            ProposalExecutionRecord(
                                job_id=job.job_id,
                                status="failed",
                                error_code=code,
                                created_at=now,
                                updated_at=now,
                            )
                        ),
                        content_type="application/json",
                    )
                if job.status in {"dispatched", "running"}:
                    failed_job = JobRecord.model_validate(
                        {
                            **job.model_dump(),
                            "status": "failed",
                            "error_code": code,
                            "dispatch_lease_id": None,
                            "dispatch_lease_expires_at": None,
                            "updated_at": now,
                        }
                    )
                    self.store.compare_and_swap(
                        job_key,
                        _json_bytes(failed_job),
                        job_stored.etag,
                        content_type="application/json",
                    )
        except Exception:
            return


class ReproposalOrchestrator:
    """Explicit, bounded manual reproposal that reuses the exact immutable Intake
    but creates a new Job/Proposal against the current catalog.

    This is the only path from a blocked bootstrap Proposal to a ready
    Proposal after catalog merge.  It never mutates the terminal blocked Job.

    It cryptographically verifies a repository-tracked CatalogMergeReceipt
    committed at catalog_updates/<catalog_update_id>/receipt.json — the
    only proof that the catalog patch was actually merged.
    """

    def __init__(self, store: ObjectStore, repository_root: Path) -> None:
        self.store = store
        self.repository_root = repository_root.resolve()

    def run(
        self,
        source_job_id: str,
        blocked_proposal_id: str,
        catalog_update_id: str,
        previous_base_bundle_digest: str,
        confirmation: str,
        *,
        bundle_path: str,
        now: datetime | None = None,
    ) -> ReproposalResult:
        from medlearn_vault.catalog_update import (
            CatalogMergeReceipt,
            canonical_receipt_json,
            receipt_object_digest,
        )

        current_time = now or datetime.now(UTC)
        if current_time.tzinfo is None:
            raise WorkflowError("INVALID_REPROPOSAL_INPUT")
        valid_input = (
            re.fullmatch(JOB_ID_PATTERN, source_job_id) is not None
            and re.fullmatch(PROPOSAL_ID_PATTERN, blocked_proposal_id) is not None
            and re.fullmatch(CATALOG_UPDATE_ID_PATTERN, catalog_update_id) is not None
            and re.fullmatch(DIGEST_PATTERN, previous_base_bundle_digest) is not None
            and confirmation == blocked_proposal_id
        )
        if not valid_input:
            raise WorkflowError("INVALID_REPROPOSAL_INPUT")

        # 1. Read and verify the existing blocked Job
        job_key = f"v1/jobs/{source_job_id}.json"
        job_stored = self._read(job_key, "JOB_NOT_FOUND")
        try:
            job = JobRecord.model_validate_json(job_stored.body)
        except (ValueError, TypeError) as exc:
            raise WorkflowError("INVALID_JOB") from exc
        if (
            job.job_id != source_job_id
            or job.status != "blocked"
            or job.proposal_id is None
            or job.proposal_id != blocked_proposal_id
            or job.workflow_run_id is None
        ):
            raise WorkflowError("INVALID_JOB")

        # 2. Read and verify the blocked Execution
        execution_key = f"v1/executions/{source_job_id}.json"
        execution_stored = self._read(execution_key, "EXECUTION_NOT_FOUND")
        try:
            execution = ProposalExecutionRecord.model_validate_json(execution_stored.body)
        except (ValueError, TypeError) as exc:
            raise WorkflowError("INVALID_EXECUTION") from exc
        if (
            execution.job_id != source_job_id
            or execution.status != "blocked"
            or execution.proposal_id != blocked_proposal_id
            or execution.proposal_digest is None
            or execution.review_digest is None
        ):
            raise WorkflowError("INVALID_EXECUTION")

        # 3. Read and verify exact Intake bytes
        intake_key = job.intake_object_key
        intake_stored = self._read(intake_key, "INTAKE_NOT_FOUND")
        intake_digest = _digest(intake_stored.body)
        if intake_digest != job.intake_digest:
            raise WorkflowError("INTAKE_DIGEST_MISMATCH")

        # 4. Read and verify the blocked Proposal
        proposal_key = f"v1/proposals/{blocked_proposal_id}.json"
        proposal_stored = self._read(proposal_key, "PROPOSAL_NOT_FOUND")
        try:
            blocked_proposal = CaptureProposal.model_validate_json(proposal_stored.body)
        except (ValueError, TypeError) as exc:
            raise WorkflowError("INVALID_PROPOSAL") from exc
        if blocked_proposal.proposal_id != blocked_proposal_id:
            raise WorkflowError("INVALID_PROPOSAL")
        if capture_proposal_digest(blocked_proposal) != blocked_proposal.proposal_digest:
            raise WorkflowError("INVALID_PROPOSAL")
        if blocked_proposal.status != "blocked":
            raise WorkflowError("PROPOSAL_NOT_BLOCKED")
        if not any(
            issue.code == "CATALOG_UPDATE_REQUIRED" for issue in blocked_proposal.issues
        ):
            raise WorkflowError("PROPOSAL_NOT_BLOCKED")
        if blocked_proposal.base_bundle_digest != previous_base_bundle_digest:
            raise WorkflowError("BASE_BUNDLE_DIGEST_MISMATCH")

        # 5. Read and verify Review
        review_key = f"v1/reviews/{blocked_proposal_id}.md"
        review_stored = self._read(review_key, "REVIEW_NOT_FOUND")
        if _digest(review_stored.body) != execution.review_digest:
            raise WorkflowError("REVIEW_DIGEST_MISMATCH")

        # 6. Load the current bundle and verify it differs from the blocked bundle
        resolved_bundle_path = resolve_bundle_path(self.repository_root, bundle_path)
        try:
            bundle = ContractBundle.from_directory(resolved_bundle_path)
        except WorkflowError as exc:
            raise WorkflowError("INVALID_BUNDLE") from exc
        current_bundle_digest = contract_bundle_digest(bundle)
        if current_bundle_digest == previous_base_bundle_digest:
            raise WorkflowError("STALE_BASE_BUNDLE")

        # 7. Load and cryptographically verify the catalog merge receipt.
        #    The receipt is the only proof that the catalog patch identified
        #    by catalog_update_id was actually merged into the repository.
        receipt_path = (
            self.repository_root
            / "catalog_updates"
            / catalog_update_id
            / "receipt.json"
        )
        try:
            receipt_bytes = receipt_path.read_bytes()
        except (OSError, ValueError) as exc:
            raise WorkflowError("RECEIPT_NOT_FOUND") from exc

        # Verify receipt is valid UTF-8, LF-only, LF-terminated
        try:
            receipt_text = receipt_bytes.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as exc:
            raise WorkflowError("INVALID_RECEIPT") from exc
        if "\r" in receipt_text or not receipt_text.endswith("\n"):
            raise WorkflowError("INVALID_RECEIPT")

        # Parse and validate receipt
        try:
            receipt = CatalogMergeReceipt.model_validate_json(receipt_bytes)
        except (ValueError, TypeError) as exc:
            raise WorkflowError("INVALID_RECEIPT") from exc

        # Verify canonical byte-for-byte consistency
        expected_canonical = canonical_receipt_json(receipt)
        if expected_canonical != receipt_bytes:
            raise WorkflowError("INVALID_RECEIPT")

        receipt_digest = receipt_object_digest(receipt)

        # Verify receipt identity
        if receipt.catalog_update_id != catalog_update_id:
            raise WorkflowError("RECEIPT_CATALOG_MISMATCH")

        # Verify receipt is bound to the blocked Proposal
        if receipt.capture_proposal_id != blocked_proposal_id:
            raise WorkflowError("RECEIPT_PROPOSAL_MISMATCH")
        if receipt.capture_proposal_digest != blocked_proposal.proposal_digest:
            raise WorkflowError("RECEIPT_PROPOSAL_MISMATCH")
        proposal_object_digest = _digest(proposal_stored.body)
        if receipt.capture_proposal_object_digest != proposal_object_digest:
            raise WorkflowError("RECEIPT_PROPOSAL_MISMATCH")

        # Verify base bundle digest matches the blocked Proposal
        if receipt.previous_base_bundle_digest != previous_base_bundle_digest:
            raise WorkflowError("RECEIPT_BUNDLE_MISMATCH")

        # Verify target bundle path
        if receipt.target_bundle_path != bundle_path:
            raise WorkflowError("RECEIPT_BUNDLE_PATH_MISMATCH")

        # Hash the current exact sources.json and concepts.json from the bundle
        # and require them to equal the receipt's sources_new_digest and
        # concepts_new_digest.  This proves the catalog patch was actually
        # applied.
        sources_path = resolved_bundle_path / "sources.json"
        concepts_path = resolved_bundle_path / "concepts.json"
        try:
            current_sources = sources_path.read_bytes()
            current_concepts = concepts_path.read_bytes()
        except (OSError, ValueError) as exc:
            raise WorkflowError("INVALID_BUNDLE") from exc

        current_sources_digest = _digest(current_sources)
        current_concepts_digest = _digest(current_concepts)
        if current_sources_digest != receipt.sources_new_digest:
            raise WorkflowError("RECEIPT_SOURCES_MISMATCH")
        if current_concepts_digest != receipt.concepts_new_digest:
            raise WorkflowError("RECEIPT_CONCEPTS_MISMATCH")

        # 8. Derive the reproposal Job identity, bound to the receipt digest.
        #    This cryptographically links the reproposal to the exact merged
        #    catalog patch, not just the user-supplied catalog_update_id.
        reproposal_job_id = _id(
            "reproposal",
            "0.1.0",
            source_job_id,
            blocked_proposal_id,
            catalog_update_id,
            receipt_digest,
            current_bundle_digest,
            intake_digest,
        )

        # 9. Check idempotency: if the reproposal Job already exists, verify and return
        reproposal_job_key = f"v1/jobs/{reproposal_job_id}.json"
        existing_job = self.store.get(reproposal_job_key)
        if existing_job is not None:
            try:
                existing = JobRecord.model_validate_json(existing_job.body)
            except (ValueError, TypeError) as exc:
                raise WorkflowError("REPROPOSAL_CONFLICT") from exc
            if (
                existing.job_id != reproposal_job_id
                or existing.intake_digest != intake_digest
                or existing.intake_object_key != intake_key
                or existing.reproposal_of_job_id != source_job_id
                or existing.reproposal_of_proposal_id != blocked_proposal_id
                or existing.catalog_update_id != catalog_update_id
            ):
                raise WorkflowError("REPROPOSAL_CONFLICT")
            if existing.status in {"succeeded", "blocked"} and existing.proposal_id:
                return ReproposalResult(
                    source_job_id=reproposal_job_id,
                    proposal_id=existing.proposal_id,
                    base_bundle_digest=current_bundle_digest,
                    reused=True,
                )
            raise WorkflowError("REPROPOSAL_CONFLICT")

        # 10. Extract draft from exact Intake bytes and rebuild Proposal
        try:
            draft_bytes, _ = extract_capture_draft(intake_stored.body, intake_digest)
        except (IntakeDigestMismatch, InvalidIntakeEnvelope) as exc:
            raise WorkflowError("INVALID_INTAKE_ENVELOPE") from exc
        try:
            draft = CaptureDraft.model_validate_json(draft_bytes)
        except (ValueError, TypeError) as exc:
            raise WorkflowError("INVALID_INTAKE_ENVELOPE") from exc

        proposal = build_capture_proposal(bundle, draft)
        proposal_bytes = exact_capture_proposal_json(proposal)
        review_bytes = render_capture_proposal_markdown(proposal, bundle=bundle).encode()
        proposal_digest = _digest(proposal_bytes)

        # 11. Create-only write the new reproposal Job
        reproposal_now = current_time
        reproposal_job = JobRecord(
            job_id=reproposal_job_id,
            status="succeeded" if proposal.status == "ready_for_review" else "blocked",
            intake_digest=intake_digest,
            intake_object_key=intake_key,
            proposal_id=proposal.proposal_id,
            workflow_run_id=reproposal_job_id,
            dispatch_attempt=0,
            created_at=reproposal_now,
            updated_at=reproposal_now,
            reproposal_of_job_id=source_job_id,
            reproposal_of_proposal_id=blocked_proposal_id,
            catalog_update_id=catalog_update_id,
        )
        if not self.store.create(
            reproposal_job_key,
            _json_bytes(reproposal_job),
            content_type="application/json",
        ):
            winner = self.store.get(reproposal_job_key)
            if winner is None:
                raise WorkflowError("CONTROL_STORE_FAILURE")
            try:
                existing = JobRecord.model_validate_json(winner.body)
            except (ValueError, TypeError) as exc:
                raise WorkflowError("REPROPOSAL_CONFLICT") from exc
            if (
                existing.job_id != reproposal_job_id
                or existing.status != reproposal_job.status
                or existing.proposal_id != proposal.proposal_id
                or existing.intake_digest != intake_digest
            ):
                raise WorkflowError("REPROPOSAL_CONFLICT")
            return ReproposalResult(
                source_job_id=reproposal_job_id,
                proposal_id=proposal.proposal_id,
                base_bundle_digest=current_bundle_digest,
                reused=True,
            )

        # 12. Create-only write Execution
        execution_status: Literal["succeeded", "blocked"] = (
            "blocked" if proposal.status == "blocked" else "succeeded"
        )
        reproposal_execution = ProposalExecutionRecord(
            job_id=reproposal_job_id,
            status=execution_status,
            proposal_id=proposal.proposal_id,
            proposal_digest=proposal_digest,
            review_digest=_digest(review_bytes),
            workflow_run_id=reproposal_job_id,
            created_at=reproposal_now,
            updated_at=reproposal_now,
        )
        if not self.store.create(
            f"v1/executions/{reproposal_job_id}.json",
            _json_bytes(reproposal_execution),
            content_type="application/json",
        ):
            raise WorkflowError("CONTROL_STORE_FAILURE")

        # 13. Create-only write Proposal and Review
        self._create_or_verify(
            f"v1/proposals/{proposal.proposal_id}.json",
            proposal_bytes,
            "application/json",
        )
        self._create_or_verify(
            f"v1/reviews/{proposal.proposal_id}.md",
            review_bytes,
            "text/markdown; charset=utf-8",
        )

        return ReproposalResult(
            source_job_id=reproposal_job_id,
            proposal_id=proposal.proposal_id,
            base_bundle_digest=current_bundle_digest,
        )

    def _read(self, key: str, missing_code: str) -> StoredObject:
        try:
            stored = self.store.get(key)
        except WorkflowError:
            raise
        except Exception as exc:
            raise WorkflowError("CONTROL_STORE_FAILURE") from exc
        if stored is None:
            raise WorkflowError(missing_code)
        return stored

    def _create_or_verify(self, key: str, expected: bytes, content_type: str) -> None:
        if self.store.create(key, expected, content_type=content_type):
            return
        existing = self.store.get(key)
        if existing is None or existing.body != expected:
            raise WorkflowError("PROPOSAL_COLLISION")
