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

from medlearn_vault.cli import app
from medlearn_vault.workflow import (
    ApprovalOrchestrator,
    JobRecord,
    ObjectStore,
    ProposalApprovalRecord,
    ProposalExecutionRecord,
    ProposalOrchestrator,
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
    assert propose_job["timeout-minutes"] == "15"
    assert set(propose_job["env"]) == {
        "CONTROL_R2_ENDPOINT",
        "CONTROL_R2_ACCESS_KEY_ID",
        "CONTROL_R2_SECRET_ACCESS_KEY",
        "MEDLEARN_PROPOSE_BUNDLE_PATH",
    }
    assert propose_job["env"]["MEDLEARN_PROPOSE_BUNDLE_PATH"] == (
        "${{ vars.MEDLEARN_PROPOSE_BUNDLE_PATH }}"
    )
    action_steps = [step for step in propose_job["steps"] if "uses" in step]
    assert action_steps
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", step["uses"]) for step in action_steps)
    checkout = next(step for step in action_steps if step["uses"].startswith("actions/checkout@"))
    assert checkout["with"]["persist-credentials"] == "false"
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
        "MEDLEARN_JOB_ID",
        "MEDLEARN_INTAKE_OBJECT_KEY",
        "MEDLEARN_INTAKE_DIGEST",
    }
    assert "actions/upload-artifact" not in text
    assert "GITHUB_STEP_SUMMARY" not in text
    assert "ACTIONS_STEP_DEBUG" not in text
    assert "medlearn-vault" not in text.lower()
    assert "MEDLEARN_VAULT" not in text
    assert "examples/copd" not in text
    assert "examples/gerd" not in text
    assert "CONTROL_BUCKET" not in text
    assert not re.search(r"run:\s*.*\$\{\{\s*inputs\.", text)


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
