import hashlib
import json
import os
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
    JobRecord,
    ObjectStore,
    ProposalExecutionRecord,
    ProposalOrchestrator,
    S3ObjectStore,
    StoredObject,
    WorkflowError,
    WorkflowInputs,
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


def test_existing_different_proposal_bytes_are_a_collision() -> None:
    store = MemoryStore()
    inputs, _ = seed_job(store, copd_envelope())
    orchestrator = ProposalOrchestrator(store, ROOT)
    result = orchestrator.run(
        inputs, bundle_path="examples/copd", workflow_run_id="run-1", now=NOW
    )
    key = f"v1/proposals/{result.proposal_id}.json"
    store.objects[key] = StoredObject(body=b'{"tampered":true}', etag=store.objects[key].etag)
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
        "group": "${{ inputs.job_id }}",
        "cancel-in-progress": "false",
    }
    propose_job = data["jobs"]["propose"]
    assert set(propose_job["env"]) == {
        "CONTROL_R2_ENDPOINT",
        "CONTROL_R2_ACCESS_KEY_ID",
        "CONTROL_R2_SECRET_ACCESS_KEY",
        "MEDLEARN_PROPOSE_BUNDLE_PATH",
    }
    assert propose_job["env"]["MEDLEARN_PROPOSE_BUNDLE_PATH"] == (
        "${{ vars.MEDLEARN_PROPOSE_BUNDLE_PATH }}"
    )
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
