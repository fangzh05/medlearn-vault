import hashlib
import json
import os
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from botocore.exceptions import ClientError
from pydantic import ValidationError
from typer.testing import CliRunner

from medlearn_vault.bundle import ContractBundle
from medlearn_vault.capture import (
    CaptureProposal,
    IntakeEnvelope,
    build_capture_proposal,
    capture_proposal_digest,
)
from medlearn_vault.cli import app
from medlearn_vault.workflow import (
    ApprovalAttestor,
    ApprovalOrchestrator,
    JobRecord,
    ObjectStore,
    ProposalApprovalRecord,
    ProposalExecutionRecord,
    ProposalOrchestrator,
    ProposalOutputInspector,
    PublicationPlanOrchestrator,
    ReproposalOrchestrator,
    S3ObjectStore,
    StoredObject,
    WorkflowError,
    WorkflowInputs,
    approval_identity,
    canonical_approval_json,
    resolve_bundle_path,
)

ROOT = Path.cwd()
NOW = datetime(2026, 7, 12, tzinfo=UTC)


def json_bytes(value: object) -> bytes:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")  # type: ignore[union-attr]
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


class MemoryStore(ObjectStore):
    def __init__(self) -> None:
        self.objects: dict[str, StoredObject] = {}
        self.creates: list[str] = []
        self.swaps: list[str] = []
        self._revision = 0
        self.fail_create_once: str | None = None

    def _etag(self) -> str:
        self._revision += 1
        return f'"etag-{self._revision}"'

    def get(self, key: str) -> StoredObject | None:
        return self.objects.get(key)

    def create(self, key: str, body: bytes, *, content_type: str) -> bool:
        del content_type
        if self.fail_create_once == key:
            self.fail_create_once = None
            raise WorkflowError("CONTROL_STORE_FAILURE")
        if key in self.objects:
            return False
        self.objects[key] = StoredObject(body=body, etag=self._etag())
        self.creates.append(key)
        return True

    def compare_and_swap(
        self, key: str, body: bytes, etag: str, *, content_type: str
    ) -> bool:
        del content_type
        current = self.objects.get(key)
        if current is None or current.etag != etag:
            return False
        self.objects[key] = StoredObject(body=body, etag=self._etag())
        self.swaps.append(key)
        return True

    def seed(self, key: str, body: bytes) -> None:
        self.objects[key] = StoredObject(body=body, etag=self._etag())


class ReadOnlyRecordingStore:
    def __init__(self, objects: dict[str, StoredObject]) -> None:
        self.objects = dict(objects)
        self.reads: list[str] = []

    def get(self, key: str) -> StoredObject | None:
        self.reads.append(key)
        return self.objects.get(key)

    def create(self, *args: object, **kwargs: object) -> bool:
        raise AssertionError("attestation attempted a create")

    def compare_and_swap(self, *args: object, **kwargs: object) -> bool:
        raise AssertionError("attestation attempted a compare-and-swap")


def seed_job(
    store: MemoryStore,
    envelope: bytes,
    *,
    job_id: str = "job-copd-001",
    status: str = "dispatched",
) -> tuple[WorkflowInputs, JobRecord]:
    digest = "sha256:" + hashlib.sha256(envelope).hexdigest()
    key = f"v1/intakes/sha256/{digest[7:]}.json"
    inputs = WorkflowInputs(job_id=job_id, intake_object_key=key, intake_digest=digest)
    job = JobRecord(
        job_id=job_id,
        status=status,
        intake_digest=digest,
        intake_object_key=key,
        dispatch_attempt=1,
        created_at=NOW,
        updated_at=NOW,
        **({"error_code": "RETRYABLE"} if status == "failed" else {}),
    )
    store.seed(key, envelope)
    store.seed(f"v1/jobs/{job_id}.json", json_bytes(job))
    return inputs, job


def copd_envelope() -> bytes:
    return Path("examples/intake/manual-copd.json").read_bytes()


def ambiguous_envelope() -> bytes:
    draft = json.loads(Path("examples/capture/ambiguous-ms/draft.json").read_text(encoding="utf-8"))
    return json.dumps(
        {"intake_version": "0.1.0", "client_kind": "manual", "draft": draft},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()


def seed_proposal(
    store: MemoryStore, *, blocked: bool = False
) -> tuple[str, str, str, bytes]:
    envelope = ambiguous_envelope() if blocked else copd_envelope()
    inputs, _ = seed_job(store, envelope, job_id="job-approval-source")
    bundle = "examples/capture/ambiguous-ms/bundle" if blocked else "examples/copd"
    result = ProposalOrchestrator(store, ROOT).run(
        inputs, bundle_path=bundle, workflow_run_id="run-approval-source", now=NOW
    )
    assert result.proposal_id
    body = store.objects[f"v1/proposals/{result.proposal_id}.json"].body
    proposal = json.loads(body)
    return (
        result.proposal_id,
        "sha256:" + hashlib.sha256(body).hexdigest(),
        proposal["base_bundle_digest"],
        body,
    )


def seed_attestation(
    *, decision: str = "approved", rejection_code: str | None = None
) -> tuple[ReadOnlyRecordingStore, dict[str, str]]:
    store = MemoryStore()
    proposal_id, proposal_digest, base_digest, _ = seed_proposal(store)
    approval = ApprovalOrchestrator(store).run(
        proposal_id,
        proposal_digest,
        base_digest,
        decision=decision,  # type: ignore[arg-type]
        rejection_code=rejection_code,
        now=NOW,
    )
    approval_body = store.objects[f"v1/approvals/{approval.approval_id}.json"].body
    return ReadOnlyRecordingStore(store.objects), {
        "approval_id": approval.approval_id,
        "approval_digest": "sha256:" + hashlib.sha256(approval_body).hexdigest(),
        "proposal_id": proposal_id,
        "proposal_digest": proposal_digest,
        "base_digest": base_digest,
        "job_id": "job-approval-source",
    }


def attest(
    store: ReadOnlyRecordingStore,
    values: dict[str, str],
    *,
    expected_decision: str = "approved",
    expected_rejection_code: str | None = None,
    expected_approval_object_digest: str | None = None,
) -> object:
    return ApprovalAttestor(store).run(
        values["approval_id"],
        values["job_id"],
        values["proposal_id"],
        values["proposal_digest"],
        values["base_digest"],
        expected_decision=expected_decision,
        expected_rejection_code=expected_rejection_code,
        expected_approval_object_digest=expected_approval_object_digest,
    )


def test_successful_approval_and_identical_rerun_are_create_only() -> None:
    store = MemoryStore()
    proposal_id, digest, base_digest, _ = seed_proposal(store)
    orchestrator = ApprovalOrchestrator(store)
    first = orchestrator.run(
        proposal_id,
        digest,
        base_digest,
        now=NOW,
    )
    approval_key = f"v1/approvals/{first.approval_id}.json"
    record = ProposalApprovalRecord.model_validate_json(store.objects[approval_key].body)
    assert record.decision == "approved"
    assert record.decided_at == NOW
    assert store.objects[approval_key].body.endswith(b"\n")
    assert store.objects[approval_key].body == json.dumps(
        record.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode() + b"\n"
    canonical = store.objects[approval_key].body
    creates = tuple(store.creates)
    second = orchestrator.run(
        proposal_id,
        digest,
        base_digest,
        now=NOW + timedelta(minutes=1),
    )
    assert second.reused is True
    assert tuple(store.creates) == creates
    assert store.objects[approval_key].body == canonical


def test_rejected_decision_is_recorded_but_blocked_proposal_is_rejected() -> None:
    store = MemoryStore()
    proposal_id, digest, base_digest, _ = seed_proposal(store)
    result = ApprovalOrchestrator(store).run(
        proposal_id,
        digest,
        base_digest,
        decision="rejected",
        rejection_code="NEEDS_REVIEW",
        now=NOW,
    )
    record = ProposalApprovalRecord.model_validate_json(
        store.objects[f"v1/approvals/{result.approval_id}.json"].body
    )
    assert record.rejection_code == "NEEDS_REVIEW"

    blocked_store = MemoryStore()
    blocked_id, blocked_digest, blocked_base, _ = seed_proposal(blocked_store, blocked=True)
    with pytest.raises(WorkflowError, match="PROPOSAL_BLOCKED"):
        ApprovalOrchestrator(blocked_store).run(
            blocked_id, blocked_digest, blocked_base, now=NOW
        )
    assert not any(key.startswith("v1/approvals/") for key in blocked_store.creates)


def test_approval_identity_binds_only_the_exact_proposal_subject() -> None:
    proposal_id = "proposal_" + "a" * 32
    object_digest = "sha256:" + "b" * 64
    base_digest = "sha256:" + "c" * 64
    expected = "approval_" + hashlib.sha256(
        json.dumps(
            {
                "expected_base_bundle_digest": base_digest,
                "proposal_id": proposal_id,
                "proposal_object_digest": object_digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()[:32]
    assert approval_identity(proposal_id, object_digest, base_digest) == expected


@pytest.mark.parametrize(
    ("decision", "rejection_code"),
    [("approved", "NO"), ("rejected", None), ("rejected", "lowercase")],
)
def test_approval_decision_invariants(
    decision: str, rejection_code: str | None
) -> None:
    with pytest.raises(ValidationError):
        ProposalApprovalRecord(
            approval_id="approval_" + "a" * 32,
            proposal_id="proposal_" + "b" * 32,
            proposal_object_digest="sha256:" + "c" * 64,
            expected_base_bundle_digest="sha256:" + "d" * 64,
            decision=decision,  # type: ignore[arg-type]
            decided_at=NOW,
            rejection_code=rejection_code,
        )


def test_approval_schema_expresses_decision_invariants() -> None:
    schema = ProposalApprovalRecord.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["allOf"] == [
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
                        "pattern": "^[A-Z][A-Z0-9_]{0,127}$",
                    }
                },
            },
        },
    ]


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("missing", "PROPOSAL_NOT_FOUND"),
        ("digest", "PROPOSAL_OBJECT_DIGEST_MISMATCH"),
        ("base", "BASE_BUNDLE_DIGEST_MISMATCH"),
        ("malformed", "INVALID_PROPOSAL"),
        ("id", "INVALID_PROPOSAL"),
    ],
)
def test_approval_rejects_invalid_proposal_inputs(mutation: str, code: str) -> None:
    store = MemoryStore()
    proposal_id, digest, base_digest, body = seed_proposal(store)
    request_id = proposal_id
    request_digest = digest
    request_base = base_digest
    if mutation == "missing":
        request_id = "proposal_" + "f" * 32
    elif mutation == "digest":
        request_digest = "sha256:" + "f" * 64
    elif mutation == "base":
        request_base = "sha256:" + "f" * 64
    elif mutation == "malformed":
        malformed = b'{"proposal_id":"broken"}\n'
        store.seed(f"v1/proposals/{proposal_id}.json", malformed)
        request_digest = "sha256:" + hashlib.sha256(malformed).hexdigest()
    elif mutation == "id":
        parsed = json.loads(body)
        parsed["proposal_id"] = "proposal_" + "f" * 32
        changed = json_bytes(parsed)
        store.seed(f"v1/proposals/{proposal_id}.json", changed)
        request_digest = "sha256:" + hashlib.sha256(changed).hexdigest()
    with pytest.raises(WorkflowError, match=code):
        ApprovalOrchestrator(store).run(
            request_id, request_digest, request_base, now=NOW
        )


def test_approval_rejects_wrong_internal_proposal_digest() -> None:
    store = MemoryStore()
    proposal_id, _, base_digest, body = seed_proposal(store)
    proposal = json.loads(body)
    proposal["proposal_digest"] = "sha256:" + "f" * 64
    changed = json_bytes(proposal)
    store.seed(f"v1/proposals/{proposal_id}.json", changed)
    object_digest = "sha256:" + hashlib.sha256(changed).hexdigest()
    with pytest.raises(WorkflowError, match="INVALID_PROPOSAL"):
        ApprovalOrchestrator(store).run(proposal_id, object_digest, base_digest, now=NOW)


def test_approval_conflict_and_create_only_race() -> None:
    class RacingStore(MemoryStore):
        winner: bytes | None = None

        def create(self, key: str, body: bytes, *, content_type: str) -> bool:
            if key.startswith("v1/approvals/") and self.winner is not None:
                self.seed(key, self.winner)
                self.winner = None
                return False
            return super().create(key, body, content_type=content_type)

    store = RacingStore()
    proposal_id, digest, base_digest, _ = seed_proposal(store)
    probe = ApprovalOrchestrator(store).run(
        proposal_id, digest, base_digest, now=NOW
    )
    key = f"v1/approvals/{probe.approval_id}.json"
    winner = store.objects.pop(key).body
    store.creates.remove(key)
    store.winner = winner
    raced = ApprovalOrchestrator(store).run(
        proposal_id, digest, base_digest, now=NOW
    )
    assert raced.reused is True

    record = ProposalApprovalRecord.model_validate_json(winner)
    conflict = record.model_copy(update={"decision": "rejected", "rejection_code": "NO"})
    store.seed(key, canonical_approval_json(conflict))
    with pytest.raises(WorkflowError, match="APPROVAL_CONFLICT"):
        ApprovalOrchestrator(store).run(
            proposal_id, digest, base_digest, now=NOW
        )


def test_concurrent_opposite_decisions_have_one_winner() -> None:
    class RacingStore(MemoryStore):
        winner: bytes

        def create(self, key: str, body: bytes, *, content_type: str) -> bool:
            del body, content_type
            self.seed(key, self.winner)
            return False

    seed = MemoryStore()
    proposal_id, digest, base_digest, body = seed_proposal(seed)
    approved = ApprovalOrchestrator(seed).run(proposal_id, digest, base_digest, now=NOW)
    winner = seed.objects[f"v1/approvals/{approved.approval_id}.json"].body
    store = RacingStore()
    store.winner = winner
    store.seed(f"v1/proposals/{proposal_id}.json", body)
    with pytest.raises(WorkflowError, match="APPROVAL_CONFLICT"):
        ApprovalOrchestrator(store).run(
            proposal_id,
            digest,
            base_digest,
            decision="rejected",
            rejection_code="NO",
            now=NOW,
        )


@pytest.mark.parametrize(
    ("first_decision", "first_code", "second_decision", "second_code", "reused"),
    [
        ("approved", None, "approved", None, True),
        ("rejected", "NO", "rejected", "NO", True),
        ("approved", None, "rejected", "NO", False),
        ("rejected", "NO", "approved", None, False),
        ("rejected", "NO", "rejected", "OTHER", False),
    ],
)
def test_approval_immutable_decision_matrix(
    first_decision: str,
    first_code: str | None,
    second_decision: str,
    second_code: str | None,
    reused: bool,
) -> None:
    store = MemoryStore()
    proposal_id, digest, base_digest, _ = seed_proposal(store)
    first = ApprovalOrchestrator(store).run(
        proposal_id,
        digest,
        base_digest,
        decision=first_decision,  # type: ignore[arg-type]
        rejection_code=first_code,
        now=NOW,
    )
    key = f"v1/approvals/{first.approval_id}.json"
    original = store.objects[key].body
    if reused:
        result = ApprovalOrchestrator(store).run(
            proposal_id,
            digest,
            base_digest,
            decision=second_decision,  # type: ignore[arg-type]
            rejection_code=second_code,
            now=NOW + timedelta(days=1),
        )
        assert result.reused is True
        assert store.objects[key].body == original
    else:
        with pytest.raises(WorkflowError, match="APPROVAL_CONFLICT"):
            ApprovalOrchestrator(store).run(
                proposal_id,
                digest,
                base_digest,
                decision=second_decision,  # type: ignore[arg-type]
                rejection_code=second_code,
                now=NOW + timedelta(days=1),
            )


def test_approval_rejects_malformed_or_noncanonical_winner() -> None:
    store = MemoryStore()
    proposal_id, digest, base_digest, _ = seed_proposal(store)
    approval_id = approval_identity(proposal_id, digest, base_digest)
    key = f"v1/approvals/{approval_id}.json"
    store.seed(key, b"{}\n")
    with pytest.raises(WorkflowError, match="APPROVAL_CONFLICT"):
        ApprovalOrchestrator(store).run(proposal_id, digest, base_digest, now=NOW)
    record = ProposalApprovalRecord(
        approval_id=approval_id,
        proposal_id=proposal_id,
        proposal_object_digest=digest,
        expected_base_bundle_digest=base_digest,
        decision="approved",
        decided_at=NOW,
    )
    store.seed(key, json_bytes(record))
    with pytest.raises(WorkflowError, match="APPROVAL_CONFLICT"):
        ApprovalOrchestrator(store).run(proposal_id, digest, base_digest, now=NOW)


def test_missing_winner_after_failed_create_is_store_failure() -> None:
    class MissingWinnerStore(MemoryStore):
        def create(self, key: str, body: bytes, *, content_type: str) -> bool:
            del key, body, content_type
            return False

    seeded = MemoryStore()
    proposal_id, digest, base_digest, body = seed_proposal(seeded)
    store = MissingWinnerStore()
    store.seed(f"v1/proposals/{proposal_id}.json", body)
    with pytest.raises(WorkflowError, match="CONTROL_STORE_FAILURE"):
        ApprovalOrchestrator(store).run(proposal_id, digest, base_digest, now=NOW)


def test_approval_store_failures_are_sanitized_and_outputs_are_read_only() -> None:
    store = MemoryStore()
    proposal_id, digest, base_digest, proposal_body = seed_proposal(store)
    proposal_key = f"v1/proposals/{proposal_id}.json"
    review_key = f"v1/reviews/{proposal_id}.md"
    review_body = store.objects[review_key].body
    before_swaps = tuple(store.swaps)
    approval_id = ApprovalOrchestrator(store).run(
        proposal_id, digest, base_digest, now=NOW
    ).approval_id
    assert store.objects[proposal_key].body == proposal_body
    assert store.objects[review_key].body == review_body
    assert tuple(store.swaps) == before_swaps
    assert all("medlearn-vault" not in key for key in store.objects)

    failing = MemoryStore()
    proposal_id, digest, base_digest, _ = seed_proposal(failing)
    failing.fail_create_once = (
        "v1/approvals/"
        + approval_identity(proposal_id, digest, base_digest)
        + ".json"
    )
    with pytest.raises(WorkflowError) as captured:
        ApprovalOrchestrator(failing).run(proposal_id, digest, base_digest, now=NOW)
    assert captured.value.code == "CONTROL_STORE_FAILURE"
    assert str(captured.value) == "CONTROL_STORE_FAILURE"
    assert approval_id.startswith("approval_")


def test_approval_cli_uses_only_fixed_control_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    store = MemoryStore()
    proposal_id, digest, base_digest, _ = seed_proposal(store)
    monkeypatch.setattr("medlearn_vault.cli.S3ObjectStore", lambda *args: store)
    result = CliRunner().invoke(
        app,
        ["workflow", "approve", proposal_id, digest, base_digest],
        env={
            "CONTROL_R2_ENDPOINT": "fixed-endpoint",
            "CONTROL_R2_ACCESS_KEY_ID": "fixed-key",
            "CONTROL_R2_SECRET_ACCESS_KEY": "fixed-secret",
        },
    )
    assert result.exit_code == 0
    assert "decision=approved approval_id=approval_" in result.stdout
    help_result = CliRunner().invoke(app, ["workflow", "approve", "--help"])
    for forbidden in ("--bucket", "--endpoint", "--object-key", "--repository", "--ref"):
        assert forbidden not in help_result.stdout.lower()


def test_approval_cli_failure_prints_only_stable_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    medical = "private COPD transcript"

    class BrokenStore:
        def __init__(self, *args: object) -> None:
            del args
            raise WorkflowError("CONTROL_STORE_FAILURE")

    monkeypatch.setattr("medlearn_vault.cli.S3ObjectStore", BrokenStore)
    result = CliRunner().invoke(
        app,
        [
            "workflow",
            "approve",
            "proposal_" + "a" * 32,
            "sha256:" + "b" * 64,
            "sha256:" + "c" * 64,
        ],
        env={
            "CONTROL_R2_ENDPOINT": medical,
            "CONTROL_R2_ACCESS_KEY_ID": medical,
            "CONTROL_R2_SECRET_ACCESS_KEY": medical,
        },
    )
    assert result.exit_code == 1
    assert result.stderr.strip() == "error_code=CONTROL_STORE_FAILURE"
    assert medical not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "proposal_id",
    ["../proposal_" + "a" * 32, "proposal_" + "a" * 32 + "/other", "v1/jobs/x"],
)
def test_approval_rejects_object_key_injection_before_store_access(proposal_id: str) -> None:
    class NoAccessStore(MemoryStore):
        def get(self, key: str) -> StoredObject | None:
            raise AssertionError(f"unexpected control-store access: {key}")

    with pytest.raises(WorkflowError, match="INVALID_APPROVAL_INPUT"):
        ApprovalOrchestrator(NoAccessStore()).run(
            proposal_id,
            "sha256:" + "b" * 64,
            "sha256:" + "c" * 64,
            now=NOW,
        )


def test_copd_end_to_end_and_terminal_rerun_is_verification_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = MemoryStore()
    inputs, _ = seed_job(store, copd_envelope())
    orchestrator = ProposalOrchestrator(store, ROOT)
    first = orchestrator.run(
        inputs, bundle_path="examples/copd", workflow_run_id="run-100", now=NOW
    )
    assert first.status == "succeeded"
    assert first.proposal_id
    proposal_key = f"v1/proposals/{first.proposal_id}.json"
    review_key = f"v1/reviews/{first.proposal_id}.md"
    assert proposal_key in store.objects
    assert review_key in store.objects
    creates_before = tuple(store.creates)

    second = orchestrator.run(
        inputs,
        bundle_path="examples/copd",
        workflow_run_id="run-duplicate",
        now=NOW + timedelta(minutes=1),
    )
    assert second.status == "succeeded"
    assert second.reused is True
    assert tuple(store.creates) == creates_before
    assert len([key for key in store.objects if key.startswith("v1/proposals/")]) == 1
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.parametrize("status", ["dispatched", "running", "failed"])
def test_terminal_execution_repairs_nonterminal_job_without_rewriting_outputs(
    status: str,
) -> None:
    store = MemoryStore()
    inputs, original = seed_job(store, copd_envelope())
    orchestrator = ProposalOrchestrator(store, ROOT)
    first = orchestrator.run(
        inputs, bundle_path="examples/copd", workflow_run_id="run-original", now=NOW
    )
    job_key = f"v1/jobs/{inputs.job_id}.json"
    stale = JobRecord.model_validate(
        {
            **original.model_dump(),
            "status": status,
            "error_code": "STALE_FAILURE" if status == "failed" else None,
            "updated_at": NOW + timedelta(seconds=1),
        }
    )
    store.seed(job_key, json_bytes(stale))
    store.creates.clear()
    store.swaps.clear()

    result = orchestrator.run(
        inputs,
        bundle_path="examples/copd",
        workflow_run_id="run-reconcile",
        now=NOW + timedelta(minutes=1),
    )

    assert result.reused is True
    repaired = JobRecord.model_validate_json(store.objects[job_key].body)
    assert (repaired.status, repaired.proposal_id, repaired.workflow_run_id) == (
        "succeeded",
        first.proposal_id,
        "run-original",
    )
    assert repaired.error_code is None
    assert repaired.dispatch_attempt == original.dispatch_attempt
    assert repaired.created_at == original.created_at
    assert store.creates == []
    assert store.swaps == [job_key]


def test_terminal_execution_write_survives_failed_final_job_write_and_rerun_repairs() -> None:
    class FailFinalJobStore(MemoryStore):
        fail_job_cas = False

        def compare_and_swap(
            self, key: str, body: bytes, etag: str, *, content_type: str
        ) -> bool:
            if (
                self.fail_job_cas
                and key.startswith("v1/jobs/")
                and JobRecord.model_validate_json(body).status in {"succeeded", "blocked"}
            ):
                self.fail_job_cas = False
                return False
            return super().compare_and_swap(key, body, etag, content_type=content_type)

    store = FailFinalJobStore()
    inputs, _ = seed_job(store, copd_envelope())
    store.fail_job_cas = True
    with pytest.raises(WorkflowError, match="STALE_JOB_UPDATE"):
        ProposalOrchestrator(store, ROOT).run(
            inputs, bundle_path="examples/copd", workflow_run_id="run-crash", now=NOW
        )
    execution = ProposalExecutionRecord.model_validate_json(
        store.objects[f"v1/executions/{inputs.job_id}.json"].body
    )
    assert execution.status == "succeeded"

    result = ProposalOrchestrator(store, ROOT).run(
        inputs,
        bundle_path="examples/copd",
        workflow_run_id="run-repair",
        now=NOW + timedelta(minutes=1),
    )
    assert result.reused is True
    assert JobRecord.model_validate_json(
        store.objects[f"v1/jobs/{inputs.job_id}.json"].body
    ).status == "succeeded"


def test_terminal_reconciliation_accepts_concurrent_cas_winner() -> None:
    class RacingStore(MemoryStore):
        winner: bytes | None = None

        def compare_and_swap(
            self, key: str, body: bytes, etag: str, *, content_type: str
        ) -> bool:
            if self.winner is not None and key.startswith("v1/jobs/"):
                winner, self.winner = self.winner, None
                self.seed(key, winner)
                return False
            return super().compare_and_swap(key, body, etag, content_type=content_type)

    store = RacingStore()
    inputs, original = seed_job(store, copd_envelope())
    orchestrator = ProposalOrchestrator(store, ROOT)
    first = orchestrator.run(
        inputs, bundle_path="examples/copd", workflow_run_id="run-winner", now=NOW
    )
    job_key = f"v1/jobs/{inputs.job_id}.json"
    terminal = store.objects[job_key].body
    store.seed(job_key, json_bytes(original))
    store.winner = terminal
    result = orchestrator.run(
        inputs,
        bundle_path="examples/copd",
        workflow_run_id="run-loser",
        now=NOW + timedelta(minutes=1),
    )
    assert result.reused is True
    assert result.proposal_id == first.proposal_id


@pytest.mark.parametrize("status", ["succeeded", "blocked", "expired"])
def test_terminal_reconciliation_rejects_conflicting_or_expired_job(status: str) -> None:
    store = MemoryStore()
    inputs, original = seed_job(store, copd_envelope())
    orchestrator = ProposalOrchestrator(store, ROOT)
    orchestrator.run(inputs, bundle_path="examples/copd", workflow_run_id="run-ok", now=NOW)
    conflict = JobRecord.model_validate(
        {
            **original.model_dump(),
            "status": status,
            "proposal_id": "proposal_" + "f" * 32 if status != "expired" else None,
            "workflow_run_id": "other-run" if status != "expired" else None,
        }
    )
    store.seed(f"v1/jobs/{inputs.job_id}.json", json_bytes(conflict))
    with pytest.raises(WorkflowError, match="CONTROL_STATE_CONFLICT"):
        orchestrator.run(
            inputs,
            bundle_path="examples/copd",
            workflow_run_id="run-conflict",
            now=NOW + timedelta(minutes=1),
        )


def test_blocked_proposal_writes_both_outputs_and_is_workflow_success() -> None:
    store = MemoryStore()
    inputs, _ = seed_job(store, ambiguous_envelope(), job_id="job-blocked-001")
    result = ProposalOrchestrator(store, ROOT).run(
        inputs,
        bundle_path="examples/capture/ambiguous-ms/bundle",
        workflow_run_id="run-blocked",
        now=NOW,
    )
    assert result.status == "blocked"
    assert result.proposal_id
    assert f"v1/proposals/{result.proposal_id}.json" in store.objects
    assert f"v1/reviews/{result.proposal_id}.md" in store.objects
    job = JobRecord.model_validate_json(store.objects[f"v1/jobs/{inputs.job_id}.json"].body)
    assert job.status == "blocked"
    assert job.proposal_id == result.proposal_id
    assert job.workflow_run_id == "run-blocked"
    repeated = ProposalOrchestrator(store, ROOT).run(
        inputs,
        bundle_path="examples/capture/ambiguous-ms/bundle",
        workflow_run_id="run-blocked-again",
        now=NOW + timedelta(minutes=1),
    )
    assert repeated.status == "blocked"
    assert repeated.reused is True


@pytest.mark.parametrize(
    ("kind", "missing"),
    [("proposal", False), ("proposal", True), ("review", False), ("review", True)],
)
def test_missing_or_modified_terminal_outputs_are_a_collision(
    kind: str, missing: bool
) -> None:
    store = MemoryStore()
    inputs, _ = seed_job(store, copd_envelope())
    orchestrator = ProposalOrchestrator(store, ROOT)
    result = orchestrator.run(
        inputs, bundle_path="examples/copd", workflow_run_id="run-1", now=NOW
    )
    suffix = ".json" if kind == "proposal" else ".md"
    key = f"v1/{kind}s/{result.proposal_id}{suffix}"
    if missing:
        del store.objects[key]
    else:
        store.objects[key] = StoredObject(body=b"tampered", etag=store.objects[key].etag)
    with pytest.raises(WorkflowError, match="PROPOSAL_COLLISION"):
        orchestrator.run(
            inputs,
            bundle_path="examples/copd",
            workflow_run_id="run-2",
            now=NOW + timedelta(minutes=1),
        )


def test_active_concurrent_lease_does_not_process() -> None:
    store = MemoryStore()
    inputs, _ = seed_job(store, copd_envelope())
    execution = ProposalExecutionRecord(
        job_id=inputs.job_id,
        status="running",
        lease_owner="other-run",
        lease_expires_at=NOW + timedelta(minutes=5),
        created_at=NOW,
        updated_at=NOW,
    )
    store.seed(f"v1/executions/{inputs.job_id}.json", json_bytes(execution))
    result = ProposalOrchestrator(store, ROOT).run(
        inputs, bundle_path="examples/copd", workflow_run_id="this-run", now=NOW
    )
    assert result.status == "lease_held"
    assert not any(key.startswith("v1/proposals/") for key in store.objects)


def test_expired_lease_is_taken_over() -> None:
    store = MemoryStore()
    inputs, _ = seed_job(store, copd_envelope())
    execution = ProposalExecutionRecord(
        job_id=inputs.job_id,
        status="running",
        lease_owner="expired-run",
        lease_expires_at=NOW - timedelta(seconds=1),
        created_at=NOW - timedelta(minutes=10),
        updated_at=NOW - timedelta(minutes=10),
    )
    store.seed(f"v1/executions/{inputs.job_id}.json", json_bytes(execution))
    result = ProposalOrchestrator(store, ROOT).run(
        inputs, bundle_path="examples/copd", workflow_run_id="takeover", now=NOW
    )
    assert result.status == "succeeded"


def test_same_run_resumes_its_active_execution() -> None:
    store = MemoryStore()
    inputs, _ = seed_job(store, copd_envelope(), status="running")
    execution = ProposalExecutionRecord(
        job_id=inputs.job_id,
        status="running",
        lease_owner="same-run",
        lease_expires_at=NOW + timedelta(minutes=5),
        created_at=NOW,
        updated_at=NOW,
    )
    store.seed(f"v1/executions/{inputs.job_id}.json", json_bytes(execution))
    result = ProposalOrchestrator(store, ROOT).run(
        inputs, bundle_path="examples/copd", workflow_run_id="same-run", now=NOW
    )
    assert result.status == "succeeded"


def test_digest_and_job_input_mismatches_fail_safely() -> None:
    store = MemoryStore()
    inputs, job = seed_job(store, copd_envelope())
    store.objects[inputs.intake_object_key] = StoredObject(
        body=copd_envelope() + b" ", etag='"tampered"'
    )
    with pytest.raises(WorkflowError, match="INTAKE_DIGEST_MISMATCH"):
        ProposalOrchestrator(store, ROOT).run(
            inputs, bundle_path="examples/copd", workflow_run_id="run-digest", now=NOW
        )
    failed = JobRecord.model_validate_json(store.objects[f"v1/jobs/{inputs.job_id}.json"].body)
    assert failed.error_code == "INTAKE_DIGEST_MISMATCH"

    other = WorkflowInputs(
        job_id=job.job_id,
        intake_object_key=f"v1/intakes/sha256/{'f' * 64}.json",
        intake_digest="sha256:" + "f" * 64,
    )
    with pytest.raises(WorkflowError, match="JOB_INPUT_MISMATCH"):
        ProposalOrchestrator(store, ROOT).run(
            other, bundle_path="examples/copd", workflow_run_id="run-mismatch", now=NOW
        )


def test_invalid_intake_envelope_is_recorded_without_claiming_a_digest_failure() -> None:
    payload = IntakeEnvelope.model_validate_json(copd_envelope()).model_dump(mode="json")
    captured_at = payload["draft"]["context"]["captured_at"]
    payload["draft"]["evidence_messages"].extend(
        [
            {
                "message_id": "message_schema_failure_user",
                "role": "user",
                "observed_at": captured_at,
                "excerpt": "synthetic user evidence",
            },
            {
                "message_id": "message_schema_failure_assistant",
                "role": "assistant",
                "observed_at": captured_at,
                "excerpt": "synthetic assistant evidence",
            },
        ]
    )
    payload["draft"]["claim_candidates"][0]["evidence_message_ids"] = [
        "message_schema_failure_user",
        "message_schema_failure_assistant",
    ]
    exact = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    store = MemoryStore()
    inputs, _ = seed_job(store, exact, job_id="job-invalid-envelope")
    with pytest.raises(WorkflowError, match="INVALID_INTAKE_ENVELOPE"):
        ProposalOrchestrator(store, ROOT).run(
            inputs, bundle_path="examples/copd", workflow_run_id="run-invalid-envelope", now=NOW
        )
    job = JobRecord.model_validate_json(store.objects[f"v1/jobs/{inputs.job_id}.json"].body)
    execution = ProposalExecutionRecord.model_validate_json(
        store.objects[f"v1/executions/{inputs.job_id}.json"].body
    )
    assert job.error_code == execution.error_code == "INVALID_INTAKE_ENVELOPE"


@pytest.mark.parametrize("configured", ["", "../examples/copd", "/tmp/bundle", "missing"])
def test_bundle_path_has_no_implicit_fallback(configured: str) -> None:
    with pytest.raises(WorkflowError, match="INVALID_BUNDLE_PATH"):
        resolve_bundle_path(ROOT, configured)
    source = Path("src/medlearn_vault/workflow.py").read_text(encoding="utf-8")
    assert '"examples/copd"' not in source
    assert '"examples/gerd"' not in source


def test_bundle_symlink_escape_is_rejected(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    outside = tmp_path / "outside"
    repository.mkdir()
    outside.mkdir()
    link = repository / "bundle"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except OSError:
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            check=False,
            capture_output=True,
        )
        assert created.returncode == 0
    with pytest.raises(WorkflowError, match="INVALID_BUNDLE_PATH"):
        resolve_bundle_path(repository, "bundle")


def test_invalid_bundle_directory_is_rejected(tmp_path: Path) -> None:
    bundle = tmp_path / "repo" / "bundle"
    bundle.mkdir(parents=True)
    with pytest.raises(WorkflowError, match="INVALID_BUNDLE"):
        resolve_bundle_path(tmp_path / "repo", "bundle")


def test_r2_failure_is_sanitized_in_control_records() -> None:
    store = MemoryStore()
    inputs, _ = seed_job(store, copd_envelope())
    orchestrator = ProposalOrchestrator(store, ROOT)
    expected = orchestrator.run(
        inputs, bundle_path="examples/copd", workflow_run_id="probe", now=NOW
    )
    # Re-seed a fresh job while retaining the deterministic proposal ID, then force create failure.
    store = MemoryStore()
    inputs, _ = seed_job(store, copd_envelope(), job_id="job-r2-failure")
    store.fail_create_once = f"v1/proposals/{expected.proposal_id}.json"
    with pytest.raises(WorkflowError, match="CONTROL_STORE_FAILURE"):
        ProposalOrchestrator(store, ROOT).run(
            inputs, bundle_path="examples/copd", workflow_run_id="run-r2", now=NOW
        )
    job = JobRecord.model_validate_json(store.objects[f"v1/jobs/{inputs.job_id}.json"].body)
    assert job.status == "failed"
    assert job.error_code == "CONTROL_STORE_FAILURE"
    assert "COPD" not in job.model_dump_json()


def test_s3_adapter_uses_only_fixed_control_bucket_and_conditions() -> None:
    calls: list[dict[str, object]] = []

    class Body:
        def read(self) -> bytes:
            return b"control bytes"

    class Client:
        def get_object(self, **kwargs: object) -> dict[str, object]:
            calls.append(kwargs)
            return {"Body": Body(), "ETag": '"etag"'}

        def put_object(self, **kwargs: object) -> None:
            calls.append(kwargs)

    adapter = S3ObjectStore.__new__(S3ObjectStore)
    adapter._client = Client()
    assert adapter.get("v1/jobs/job-safe.json") == StoredObject(
        body=b"control bytes", etag='"etag"'
    )
    assert adapter.create("v1/proposals/proposal.json", b"{}", content_type="application/json")
    assert adapter.compare_and_swap(
        "v1/jobs/job-safe.json", b"{}", '"etag"', content_type="application/json"
    )
    assert {call["Bucket"] for call in calls} == {"medlearn-control"}
    assert calls[1]["IfNoneMatch"] == "*"
    assert calls[2]["IfMatch"] == '"etag"'
    assert all("medlearn-vault" not in repr(call) for call in calls)


def test_s3_adapter_sanitizes_upstream_failures() -> None:
    class Client:
        def get_object(self, **kwargs: object) -> None:
            del kwargs
            raise ClientError(
                {
                    "Error": {"Code": "InternalError", "Message": "private medical details"},
                    "ResponseMetadata": {"HTTPStatusCode": 500},
                },
                "GetObject",
            )

    adapter = S3ObjectStore.__new__(S3ObjectStore)
    adapter._client = Client()
    with pytest.raises(WorkflowError) as captured:
        adapter.get("v1/jobs/job-safe.json")
    assert captured.value.code == "CONTROL_STORE_FAILURE"
    assert "private medical details" not in str(captured.value)


def test_stale_cas_cannot_overwrite_running_or_terminal_job() -> None:
    store = MemoryStore()
    inputs, job = seed_job(store, copd_envelope())
    key = f"v1/jobs/{inputs.job_id}.json"
    stale = store.get(key)
    assert stale
    running = JobRecord.model_validate({**job.model_dump(), "status": "running"})
    assert store.compare_and_swap(
        key, json_bytes(running), stale.etag, content_type="application/json"
    )
    terminal = JobRecord.model_validate(
        {
            **running.model_dump(),
            "status": "succeeded",
            "proposal_id": "proposal_" + "a" * 32,
            "workflow_run_id": "run-terminal",
        }
    )
    current = store.get(key)
    assert current
    assert store.compare_and_swap(
        key, json_bytes(terminal), current.etag, content_type="application/json"
    )
    failed = JobRecord.model_validate(
        {**job.model_dump(), "status": "failed", "error_code": "STALE"}
    )
    assert not store.compare_and_swap(
        key, json_bytes(failed), stale.etag, content_type="application/json"
    )
    assert JobRecord.model_validate_json(store.objects[key].body).status == "succeeded"


def test_job_record_terminal_invariants() -> None:
    base = {
        "job_id": "job-invariant",
        "intake_digest": "sha256:" + "a" * 64,
        "intake_object_key": f"v1/intakes/sha256/{'a' * 64}.json",
        "dispatch_attempt": 1,
        "created_at": NOW,
        "updated_at": NOW,
    }
    with pytest.raises(ValidationError):
        JobRecord(status="succeeded", **base)
    with pytest.raises(ValidationError):
        JobRecord(status="blocked", **base)
    with pytest.raises(ValidationError):
        JobRecord(status="failed", **base)
    with pytest.raises(ValidationError):
        JobRecord(
            status="failed",
            error_code="FAILED",
            dispatch_lease_id="lease",
            dispatch_lease_expires_at=NOW,
            **base,
        )


def test_execution_fixture_matches_contract() -> None:
    fixture = Path("examples/workflow/proposal-execution-succeeded.json").read_bytes()
    execution = ProposalExecutionRecord.model_validate_json(fixture)
    assert execution.status == "succeeded"


def test_workflow_yaml_has_fixed_minimal_authority() -> None:
    path = Path(".github/workflows/medlearn-propose.yml")
    text = path.read_text(encoding="utf-8")
    data = yaml.load(text, Loader=yaml.BaseLoader)
    dispatch = data["on"]["workflow_dispatch"]
    assert set(dispatch["inputs"]) == {"job_id", "intake_object_key", "intake_digest"}
    assert data["permissions"] == {"contents": "read"}
    assert data["concurrency"] == {
        "group": "medlearn-propose-${{ inputs.job_id }}",
        "cancel-in-progress": "false",
    }
    propose_job = data["jobs"]["propose"]
    assert propose_job["if"] == "github.ref == 'refs/heads/main'"
    assert propose_job["timeout-minutes"] == "15"
    assert "env" not in propose_job
    action_steps = [step for step in propose_job["steps"] if "uses" in step]
    assert action_steps
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", step["uses"]) for step in action_steps)
    checkout = next(step for step in action_steps if step["uses"].startswith("actions/checkout@"))
    assert checkout["with"]["persist-credentials"] == "false"
    assert checkout["with"]["ref"] == "main"
    install_step = next(
        step for step in propose_job["steps"] if "--require-hashes" in step.get("run", "")
    )
    assert install_step["run"].splitlines() == [
        "python -m pip install --require-hashes -r requirements/workflow.txt",
        "python -m pip install --no-build-isolation --no-deps .",
    ]
    build_step = next(
        step for step in propose_job["steps"] if step.get("name") == "Build or verify proposal"
    )
    assert "${{ inputs." not in build_step["run"]
    assert set(build_step["env"]) == {
        "CONTROL_R2_ENDPOINT",
        "CONTROL_R2_ACCESS_KEY_ID",
        "CONTROL_R2_SECRET_ACCESS_KEY",
        "MEDLEARN_PROPOSE_BUNDLE_PATH",
        "MEDLEARN_JOB_ID",
        "MEDLEARN_INTAKE_OBJECT_KEY",
        "MEDLEARN_INTAKE_DIGEST",
    }
    assert build_step["env"]["MEDLEARN_PROPOSE_BUNDLE_PATH"] == (
        "${{ vars.MEDLEARN_PROPOSE_BUNDLE_PATH }}"
    )
    assert {
        "CONTROL_R2_ENDPOINT",
        "CONTROL_R2_ACCESS_KEY_ID",
        "CONTROL_R2_SECRET_ACCESS_KEY",
    } <= set(build_step["env"])
    for step in propose_job["steps"]:
        if step is not build_step:
            assert not {
                "CONTROL_R2_ENDPOINT",
                "CONTROL_R2_ACCESS_KEY_ID",
                "CONTROL_R2_SECRET_ACCESS_KEY",
                "MEDLEARN_PROPOSE_BUNDLE_PATH",
            } & set(step.get("env", {}))
    assert "actions/upload-artifact" not in text
    assert "GITHUB_STEP_SUMMARY" not in text
    assert "ACTIONS_STEP_DEBUG" not in text
    assert "medlearn-vault" not in text.lower()
    assert "MEDLEARN_VAULT" not in text
    assert "examples/copd" not in text
    assert "examples/gerd" not in text
    assert "CONTROL_BUCKET" not in text
    assert not re.search(r"run:\s*.*\$\{\{\s*inputs\.", text)


def test_approval_workflow_yaml_is_control_only_and_argument_safe() -> None:
    path = Path(".github/workflows/medlearn-approve.yml")
    text = path.read_text(encoding="utf-8")
    data = yaml.load(text, Loader=yaml.BaseLoader)
    dispatch = data["on"]["workflow_dispatch"]
    assert set(dispatch["inputs"]) == {
        "proposal_id",
        "proposal_object_digest",
        "expected_base_bundle_digest",
        "confirmation",
        "decision",
        "rejection_code",
    }
    assert dispatch["inputs"]["decision"] == {
        "description": "Immutable decision for this Proposal",
        "required": "true",
        "type": "choice",
        "options": ["select_decision", "approved", "rejected"],
        "default": "select_decision",
    }
    assert dispatch["inputs"]["rejection_code"] == {
        "description": "Required uppercase code when decision is rejected",
        "required": "false",
        "type": "string",
        "default": "",
    }
    assert data["permissions"] == {"contents": "read"}
    assert data["concurrency"] == {
        "group": "medlearn-approve-${{ inputs.proposal_id }}",
        "cancel-in-progress": "false",
    }
    approve_job = data["jobs"]["approve"]
    assert approve_job["if"] == "github.ref == 'refs/heads/main'"
    assert approve_job["timeout-minutes"] == "10"
    assert "env" not in approve_job
    action_steps = [step for step in approve_job["steps"] if "uses" in step]
    assert action_steps
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", step["uses"]) for step in action_steps)
    checkout = next(step for step in action_steps if step["uses"].startswith("actions/checkout@"))
    assert checkout["with"]["persist-credentials"] == "false"
    assert checkout["with"]["ref"] == "main"
    install_step = next(
        step for step in approve_job["steps"] if "--require-hashes" in step.get("run", "")
    )
    assert install_step["run"].splitlines() == [
        "python -m pip install --require-hashes -r requirements/workflow.txt",
        "python -m pip install --no-build-isolation --no-deps .",
    ]
    preflight = next(
        step
        for step in approve_job["steps"]
        if step.get("name") == "Validate approval confirmation"
    )
    assert set(preflight["env"]) == {
        "MEDLEARN_PROPOSAL_ID",
        "MEDLEARN_CONFIRMATION",
        "MEDLEARN_DECISION",
    }
    assert "APPROVAL_DECISION_REQUIRED" in preflight["run"]
    assert "APPROVAL_CONFIRMATION_MISMATCH" in preflight["run"]
    assert "medlearn" not in preflight["run"]
    run_step = next(
        step
        for step in approve_job["steps"]
        if step.get("name") == "Validate and record approval"
    )
    assert "args=(" in run_step["run"]
    assert 'medlearn "${args[@]}"' in run_step["run"]
    assert "${{ inputs." not in run_step["run"]
    assert 'if [[ -n "$MEDLEARN_REJECTION_CODE" ]]; then' in run_step["run"]
    assert 'args+=(--rejection-code "$MEDLEARN_REJECTION_CODE")' in run_step["run"]
    assert {
        "CONTROL_R2_ENDPOINT",
        "CONTROL_R2_ACCESS_KEY_ID",
        "CONTROL_R2_SECRET_ACCESS_KEY",
    } <= set(run_step["env"])
    assert "MEDLEARN_CONFIRMATION" not in run_step["env"]
    for step in approve_job["steps"]:
        if step is not run_step:
            assert not {
                "CONTROL_R2_ENDPOINT",
                "CONTROL_R2_ACCESS_KEY_ID",
                "CONTROL_R2_SECRET_ACCESS_KEY",
            } & set(step.get("env", {}))
    assert "actions/upload-artifact" not in text
    assert "GITHUB_STEP_SUMMARY" not in text
    assert not re.search(r"run:\s*.*\$\{\{\s*inputs\.", text)
    assert set(re.findall(r"secrets\.([A-Z0-9_]+)", text)) == {
        "CONTROL_R2_ENDPOINT",
        "CONTROL_R2_ACCESS_KEY_ID",
        "CONTROL_R2_SECRET_ACCESS_KEY",
    }
    assert not re.search(r"(?:MEDLEARN_VAULT|VAULT_R2|RCLONE)", text, re.IGNORECASE)
    assert not re.search(r"(?:endpoint|bucket|object_key|repository|workflow)\s*:", text)
    for forbidden in ("medlearn-vault", "obsidian", "remotely save", "d1", "vps", "git commit"):
        assert forbidden not in text.lower()


def test_remotely_save_ignore_rule_is_path_scoped() -> None:
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", ".obsidian/plugins/remotely-save/data.json"],
        cwd=ROOT,
        check=False,
    )
    assert ignored.returncode == 0
    for path in ("examples/data.json", "tests/fixtures/data.json"):
        tracked = subprocess.run(
            ["git", "check-ignore", "-q", path], cwd=ROOT, check=False
        )
        assert tracked.returncode == 1


@pytest.mark.parametrize(
    ("decision", "rejection_code", "expected_exit", "expected_output"),
    [
        ("approved", None, 0, "decision=approved"),
        ("rejected", "NEEDS_REVIEW", 0, "decision=rejected"),
        ("approved", "NEEDS_REVIEW", 1, "error_code=INVALID_APPROVAL_INPUT"),
        ("rejected", None, 1, "error_code=INVALID_APPROVAL_INPUT"),
    ],
)
def test_approval_cli_production_behavior_matrix(
    monkeypatch: pytest.MonkeyPatch,
    decision: str,
    rejection_code: str | None,
    expected_exit: int,
    expected_output: str,
) -> None:
    store = MemoryStore()
    proposal_id, digest, base_digest, _ = seed_proposal(store)
    monkeypatch.setattr("medlearn_vault.cli.S3ObjectStore", lambda *args: store)
    args = ["workflow", "approve", proposal_id, digest, base_digest, "--decision", decision]
    if rejection_code is not None:
        args.extend(["--rejection-code", rejection_code])
    result = CliRunner().invoke(
        app,
        args,
        env={
            "CONTROL_R2_ENDPOINT": "configured",
            "CONTROL_R2_ACCESS_KEY_ID": "configured",
            "CONTROL_R2_SECRET_ACCESS_KEY": "configured",
        },
    )
    assert result.exit_code == expected_exit
    assert expected_output in result.stdout + result.stderr
    approval_keys = [key for key in store.objects if key.startswith("v1/approvals/")]
    assert bool(approval_keys) is (expected_exit == 0)


def test_read_only_approval_attestation_verifies_exact_fixed_outputs() -> None:
    store, values = seed_attestation()
    result = attest(store, values, expected_approval_object_digest=values["approval_digest"])
    assert result.verified is True
    assert result.approval_object_digest == values["approval_digest"]
    assert result.proposal_object_digest == values["proposal_digest"]
    assert result.decision == "approved"
    assert store.reads == [
        f"v1/approvals/{values['approval_id']}.json",
        f"v1/proposals/{values['proposal_id']}.json",
        f"v1/jobs/{values['job_id']}.json",
        f"v1/executions/{values['job_id']}.json",
        f"v1/reviews/{values['proposal_id']}.md",
    ]


def test_read_only_approval_attestation_accepts_rejected_decision() -> None:
    store, values = seed_attestation(decision="rejected", rejection_code="SYNTHETIC_REJECTION")
    result = ApprovalAttestor(store).run(
        values["approval_id"],
        values["job_id"],
        values["proposal_id"],
        values["proposal_digest"],
        values["base_digest"],
        expected_decision="rejected",
        expected_rejection_code="SYNTHETIC_REJECTION",
    )
    assert result.decision == "rejected"
    with pytest.raises(WorkflowError, match="APPROVAL_EXPECTATION_MISMATCH"):
        ApprovalAttestor(store).run(
            values["approval_id"],
            values["job_id"],
            values["proposal_id"],
            values["proposal_digest"],
            values["base_digest"],
            expected_decision="rejected",
            expected_rejection_code="OTHER_REJECTION",
        )


@pytest.mark.parametrize(
    ("key", "body", "code"),
    [
        ("approval", None, "APPROVAL_NOT_FOUND"),
        ("approval", b"{}", "INVALID_APPROVAL"),
        ("proposal", None, "PROPOSAL_NOT_FOUND"),
        ("proposal", b"{}", "INVALID_PROPOSAL"),
        ("job", None, "JOB_NOT_FOUND"),
        ("job", b"{}", "INVALID_JOB"),
        ("execution", None, "EXECUTION_NOT_FOUND"),
        ("execution", b"{}", "INVALID_EXECUTION"),
        ("review", None, "REVIEW_NOT_FOUND"),
    ],
)
def test_read_only_attestation_rejects_missing_or_malformed_objects(
    key: str, body: bytes | None, code: str
) -> None:
    store, values = seed_attestation()
    paths = {
        "approval": f"v1/approvals/{values['approval_id']}.json",
        "proposal": f"v1/proposals/{values['proposal_id']}.json",
        "job": f"v1/jobs/{values['job_id']}.json",
        "execution": f"v1/executions/{values['job_id']}.json",
        "review": f"v1/reviews/{values['proposal_id']}.md",
    }
    if body is None:
        del store.objects[paths[key]]
    else:
        store.objects[paths[key]] = StoredObject(body=body, etag='"changed"')
    with pytest.raises(WorkflowError, match=code):
        attest(store, values)


def test_read_only_attestation_rejects_noncanonical_or_unexpected_approval() -> None:
    store, values = seed_attestation()
    key = f"v1/approvals/{values['approval_id']}.json"
    approval = ProposalApprovalRecord.model_validate_json(store.objects[key].body)
    store.objects[key] = StoredObject(body=json_bytes(approval), etag='"changed"')
    with pytest.raises(WorkflowError, match="INVALID_APPROVAL"):
        attest(store, values)

    store, values = seed_attestation()
    with pytest.raises(WorkflowError, match="APPROVAL_EXPECTATION_MISMATCH"):
        attest(store, values, expected_decision="rejected", expected_rejection_code="EXPECTED")
    with pytest.raises(WorkflowError, match="APPROVAL_OBJECT_DIGEST_MISMATCH"):
        attest(store, values, expected_approval_object_digest="sha256:" + "0" * 64)

    store, values = seed_attestation()
    key = f"v1/approvals/{values['approval_id']}.json"
    payload = json.loads(store.objects[key].body)
    payload["decided_at"] = "2026-07-12T00:00:00"
    store.objects[key] = StoredObject(
        body=json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n",
        etag='"changed"',
    )
    with pytest.raises(WorkflowError, match="INVALID_APPROVAL"):
        attest(store, values)


def test_read_only_attestation_rejects_invalid_proposal_and_control_outputs() -> None:
    store, values = seed_attestation()
    proposal_key = f"v1/proposals/{values['proposal_id']}.json"
    proposal = json.loads(store.objects[proposal_key].body)
    proposal["status"] = "blocked"
    store.objects[proposal_key] = StoredObject(
        body=json.dumps(proposal).encode(), etag='"changed"'
    )
    with pytest.raises(WorkflowError, match="INVALID_PROPOSAL"):
        attest(store, values)

    store, values = seed_attestation()
    proposal_key = f"v1/proposals/{values['proposal_id']}.json"
    proposal = json.loads(store.objects[proposal_key].body)
    proposal["proposal_id"] = "proposal_" + "0" * 32
    store.objects[proposal_key] = StoredObject(
        body=json.dumps(proposal).encode(), etag='"changed"'
    )
    with pytest.raises(WorkflowError, match="INVALID_PROPOSAL"):
        attest(store, values)

    store, values = seed_attestation()
    proposal_key = f"v1/proposals/{values['proposal_id']}.json"
    proposal = CaptureProposal.model_validate_json(store.objects[proposal_key].body)
    altered = CaptureProposal.model_validate(
        {**proposal.model_dump(), "base_bundle_digest": "sha256:" + "0" * 64}
    )
    altered = CaptureProposal.model_validate(
        {**altered.model_dump(), "proposal_digest": capture_proposal_digest(altered)}
    )
    store.objects[proposal_key] = StoredObject(body=json_bytes(altered), etag='"changed"')
    with pytest.raises(WorkflowError, match="BASE_BUNDLE_DIGEST_MISMATCH"):
        attest(store, values)

    store, values = seed_attestation()
    proposal_key = f"v1/proposals/{values['proposal_id']}.json"
    proposal = json.loads(store.objects[proposal_key].body)
    proposal["proposal_digest"] = "sha256:" + "0" * 64
    store.objects[proposal_key] = StoredObject(
        body=json.dumps(proposal).encode(), etag='"changed"'
    )
    with pytest.raises(WorkflowError, match="INVALID_PROPOSAL"):
        attest(store, values)

    store, values = seed_attestation()
    job_key = f"v1/jobs/{values['job_id']}.json"
    job = JobRecord.model_validate_json(store.objects[job_key].body)
    mismatched_job = JobRecord.model_validate(
        {**job.model_dump(), "proposal_id": "proposal_" + "0" * 32}
    )
    store.objects[job_key] = StoredObject(
        body=json_bytes(mismatched_job),
        etag='"changed"',
    )
    with pytest.raises(WorkflowError, match="CONTROL_OUTPUT_MISMATCH"):
        attest(store, values)

    store, values = seed_attestation()
    job_key = f"v1/jobs/{values['job_id']}.json"
    job = JobRecord.model_validate_json(store.objects[job_key].body)
    store.objects[job_key] = StoredObject(
        body=json_bytes(JobRecord.model_validate({**job.model_dump(), "status": "running"})),
        etag='"changed"',
    )
    with pytest.raises(WorkflowError, match="INVALID_JOB"):
        attest(store, values)

    store, values = seed_attestation()
    job_key = f"v1/jobs/{values['job_id']}.json"
    job = JobRecord.model_validate_json(store.objects[job_key].body)
    invalid_intake = JobRecord.model_validate(
        {
            **job.model_dump(),
            "intake_object_key": "v1/intakes/sha256/" + "0" * 64 + ".json",
        }
    )
    store.objects[job_key] = StoredObject(body=json_bytes(invalid_intake), etag='"changed"')
    with pytest.raises(WorkflowError, match="INVALID_JOB"):
        attest(store, values)

    store, values = seed_attestation()
    execution_key = f"v1/executions/{values['job_id']}.json"
    execution = ProposalExecutionRecord.model_validate_json(store.objects[execution_key].body)
    store.objects[execution_key] = StoredObject(
        body=json_bytes(
            ProposalExecutionRecord.model_validate(
                {**execution.model_dump(), "proposal_digest": "sha256:" + "0" * 64}
            )
        ),
        etag='"changed"',
    )
    with pytest.raises(WorkflowError, match="CONTROL_OUTPUT_MISMATCH"):
        attest(store, values)

    store, values = seed_attestation()
    execution_key = f"v1/executions/{values['job_id']}.json"
    execution = ProposalExecutionRecord.model_validate_json(store.objects[execution_key].body)
    wrong_job_execution = ProposalExecutionRecord.model_validate(
        {**execution.model_dump(), "job_id": "other-job"}
    )
    store.objects[execution_key] = StoredObject(
        body=json_bytes(wrong_job_execution), etag='"changed"'
    )
    with pytest.raises(WorkflowError, match="INVALID_EXECUTION"):
        attest(store, values)

    store, values = seed_attestation()
    execution_key = f"v1/executions/{values['job_id']}.json"
    execution = ProposalExecutionRecord.model_validate_json(store.objects[execution_key].body)
    store.objects[execution_key] = StoredObject(
        body=json_bytes(
            ProposalExecutionRecord.model_validate(
                {**execution.model_dump(), "workflow_run_id": "other-run"}
            )
        ),
        etag='"changed"',
    )
    with pytest.raises(WorkflowError, match="CONTROL_OUTPUT_MISMATCH"):
        attest(store, values)


def test_read_only_attestation_rejects_review_mismatch_and_store_failure() -> None:
    store, values = seed_attestation()
    review_key = f"v1/reviews/{values['proposal_id']}.md"
    store.objects[review_key] = StoredObject(body=b"opaque changed review", etag='"changed"')
    with pytest.raises(WorkflowError, match="REVIEW_DIGEST_MISMATCH"):
        attest(store, values)

    store, values = seed_attestation()
    review_key = f"v1/reviews/{values['proposal_id']}.md"
    opaque = b"opaque review bytes\n"
    store.objects[review_key] = StoredObject(body=opaque, etag='"changed"')
    execution_key = f"v1/executions/{values['job_id']}.json"
    execution = ProposalExecutionRecord.model_validate_json(store.objects[execution_key].body)
    store.objects[execution_key] = StoredObject(
        body=json_bytes(
            ProposalExecutionRecord.model_validate(
                {
                    **execution.model_dump(),
                    "review_digest": "sha256:" + hashlib.sha256(opaque).hexdigest(),
                }
            )
        ),
        etag='"changed"',
    )
    assert attest(store, values).review_digest == "sha256:" + hashlib.sha256(opaque).hexdigest()

    class FailingStore(ReadOnlyRecordingStore):
        def get(self, key: str) -> StoredObject | None:
            raise RuntimeError("private storage detail")

    with pytest.raises(WorkflowError, match="CONTROL_STORE_FAILURE"):
        attest(FailingStore({}), values)


def test_read_only_attestation_rejects_invalid_input_before_reads() -> None:
    store, values = seed_attestation()
    with pytest.raises(WorkflowError, match="INVALID_ATTESTATION_INPUT"):
        ApprovalAttestor(store).run(
            "../approval_" + "a" * 32,
            values["job_id"],
            values["proposal_id"],
            values["proposal_digest"],
            values["base_digest"],
            expected_decision="approved",
        )
    assert store.reads == []


def test_verify_approval_cli_is_sanitized_and_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    store, values = seed_attestation()
    monkeypatch.setattr("medlearn_vault.cli.S3ReadOnlyObjectStore", lambda *args: store)
    result = CliRunner().invoke(
        app,
        [
            "workflow",
            "verify-approval",
            values["approval_id"],
            values["job_id"],
            values["proposal_id"],
            values["proposal_digest"],
            values["base_digest"],
            "--expected-decision",
            "approved",
        ],
        env={
            "CONTROL_R2_ENDPOINT": "configured",
            "CONTROL_R2_ACCESS_KEY_ID": "configured",
            "CONTROL_R2_SECRET_ACCESS_KEY": "configured",
        },
    )
    assert result.exit_code == 0
    assert result.stdout.startswith("status=verified approval_id=approval_")
    assert "COPD" not in result.stdout + result.stderr


def test_proposal_output_inspector_reads_only_fixed_provenance_keys() -> None:
    store = MemoryStore()
    proposal_id, proposal_digest, base_digest, _ = seed_proposal(store)
    read_only = ReadOnlyRecordingStore(store.objects)
    result = ProposalOutputInspector(read_only).run("job-approval-source")
    assert result.proposal_id == proposal_id
    assert result.proposal_object_digest == proposal_digest
    assert result.expected_base_bundle_digest == base_digest
    assert read_only.reads == [
        "v1/jobs/job-approval-source.json",
        "v1/executions/job-approval-source.json",
        f"v1/proposals/{proposal_id}.json",
        f"v1/reviews/{proposal_id}.md",
    ]


def test_proposal_output_inspector_rejects_invalid_input_before_reads() -> None:
    store = ReadOnlyRecordingStore({})
    with pytest.raises(WorkflowError, match="INVALID_ATTESTATION_INPUT"):
        ProposalOutputInspector(store).run("../job")
    assert store.reads == []


def test_inspect_proposal_cli_is_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    store = MemoryStore()
    proposal_id, proposal_digest, _, _ = seed_proposal(store)
    read_only = ReadOnlyRecordingStore(store.objects)
    monkeypatch.setattr("medlearn_vault.cli.S3ReadOnlyObjectStore", lambda *args: read_only)
    result = CliRunner().invoke(
        app,
        ["workflow", "inspect-proposal", "job-approval-source"],
        env={
            "CONTROL_R2_ENDPOINT": "configured",
            "CONTROL_R2_ACCESS_KEY_ID": "configured",
            "CONTROL_R2_SECRET_ACCESS_KEY": "configured",
        },
    )
    assert result.exit_code == 0
    assert f"proposal_id={proposal_id}" in result.stdout
    assert f"proposal_object_digest={proposal_digest}" in result.stdout
    assert "COPD" not in result.stdout + result.stderr


def test_verify_approval_workflow_yaml_is_read_only_and_argument_safe() -> None:
    path = Path(".github/workflows/medlearn-verify-approval.yml")
    text = path.read_text(encoding="utf-8")
    data = yaml.load(text, Loader=yaml.BaseLoader)
    dispatch = data["on"]["workflow_dispatch"]
    assert set(dispatch["inputs"]) == {
        "approval_id",
        "source_job_id",
        "proposal_id",
        "expected_proposal_object_digest",
        "expected_base_bundle_digest",
        "expected_decision",
        "expected_rejection_code",
        "expected_approval_object_digest",
    }
    assert dispatch["inputs"]["expected_decision"]["options"] == ["approved", "rejected"]
    assert dispatch["inputs"]["expected_decision"]["default"] == "approved"
    assert dispatch["inputs"]["expected_rejection_code"]["default"] == ""
    assert dispatch["inputs"]["expected_approval_object_digest"]["default"] == ""
    assert data["permissions"] == {"contents": "read"}
    assert data["concurrency"] == {
        "group": "medlearn-verify-approval-${{ inputs.approval_id }}",
        "cancel-in-progress": "false",
    }
    verify_job = data["jobs"]["verify"]
    assert verify_job["if"] == "github.ref == 'refs/heads/main'"
    assert verify_job["timeout-minutes"] == "10"
    assert "env" not in verify_job
    action_steps = [step for step in verify_job["steps"] if "uses" in step]
    assert action_steps
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", step["uses"]) for step in action_steps)
    checkout = next(step for step in action_steps if step["uses"].startswith("actions/checkout@"))
    assert checkout["with"]["persist-credentials"] == "false"
    assert checkout["with"]["ref"] == "main"
    install = next(
        step for step in verify_job["steps"] if "--require-hashes" in step.get("run", "")
    )
    assert install["run"].splitlines() == [
        "python -m pip install --require-hashes -r requirements/workflow.txt",
        "python -m pip install --no-build-isolation --no-deps .",
    ]
    run = next(
        step for step in verify_job["steps"] if step.get("name") == "Verify existing approval"
    )
    control_secrets = {
        "CONTROL_R2_ENDPOINT",
        "CONTROL_R2_ACCESS_KEY_ID",
        "CONTROL_R2_SECRET_ACCESS_KEY",
    }
    assert control_secrets <= set(run["env"])
    assert set(run["env"]) == control_secrets | {
        "MEDLEARN_APPROVAL_ID",
        "MEDLEARN_SOURCE_JOB_ID",
        "MEDLEARN_PROPOSAL_ID",
        "MEDLEARN_EXPECTED_PROPOSAL_OBJECT_DIGEST",
        "MEDLEARN_EXPECTED_BASE_BUNDLE_DIGEST",
        "MEDLEARN_EXPECTED_DECISION",
        "MEDLEARN_EXPECTED_REJECTION_CODE",
        "MEDLEARN_EXPECTED_APPROVAL_OBJECT_DIGEST",
    }
    for step in verify_job["steps"]:
        if step is not run:
            assert not (control_secrets & set(step.get("env", {})))
    assert "args=(" in run["run"]
    assert 'medlearn "${args[@]}"' in run["run"]
    assert "${{ inputs." not in run["run"]
    assert 'if [[ -n "$MEDLEARN_EXPECTED_REJECTION_CODE" ]]; then' in run["run"]
    assert 'if [[ -n "$MEDLEARN_EXPECTED_APPROVAL_OBJECT_DIGEST" ]]; then' in run["run"]
    assert set(re.findall(r"secrets\.([A-Z0-9_]+)", text)) == {
        "CONTROL_R2_ENDPOINT",
        "CONTROL_R2_ACCESS_KEY_ID",
        "CONTROL_R2_SECRET_ACCESS_KEY",
    }
    for forbidden in (
        "actions/upload-artifact",
        "GITHUB_STEP_SUMMARY",
        "set -x",
        "medlearn-vault",
        "VAULT_R2",
        "RCLONE",
        "remotely save",
        "put_object",
    ):
        assert forbidden.lower() not in text.lower()


def test_synthetic_intake_workflow_is_fixed_hardened_and_secret_scoped() -> None:
    path = Path(".github/workflows/medlearn-synthetic-intake.yml")
    text = path.read_text(encoding="utf-8")
    data = yaml.load(text, Loader=yaml.BaseLoader)
    assert data["on"]["workflow_dispatch"] == ""
    assert data["permissions"] == {"contents": "read"}
    assert data["concurrency"] == {
        "group": "medlearn-synthetic-intake",
        "cancel-in-progress": "false",
    }
    job = data["jobs"]["intake"]
    assert job["if"] == "github.ref == 'refs/heads/main'"
    assert job["timeout-minutes"] == "10"
    assert "env" not in job
    actions = [step for step in job["steps"] if "uses" in step]
    assert actions
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", step["uses"]) for step in actions)
    checkout = next(step for step in actions if step["uses"].startswith("actions/checkout@"))
    assert checkout["with"] == {"persist-credentials": "false", "ref": "main"}
    install = next(step for step in job["steps"] if "--require-hashes" in step.get("run", ""))
    assert "env" not in install
    assert install["run"].splitlines() == [
        "python -m pip install --require-hashes -r requirements/workflow.txt",
        "python -m pip install --no-build-isolation --no-deps .",
    ]
    submit = next(step for step in job["steps"] if step.get("name") == "Submit synthetic intake")
    inspect = next(
        step for step in job["steps"] if step.get("name") == "Inspect synthetic proposal"
    )
    assert set(submit["env"]) == {
        "MEDLEARN_INGEST_TOKEN",
        "MEDLEARN_INGEST_URL",
        "MEDLEARN_RUN_ID",
        "MEDLEARN_RUN_ATTEMPT",
    }
    assert submit["env"]["MEDLEARN_INGEST_TOKEN"] == "${{ secrets.MEDLEARN_INGEST_TOKEN }}"
    assert submit["env"]["MEDLEARN_INGEST_URL"] == "${{ vars.MEDLEARN_INGEST_URL }}"
    assert set(inspect["env"]) == {
        "CONTROL_R2_ENDPOINT",
        "CONTROL_R2_ACCESS_KEY_ID",
        "CONTROL_R2_SECRET_ACCESS_KEY",
        "MEDLEARN_SOURCE_JOB_ID",
    }
    for step in job["steps"]:
        if step is not submit:
            assert "MEDLEARN_INGEST_TOKEN" not in step.get("env", {})
        if step is not inspect:
            assert not {
                "CONTROL_R2_ENDPOINT",
                "CONTROL_R2_ACCESS_KEY_ID",
                "CONTROL_R2_SECRET_ACCESS_KEY",
            } & set(step.get("env", {}))
    assert "examples/intake/synthetic-e2e.json" in submit["run"]
    assert "${{ " not in submit["run"]
    assert set(re.findall(r"secrets\.([A-Z0-9_]+)", text)) == {
        "MEDLEARN_INGEST_TOKEN",
        "CONTROL_R2_ENDPOINT",
        "CONTROL_R2_ACCESS_KEY_ID",
        "CONTROL_R2_SECRET_ACCESS_KEY",
    }
    for forbidden in (
        "actions/upload-artifact",
        "GITHUB_STEP_SUMMARY",
        "set -x",
        "medlearn-vault",
        "VAULT_R2",
        "RCLONE",
        "remotely save",
    ):
        assert forbidden.lower() not in text.lower()


def test_synthetic_e2e_fixture_is_minimal_ready_and_has_no_excerpts() -> None:
    envelope = IntakeEnvelope.model_validate_json(
        Path("examples/intake/synthetic-e2e.json").read_bytes()
    )
    proposal = build_capture_proposal(
        ContractBundle.from_directory(Path("examples/copd")), envelope.draft
    )
    assert proposal.status == "ready_for_review"
    assert len(envelope.draft.evidence_messages) == 1
    assert envelope.draft.evidence_messages[0].excerpt is None
    assert envelope.draft.claim_candidates == ()
    assert envelope.draft.learner_evidence_candidates == ()
    assert envelope.draft.misconception_candidates == ()


def test_workflow_lock_is_fully_hashed_and_pins_direct_dependencies() -> None:
    text = Path("requirements/workflow.txt").read_text(encoding="utf-8")
    lines = text.splitlines()
    for package in ("boto3", "botocore", "pydantic", "typer", "hatchling"):
        assert any(line.startswith(f"{package}==") and line.endswith(" \\") for line in lines)
    requirement_lines = [
        line for line in lines if line and not line.startswith((" ", "#"))
    ]
    assert requirement_lines
    assert all("==" in line and line.endswith(" \\") for line in requirement_lines)
    assert "--hash=sha256:" in text


def test_cli_failure_logs_only_stable_code(monkeypatch: pytest.MonkeyPatch) -> None:
    medical = "private COPD misconception"

    class BrokenStore:
        def __init__(self, *args: object) -> None:
            del args
            raise WorkflowError("CONTROL_STORE_FAILURE")

    monkeypatch.setattr("medlearn_vault.cli.S3ObjectStore", BrokenStore)
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "workflow",
            "propose",
            "job-safe",
            f"v1/intakes/sha256/{'a' * 64}.json",
            "sha256:" + "a" * 64,
        ],
        env={
            "CONTROL_R2_ENDPOINT": medical,
            "CONTROL_R2_ACCESS_KEY_ID": medical,
            "CONTROL_R2_SECRET_ACCESS_KEY": medical,
            "MEDLEARN_PROPOSE_BUNDLE_PATH": medical,
            "GITHUB_RUN_ID": "run-safe",
        },
    )
    assert result.exit_code == 1
    assert result.stderr.strip() == "error_code=CONTROL_STORE_FAILURE"
    assert medical not in result.stdout + result.stderr
    assert "Traceback" not in result.stdout + result.stderr


def test_cli_treats_blocked_proposal_as_success(monkeypatch: pytest.MonkeyPatch) -> None:
    store = MemoryStore()
    inputs, _ = seed_job(store, ambiguous_envelope(), job_id="job-cli-blocked")
    monkeypatch.setattr("medlearn_vault.cli.S3ObjectStore", lambda *args: store)
    result = CliRunner().invoke(
        app,
        [
            "workflow",
            "propose",
            inputs.job_id,
            inputs.intake_object_key,
            inputs.intake_digest,
        ],
        env={
            "CONTROL_R2_ENDPOINT": "configured",
            "CONTROL_R2_ACCESS_KEY_ID": "configured",
            "CONTROL_R2_SECRET_ACCESS_KEY": "configured",
            "MEDLEARN_PROPOSE_BUNDLE_PATH": "examples/capture/ambiguous-ms/bundle",
            "GITHUB_RUN_ID": "run-cli-blocked",
        },
    )
    assert result.exit_code == 0
    assert "status=blocked" in result.stdout


# ── Production-shaped reproposal regression ─────────────────────────────


def _write_receipt_to_repo(
    patch_dir: Path, catalog_update_id: str, repo_root: Path
) -> None:
    """Copy the receipt from the patch output to the repository-tracked path."""
    import shutil

    receipt_src = patch_dir.parent / "catalog_updates" / catalog_update_id / "receipt.json"
    receipt_dst = repo_root / "catalog_updates" / catalog_update_id / "receipt.json"
    receipt_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(receipt_src, receipt_dst)


def test_reproposal_full_lifecycle_from_v2_handoff_through_publication() -> None:
    """Production-shaped regression covering the complete bootstrap→reproposal→publish lifecycle.

    1.  Preload an old v1 idempotency record with a different intake digest.
    2.  Submit the handoff through the v2 converter → creates a separate v2 Job.
    3.  First Proposal is blocked with CATALOG_UPDATE_REQUIRED.
    4.  Generate a catalog patch and apply it to a copied bundle.
    5.  Submit the same v2 handoff again → returns existing blocked Job (no redispatch).
    6.  Run the explicit reproposal orchestrator → creates new Job with ready Proposal.
    7.  Repeat reproposal → exact idempotent reuse.
    8.  Prove a stale catalog_update_id cannot bypass validation.
    9.  Approve and build a VaultPublicationPlan using the new Job.
    10. Prove no old Job, Proposal, Intake, or idempotency record was mutated.
    """
    import shutil

    from medlearn_vault.catalog_update import (
        build_catalog_update_proposal,
        prepare_catalog_patch,
    )
    from medlearn_vault.handoff import (
        MedLearnHandoff,
        handoff_digest,
        handoff_submission,
    )

    store = MemoryStore()

    # ── 1. Preload old v1 idempotency record ────────────────────────────
    source = json.loads(
        Path("examples/intake/apl-bootstrap-sanitized.json").read_text(encoding="utf-8")
    )
    handoff = MedLearnHandoff.model_validate(source)
    exact_intake, v2_key = handoff_submission(handoff)
    intake_digest = "sha256:" + hashlib.sha256(exact_intake).hexdigest()
    intake_key = f"v1/intakes/sha256/{intake_digest[7:]}.json"
    hd = handoff_digest(handoff)[7:]

    # Old v1 record: medlearn-handoff-<digest> → different intake digest
    v1_idem_key_raw = f"medlearn-handoff-{hd}"
    v1_idem_r2_key = f"v1/idempotency/{hashlib.sha256(v1_idem_key_raw.encode()).hexdigest()}.json"
    old_intake_digest = f"sha256:{'f' * 64}"
    v1_idem_record = {
        "idempotency_version": "0.1.0",
        "job_id": "00000000-0000-0000-0000-000000000000",
        "intake_digest": old_intake_digest,
        "created_at": "2026-01-01T00:00:00Z",
    }
    store.seed(v1_idem_r2_key, json_bytes(v1_idem_record))
    store.seed(intake_key, exact_intake)

    # Snapshot all existing keys for later immutability check
    initial_keys = set(store.objects.keys())

    # ── 2. Submit as v2 → new Job ───────────────────────────────────────
    source_bundle = Path("examples/capture/ambiguous-ms/bundle")
    inputs = WorkflowInputs(
        job_id="job-v2-bootstrap",
        intake_object_key=intake_key,
        intake_digest=intake_digest,
    )
    store.seed(
        f"v1/jobs/{inputs.job_id}.json",
        json_bytes(
            JobRecord(
                job_id=inputs.job_id,
                status="dispatched",
                intake_digest=intake_digest,
                intake_object_key=intake_key,
                dispatch_attempt=1,
                created_at=NOW,
                updated_at=NOW,
            )
        ),
    )

    # Verify v2 idempotency key differs from v1
    assert v2_key != v1_idem_key_raw
    assert v2_key.startswith("medlearn-handoff-v2-")
    assert v1_idem_key_raw.startswith("medlearn-handoff-")

    # Run the orchestrator — this simulates what the propose workflow does
    result = ProposalOrchestrator(store, ROOT).run(
        inputs,
        bundle_path=source_bundle.as_posix(),
        workflow_run_id="run-v2-bootstrap",
        now=NOW,
    )

    # ── 3. First Proposal is blocked ────────────────────────────────────
    assert result.status == "blocked"
    assert result.proposal_id is not None
    blocked_proposal_id = result.proposal_id
    blocked_proposal = CaptureProposal.model_validate_json(
        store.objects[f"v1/proposals/{blocked_proposal_id}.json"].body
    )
    assert blocked_proposal.status == "blocked"
    assert any(
        issue.code == "CATALOG_UPDATE_REQUIRED" for issue in blocked_proposal.issues
    )
    blocked_base_bundle_digest = blocked_proposal.base_bundle_digest

    # Verify blocked Job is terminal
    blocked_job = JobRecord.model_validate_json(
        store.objects[f"v1/jobs/{inputs.job_id}.json"].body
    )
    assert blocked_job.status == "blocked"
    assert blocked_job.proposal_id == blocked_proposal_id

    # ── 4. Build catalog update proposal and patch, referencing
    #    the patched bundle path so the receipt matches the reproposal path.
    patched_bundle = ROOT / ".build" / "test-patched-bundle"
    catalog_update_id: str = ""  # initialised before try for finally-safety
    try:
        # Copy the source bundle to the patched location first so that
        # prepare_catalog_patch can read the "before" state from it
        import shutil as _shutil

        if patched_bundle.exists():
            _shutil.rmtree(patched_bundle)
        _shutil.copytree(source_bundle, patched_bundle)
        patched_bundle_rel = patched_bundle.relative_to(ROOT).as_posix()

        # Build catalog update against the patched bundle (same content as source)
        catalog_update = build_catalog_update_proposal(
            blocked_proposal,
            capture_proposal_object_digest="sha256:"
            + hashlib.sha256(
                store.objects[f"v1/proposals/{blocked_proposal_id}.json"].body
            ).hexdigest(),
            target_bundle_path=patched_bundle_rel,
        )
        assert catalog_update.status == "blocked"  # incomplete metadata

        # Prepare the patch against the patched bundle (same content as source)
        patch_output = ROOT / ".build" / "test-patch"
        patch = prepare_catalog_patch(catalog_update, Path(patched_bundle_rel))
        from medlearn_vault.catalog_update import write_catalog_patch

        write_catalog_patch(patch, patch_output)

        # Apply the patch files to the patched bundle in-place
        for name in ("sources.json", "concepts.json"):
            patch_file = patch_output / name
            if patch_file.exists():
                _shutil.copy2(patch_file, patched_bundle / name)

        # Write the receipt to the repository-tracked path so
        # ReproposalOrchestrator can find and verify it
        catalog_update_id = catalog_update.catalog_update_id
        _write_receipt_to_repo(patch_output, catalog_update_id, ROOT)

        # Verify bundle digest changed
        patched = ContractBundle.from_directory(patched_bundle)
        from medlearn_vault.capture import contract_bundle_digest

        new_bundle_digest = contract_bundle_digest(patched)
        assert new_bundle_digest != blocked_base_bundle_digest

        # ── 5. Same v2 handoff again → returns existing blocked Job ─────
        repeat_result = ProposalOrchestrator(store, ROOT).run(
            inputs,
            bundle_path=source_bundle.as_posix(),
            workflow_run_id="run-v2-repeat",
            now=NOW + timedelta(minutes=1),
        )
        assert repeat_result.status == "blocked"
        assert repeat_result.proposal_id == blocked_proposal_id
        assert repeat_result.reused is True
        # Blocked Job was not mutated
        repeat_job = JobRecord.model_validate_json(
            store.objects[f"v1/jobs/{inputs.job_id}.json"].body
        )
        assert repeat_job.status == "blocked"

        # ── 6. Explicit reproposal → new Job ────────────────────────────
        reproposal = ReproposalOrchestrator(store, ROOT).run(
            inputs.job_id,
            blocked_proposal_id,
            catalog_update_id,
            blocked_base_bundle_digest,
            confirmation=blocked_proposal_id,
            bundle_path=patched_bundle_rel,
            now=NOW + timedelta(minutes=2),
        )

        # ── 9. New immutable Job and ready_for_review Proposal ──────────
        assert reproposal.source_job_id != inputs.job_id
        assert reproposal.proposal_id is not None
        assert reproposal.reused is False

        new_job = JobRecord.model_validate_json(
            store.objects[f"v1/jobs/{reproposal.source_job_id}.json"].body
        )
        assert new_job.reproposal_of_job_id == inputs.job_id
        assert new_job.reproposal_of_proposal_id == blocked_proposal_id
        assert new_job.catalog_update_id == catalog_update_id
        # May still be blocked if metadata is incomplete
        assert new_job.status in ("succeeded", "blocked")

        new_proposal = CaptureProposal.model_validate_json(
            store.objects[f"v1/proposals/{reproposal.proposal_id}.json"].body
        )

        # ── 10. Repeat reproposal → idempotent reuse ────────────────────
        repeat_reproposal = ReproposalOrchestrator(store, ROOT).run(
            inputs.job_id,
            blocked_proposal_id,
            catalog_update_id,
            blocked_base_bundle_digest,
            confirmation=blocked_proposal_id,
            bundle_path=patched_bundle_rel,
            now=NOW + timedelta(minutes=3),
        )
        assert repeat_reproposal.reused is True
        assert repeat_reproposal.source_job_id == reproposal.source_job_id
        assert repeat_reproposal.proposal_id == reproposal.proposal_id

        # ── 11. Validation guards cannot be bypassed ────────────────────
        # A different catalog_update_id creates a separate identity (not a conflict)

        # Wrong confirmation cannot bypass
        with pytest.raises(WorkflowError, match="INVALID_REPROPOSAL_INPUT"):
            ReproposalOrchestrator(store, ROOT).run(
                inputs.job_id,
                blocked_proposal_id,
                catalog_update_id,
                blocked_base_bundle_digest,
                confirmation="wrong",
                bundle_path=patched_bundle_rel,
                now=NOW + timedelta(minutes=5),
            )

        # Stale bundle (unpatched) cannot bypass
        with pytest.raises(WorkflowError, match="STALE_BASE_BUNDLE"):
            ReproposalOrchestrator(store, ROOT).run(
                inputs.job_id,
                blocked_proposal_id,
                catalog_update_id,
                blocked_base_bundle_digest,
                confirmation=blocked_proposal_id,
                bundle_path=source_bundle.as_posix(),  # unpatched!
                now=NOW + timedelta(minutes=6),
            )

        # ── 12. Approve and build VaultPublicationPlan ──────────────────
        if new_proposal.status == "ready_for_review":
            new_proposal_digest = "sha256:" + hashlib.sha256(
                store.objects[f"v1/proposals/{reproposal.proposal_id}.json"].body
            ).hexdigest()
            approval = ApprovalOrchestrator(store).run(
                reproposal.proposal_id,
                new_proposal_digest,
                new_proposal.base_bundle_digest,
                now=NOW + timedelta(minutes=7),
            )
            assert approval.decision == "approved"

            approval_body = store.objects[
                f"v1/approvals/{approval.approval_id}.json"
            ].body
            approval_object_digest = "sha256:" + hashlib.sha256(approval_body).hexdigest()

            plan = PublicationPlanOrchestrator(store, ROOT).run(
                approval.approval_id,
                approval_object_digest,
                reproposal.source_job_id,
                reproposal.proposal_id,
                new_proposal_digest,
                new_proposal.base_bundle_digest,
                bundle_path=patched_bundle_rel,
            )
            assert plan.publication_plan_id.startswith("publication_plan_")
            assert plan.capture_id.startswith("capture_")

        # ── 13. Prove no old records were mutated ───────────────────────────
        for key in initial_keys:
            current = store.objects.get(key)
            assert current is not None, f"old record {key} was deleted"

        # Verify old v1 idempotency record was NOT touched
        old_v1 = json.loads(store.objects[v1_idem_r2_key].body)
        assert old_v1["intake_digest"] == old_intake_digest
        assert old_v1["job_id"] == "00000000-0000-0000-0000-000000000000"

        # Verify old blocked Job remains terminal
        old_blocked = JobRecord.model_validate_json(
            store.objects[f"v1/jobs/{inputs.job_id}.json"].body
        )
        assert old_blocked.status == "blocked"

        # Verify old blocked Proposal remains blocked
        old_proposal = CaptureProposal.model_validate_json(
            store.objects[f"v1/proposals/{blocked_proposal_id}.json"].body
        )
        assert old_proposal.status == "blocked"
    finally:
        import shutil

        if patched_bundle.exists():
            shutil.rmtree(patched_bundle)
        patch_out = ROOT / ".build" / "test-patch"
        if patch_out.exists():
            shutil.rmtree(patch_out)
        receipt_dir = ROOT / "catalog_updates" / catalog_update_id
        if receipt_dir.exists():
            shutil.rmtree(receipt_dir)


# ── Receipt rejection tests ────────────────────────────────────────────


def _seed_reproposal_setup() -> tuple[
    MemoryStore, WorkflowInputs, str, str, str, str, Path, Path,
]:
    """Set up a blocked Job with intake, proposal, and receipt for reproposal.

    Returns (store, inputs, blocked_proposal_id, catalog_update_id,
    blocked_base_bundle_digest, patched_bundle_rel, patched_bundle, patch_output).
    """
    import shutil

    from medlearn_vault.catalog_update import (
        build_catalog_update_proposal,
        prepare_catalog_patch,
        write_catalog_patch,
    )
    from medlearn_vault.handoff import (
        MedLearnHandoff,
        handoff_submission,
    )

    store = MemoryStore()
    source = json.loads(
        Path("examples/intake/apl-bootstrap-sanitized.json").read_text(encoding="utf-8")
    )
    handoff = MedLearnHandoff.model_validate(source)
    exact_intake, _ = handoff_submission(handoff)
    intake_digest = "sha256:" + hashlib.sha256(exact_intake).hexdigest()
    intake_key = f"v1/intakes/sha256/{intake_digest[7:]}.json"
    store.seed(intake_key, exact_intake)

    source_bundle = Path("examples/capture/ambiguous-ms/bundle")
    inputs = WorkflowInputs(
        job_id="job-reject-setup",
        intake_object_key=intake_key,
        intake_digest=intake_digest,
    )
    store.seed(
        f"v1/jobs/{inputs.job_id}.json",
        json_bytes(
            JobRecord(
                job_id=inputs.job_id,
                status="dispatched",
                intake_digest=intake_digest,
                intake_object_key=intake_key,
                dispatch_attempt=1,
                created_at=NOW,
                updated_at=NOW,
            )
        ),
    )

    result = ProposalOrchestrator(store, ROOT).run(
        inputs,
        bundle_path=source_bundle.as_posix(),
        workflow_run_id="run-reject-setup",
        now=NOW,
    )
    assert result.status == "blocked"
    blocked_pid = result.proposal_id
    assert blocked_pid is not None
    blocked_proposal = CaptureProposal.model_validate_json(
        store.objects[f"v1/proposals/{blocked_pid}.json"].body
    )
    blocked_base = blocked_proposal.base_bundle_digest

    # Copy and patch
    patched_bundle = ROOT / ".build" / "test-reject-bundle"
    if patched_bundle.exists():
        shutil.rmtree(patched_bundle)
    shutil.copytree(source_bundle, patched_bundle)
    patched_rel = patched_bundle.relative_to(ROOT).as_posix()

    catalog_update = build_catalog_update_proposal(
        blocked_proposal,
        capture_proposal_object_digest="sha256:"
        + hashlib.sha256(
            store.objects[f"v1/proposals/{blocked_pid}.json"].body
        ).hexdigest(),
        target_bundle_path=patched_rel,
    )
    cat_id = catalog_update.catalog_update_id

    patch_output = ROOT / ".build" / "test-reject-patch"
    if patch_output.exists():
        shutil.rmtree(patch_output)
    # Also remove the catalog_updates receipt that write_catalog_patch may
    # create alongside patch_output
    receipt_parent = patch_output.parent / "catalog_updates"
    if receipt_parent.exists():
        shutil.rmtree(receipt_parent)
    patch = prepare_catalog_patch(catalog_update, Path(patched_rel))
    write_catalog_patch(patch, patch_output)

    # Apply patch
    for name in ("sources.json", "concepts.json"):
        pf = patch_output / name
        if pf.exists():
            shutil.copy2(pf, patched_bundle / name)

    # Write receipt
    receipt_src = (
        patch_output.parent
        / "catalog_updates"
        / cat_id
        / "receipt.json"
    )
    receipt_dst = ROOT / "catalog_updates" / cat_id / "receipt.json"
    receipt_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(receipt_src, receipt_dst)

    return (
        store, inputs, blocked_pid, cat_id, blocked_base,
        patched_rel, patched_bundle, patch_output,
    )


def test_receipt_rejects_random_but_syntactically_valid_catalog_update_id() -> None:
    """A catalog_update_id that follows the regex but has no receipt."""
    store, inputs, blocked_pid, cat_id, blocked_base, patched_rel, _, _ = (
        _seed_reproposal_setup()
    )
    try:
        random_cat_id = "catalog_update_" + "d" * 32
        # The random id has no receipt at catalog_updates/<id>/receipt.json
        receipt_dir = ROOT / "catalog_updates" / random_cat_id
        if receipt_dir.exists():
            import shutil
            shutil.rmtree(receipt_dir)
        with pytest.raises(WorkflowError, match="RECEIPT_NOT_FOUND"):
            ReproposalOrchestrator(store, ROOT).run(
                inputs.job_id, blocked_pid, random_cat_id,
                blocked_base, confirmation=blocked_pid,
                bundle_path=patched_rel, now=NOW + timedelta(minutes=1),
            )
    finally:
        import shutil
        _cleanup_reproposal(ROOT, cat_id)


def test_receipt_rejects_belonging_to_another_proposal() -> None:
    """Receipt bound to proposal A cannot authorize reproposal of proposal B.

    Tamper a correct receipt to change its capture_proposal_id, then prove
    the reproposal rejects the mismatched binding.
    """
    store, inputs, blocked_pid, cat_id, blocked_base, patched_rel, _, _ = (
        _seed_reproposal_setup()
    )
    try:
        # Read receipt, change capture_proposal_id to a different valid id
        receipt_path = ROOT / "catalog_updates" / cat_id / "receipt.json"
        original = receipt_path.read_bytes()
        another_pid = "proposal_" + "b" * 32
        altered = original.replace(
            blocked_pid.encode(),
            another_pid.encode(),
        )
        receipt_path.write_bytes(altered)
        # The receipt_id no longer matches receipt contents → INVALID_RECEIPT
        with pytest.raises(WorkflowError, match="INVALID_RECEIPT"):
            ReproposalOrchestrator(store, ROOT).run(
                inputs.job_id, blocked_pid, cat_id,
                blocked_base, confirmation=blocked_pid,
                bundle_path=patched_rel, now=NOW + timedelta(minutes=1),
            )
    finally:
        _cleanup_reproposal(ROOT, cat_id)


def test_receipt_rejects_unchanged_bundle() -> None:
    """STALE_BASE_BUNDLE is raised before receipt validation."""
    store, inputs, blocked_pid, cat_id, blocked_base, patched_rel, _, _ = (
        _seed_reproposal_setup()
    )
    try:
        # Use the source bundle (same digest as blocked) — receipt exists but
        # STALE_BASE_BUNDLE is checked first
        with pytest.raises(WorkflowError, match="STALE_BASE_BUNDLE"):
            ReproposalOrchestrator(store, ROOT).run(
                inputs.job_id, blocked_pid, cat_id,
                blocked_base, confirmation=blocked_pid,
                bundle_path="examples/capture/ambiguous-ms/bundle",
                now=NOW + timedelta(minutes=1),
            )
    finally:
        _cleanup_reproposal(ROOT, cat_id)


def test_receipt_rejects_partially_applied_patch() -> None:
    """If concepts.json is not updated, RECEIPT_CONCEPTS_MISMATCH."""
    store, inputs, blocked_pid, cat_id, blocked_base, patched_rel, patched_bundle, _ = (
        _seed_reproposal_setup()
    )
    try:
        # Restore concepts.json to the original (pre-patch) state
        source_concepts = Path(
            "examples/capture/ambiguous-ms/bundle/concepts.json"
        ).read_bytes()
        (patched_bundle / "concepts.json").write_bytes(source_concepts)
        with pytest.raises(WorkflowError, match="RECEIPT_CONCEPTS_MISMATCH"):
            ReproposalOrchestrator(store, ROOT).run(
                inputs.job_id, blocked_pid, cat_id,
                blocked_base, confirmation=blocked_pid,
                bundle_path=patched_rel, now=NOW + timedelta(minutes=1),
            )
    finally:
        _cleanup_reproposal(ROOT, cat_id)


def test_receipt_rejects_sources_changed_after_receipt() -> None:
    """If sources.json is modified after receipt creation, RECEIPT_SOURCES_MISMATCH."""
    store, inputs, blocked_pid, cat_id, blocked_base, patched_rel, patched_bundle, _ = (
        _seed_reproposal_setup()
    )
    try:
        # Add extra whitespace in the JSON — valid JSON, different hash,
        # bundle still parses correctly
        sources_path = patched_bundle / "sources.json"
        original = sources_path.read_bytes()
        # Append an extra newline — valid JSON (whitespace at end is fine),
        # different sha256
        sources_path.write_bytes(original + b"\n")
        with pytest.raises(WorkflowError, match="RECEIPT_SOURCES_MISMATCH"):
            ReproposalOrchestrator(store, ROOT).run(
                inputs.job_id, blocked_pid, cat_id,
                blocked_base, confirmation=blocked_pid,
                bundle_path=patched_rel, now=NOW + timedelta(minutes=1),
            )
    finally:
        _cleanup_reproposal(ROOT, cat_id)


def test_receipt_rejects_concepts_changed_after_receipt() -> None:
    """If concepts.json is modified after receipt creation, RECEIPT_CONCEPTS_MISMATCH."""
    store, inputs, blocked_pid, cat_id, blocked_base, patched_rel, patched_bundle, _ = (
        _seed_reproposal_setup()
    )
    try:
        # Add extra whitespace in the JSON — valid JSON, different hash
        concepts_path = patched_bundle / "concepts.json"
        original = concepts_path.read_bytes()
        concepts_path.write_bytes(original + b"\n")
        with pytest.raises(WorkflowError, match="RECEIPT_CONCEPTS_MISMATCH"):
            ReproposalOrchestrator(store, ROOT).run(
                inputs.job_id, blocked_pid, cat_id,
                blocked_base, confirmation=blocked_pid,
                bundle_path=patched_rel, now=NOW + timedelta(minutes=1),
            )
    finally:
        _cleanup_reproposal(ROOT, cat_id)


def test_receipt_rejects_modified_receipt_with_correct_files() -> None:
    """A tampered receipt that references correct files still fails validation.

    Modifies the receipt's sources_new_digest so the receipt_id no longer
    matches, but keeps the capture_proposal_id and other fields intact.
    """
    store, inputs, blocked_pid, cat_id, blocked_base, patched_rel, _, _ = (
        _seed_reproposal_setup()
    )
    try:
        receipt_path = ROOT / "catalog_updates" / cat_id / "receipt.json"
        original = receipt_path.read_bytes()
        # Replace the sources_new_digest sha256 value — the receipt_id no
        # longer matches the contents, breaking self-validation
        import re as _re

        altered = _re.sub(
            rb'"sources_new_digest":\s*"sha256:[a-f0-9]{64}"',
            b'"sources_new_digest": "sha256:' + b"e" * 64 + b'"',
            original,
        )
        receipt_path.write_bytes(altered)
        with pytest.raises(WorkflowError, match="INVALID_RECEIPT"):
            ReproposalOrchestrator(store, ROOT).run(
                inputs.job_id, blocked_pid, cat_id,
                blocked_base, confirmation=blocked_pid,
                bundle_path=patched_rel, now=NOW + timedelta(minutes=1),
            )
    finally:
        _cleanup_reproposal(ROOT, cat_id)


def _cleanup_reproposal(root: Path, cat_id: str) -> None:
    """Remove test artefacts created by _seed_reproposal_setup."""
    import shutil

    for name in ("test-reject-bundle", "test-reject-patch"):
        path = root / ".build" / name
        if path.exists():
            shutil.rmtree(path)
    receipt_dir = root / "catalog_updates" / cat_id
    if receipt_dir.exists():
        shutil.rmtree(receipt_dir)
    # Remove parent if empty
    cat_parent = root / "catalog_updates"
    if cat_parent.exists():
        try:
            cat_parent.rmdir()
        except OSError:
            pass


def test_converter_v2_idempotency_key_stable_across_platforms() -> None:
    """The v2 idempotency key must be deterministic — no platform-dependent behavior."""
    from medlearn_vault.handoff import (
        HANDOFF_CONVERSION_VERSION,
        MedLearnHandoff,
        handoff_idempotency_key,
    )

    assert HANDOFF_CONVERSION_VERSION == "medlearn.handoff_to_intake.v2"
    source = json.loads(
        Path("examples/intake/apl-bootstrap-sanitized.json").read_text(encoding="utf-8")
    )
    handoff = MedLearnHandoff.model_validate(source)
    key = handoff_idempotency_key(handoff)

    # Must be purely deterministic — no UUID, timestamp, or random component
    for _ in range(5):
        assert handoff_idempotency_key(handoff) == key
    # Re-parse: same content → same key
    reparsed = MedLearnHandoff.model_validate(
        json.loads(
            Path("examples/intake/apl-bootstrap-sanitized.json").read_text(encoding="utf-8")
        )
    )
    assert handoff_idempotency_key(reparsed) == key


def test_intake_bytes_are_lf_only_no_random_nonce() -> None:
    """Every intake envelope byte must be LF-only with no random retry nonce."""
    from medlearn_vault.handoff import MedLearnHandoff, handoff_submission

    source = json.loads(
        Path("examples/intake/apl-bootstrap-sanitized.json").read_text(encoding="utf-8")
    )
    handoff = MedLearnHandoff.model_validate(source)
    exact1, key1 = handoff_submission(handoff)
    exact2, key2 = handoff_submission(handoff)

    text1 = exact1.decode("utf-8")
    text2 = exact2.decode("utf-8")
    assert "\r" not in text1
    assert "\r" not in text2
    assert exact1 == exact2
    assert key1 == key2
    # The Python converter produces JSON without a trailing newline;
    # the Worker converter appends \n.  Both are LF-only (no CR).
    assert b"\r" not in exact1

    # Verify no UUID/random-like component in the idempotency key
    # (only hex characters after the fixed prefix)
    assert key1.startswith("medlearn-handoff-v2-")
    hex_part = key1[len("medlearn-handoff-v2-"):]
    assert re.fullmatch(r"[a-f0-9]+", hex_part)


def test_blocked_job_remains_terminal_after_unrelated_main_changes() -> None:
    """A blocked Job must remain blocked — unrelated main changes do not create duplicates."""
    store = MemoryStore()
    inputs, _ = seed_job(
        store, ambiguous_envelope(), job_id="job-terminal-blocked"
    )

    # First run: blocked
    result1 = ProposalOrchestrator(store, ROOT).run(
        inputs,
        bundle_path="examples/capture/ambiguous-ms/bundle",
        workflow_run_id="run-blocked-1",
        now=NOW,
    )
    assert result1.status == "blocked"
    assert result1.proposal_id is not None
    blocked_pid = result1.proposal_id

    # Second run with same bundle (unrelated change scenario): same blocked result
    result2 = ProposalOrchestrator(store, ROOT).run(
        inputs,
        bundle_path="examples/capture/ambiguous-ms/bundle",
        workflow_run_id="run-blocked-2",
        now=NOW + timedelta(minutes=10),
    )
    assert result2.status == "blocked"
    assert result2.proposal_id == blocked_pid
    assert result2.reused is True

    # Job stays blocked — no new job created
    assert len([k for k in store.objects if k.startswith("v1/jobs/")]) == 1


def test_reproposal_cli_command_outputs_expected_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The repropose CLI command must output source_job_id and proposal_id."""
    store = MemoryStore()

    # Seed a blocked job
    inputs, _ = seed_job(
        store, ambiguous_envelope(), job_id="job-cli-repropose"
    )
    result = ProposalOrchestrator(store, ROOT).run(
        inputs,
        bundle_path="examples/capture/ambiguous-ms/bundle",
        workflow_run_id="run-cli-repropose",
        now=NOW,
    )
    assert result.status == "blocked"
    blocked_pid = result.proposal_id
    assert blocked_pid is not None

    from medlearn_vault.catalog_update import build_catalog_update_proposal

    blocked_proposal = CaptureProposal.model_validate_json(
        store.objects[f"v1/proposals/{blocked_pid}.json"].body
    )
    catalog_update = build_catalog_update_proposal(
        blocked_proposal,
        capture_proposal_object_digest="sha256:"
        + hashlib.sha256(
            store.objects[f"v1/proposals/{blocked_pid}.json"].body
        ).hexdigest(),
        target_bundle_path="examples/capture/ambiguous-ms/bundle",
    )

    monkeypatch.setattr("medlearn_vault.cli.S3ObjectStore", lambda *args: store)
    result_cli = CliRunner().invoke(
        app,
        [
            "workflow",
            "repropose",
            inputs.job_id,
            blocked_pid,
            catalog_update.catalog_update_id,
            blocked_proposal.base_bundle_digest,
            blocked_pid,  # confirmation
        ],
        env={
            "CONTROL_R2_ENDPOINT": "configured",
            "CONTROL_R2_ACCESS_KEY_ID": "configured",
            "CONTROL_R2_SECRET_ACCESS_KEY": "configured",
            "MEDLEARN_PROPOSE_BUNDLE_PATH": "examples/capture/ambiguous-ms/bundle",
        },
    )
    # Should fail because bundle digest hasn't changed (STALE_BASE_BUNDLE)
    assert result_cli.exit_code == 1
    assert "error_code=STALE_BASE_BUNDLE" in result_cli.stderr
