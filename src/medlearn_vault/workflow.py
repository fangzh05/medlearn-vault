"""Idempotent control-plane orchestration for proposal generation."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from pydantic import ConfigDict, Field, model_validator

from medlearn_vault.bundle import ContractBundle
from medlearn_vault.capture import (
    CaptureDraft,
    CaptureProposal,
    build_capture_proposal,
    extract_capture_draft,
    render_capture_proposal_markdown,
)
from medlearn_vault.domain.base import AwareDatetime, DomainModel

CONTROL_BUCKET = "medlearn-control"
LEASE_DURATION = timedelta(minutes=10)
JOB_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"
DIGEST_PATTERN = r"^sha256:[a-f0-9]{64}$"
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


class S3ObjectStore:
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
        except ValueError as exc:
            self._record_failure(
                job_key,
                execution_key,
                workflow_run_id,
                "INTAKE_DIGEST_MISMATCH",
                current_time,
            )
            raise WorkflowError("INTAKE_DIGEST_MISMATCH") from exc
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
            proposal_bytes = _json_bytes(proposal)
            review_bytes = render_capture_proposal_markdown(proposal, bundle=bundle).encode()
            if execution.status in {"succeeded", "blocked"}:
                self._verify_terminal_outputs(execution, proposal, proposal_bytes, review_bytes)
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
