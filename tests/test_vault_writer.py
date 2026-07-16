"""Tests for immutable medlearn-vault writer."""

import hashlib
import json

import pytest
from test_workflow import NOW, ROOT, MemoryStore, copd_envelope, seed_job, seed_proposal
from typer.testing import CliRunner

from medlearn_vault.cli import app
from medlearn_vault.publication import (
    VaultPublicationPlan,
    canonical_publication_plan_json,
    publication_plan_identity,
    publication_plan_object_digest,
)
from medlearn_vault.vault_writer import (
    VaultPublicationWriter,
    VaultStoredObject,
)
from medlearn_vault.workflow import (
    ApprovalAttestor,
    ApprovalOrchestrator,
    AutoPublicationOrchestrator,
    ProposalOrchestrator,
    PublicationPlanOrchestrator,
    StoredObject,
    WorkflowError,
)


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


# ── memory test doubles ──────────────────────────────────────────────


class MemoryVaultStore:
    """In-memory VaultObjectStore for testing."""

    def __init__(self) -> None:
        self.objects: dict[str, VaultStoredObject] = {}
        self.creates: list[str] = []
        self._fail_get: str | None = None
        self._fail_create: str | None = None
        self._fail_get_with: Exception | None = None

    def get(self, key: str) -> VaultStoredObject | None:
        if self._fail_get == key:
            if self._fail_get_with:
                raise self._fail_get_with
            raise WorkflowError("VAULT_STORE_FAILURE")
        return self.objects.get(key)

    def create(
        self, key: str, body: bytes, *, content_type: str
    ) -> bool:
        if self._fail_create == key:
            raise WorkflowError("VAULT_STORE_FAILURE")
        if key in self.objects:
            return False
        self.objects[key] = VaultStoredObject(
            body=body, etag=f'"etag-{len(self.objects)}"',
            content_type=content_type,
        )
        self.creates.append(key)
        return True

    def seed(self, key: str, body: bytes, *, content_type: str) -> None:
        self.objects[key] = VaultStoredObject(
            body=body, etag=f'"etag-{len(self.objects)}"',
            content_type=content_type,
        )


# ── helpers ──────────────────────────────────────────────────────────


def _build_plan(
    store: MemoryStore,
) -> tuple[VaultPublicationPlan, bytes, MemoryStore]:
    """Build a full plan in a MemoryStore and return plan, body, store."""
    proposal_id, proposal_digest, base_digest, _ = seed_proposal(store)
    approval = ApprovalOrchestrator(store).run(
        proposal_id, proposal_digest, base_digest, now=NOW
    )
    stored = store.objects[f"v1/approvals/{approval.approval_id}.json"]
    approval_body = stored.body
    approval_digest = _sha256(approval_body)
    result = PublicationPlanOrchestrator(store, ROOT).run(
        approval.approval_id,
        approval_digest,
        "job-approval-source",
        proposal_id,
        proposal_digest,
        base_digest,
        bundle_path="examples/copd",
    )
    plan_body = store.objects[
        f"v1/publication-plans/{result.publication_plan_id}.json"
    ].body
    plan = VaultPublicationPlan.model_validate_json(plan_body)
    return plan, plan_body, store


# ── happy path ───────────────────────────────────────────────────────


def test_writes_both_artifacts_exact_bytes() -> None:
    store = MemoryStore()
    vault = MemoryVaultStore()
    plan, plan_body, _ = _build_plan(store)
    plan_digest = _sha256(plan_body)
    writer = VaultPublicationWriter(store, vault)
    result = writer.run(
        plan.publication_plan_id,
        plan_digest,
        "job-approval-source",
    )
    assert result.capture_id == plan.capture_id
    assert len(result.created_paths) == 2
    assert len(result.reused_paths) == 0
    # Verify bytes match Plan exactly
    for artifact in plan.artifacts:
        key = artifact.path
        assert key in vault.objects
        stored = vault.objects[key]
        assert stored.body == artifact.content_utf8.encode("utf-8")
        assert stored.content_type == artifact.media_type


def test_auto_publication_uses_source_job_only_and_replays() -> None:
    store = MemoryStore()
    vault = MemoryVaultStore()
    envelope = json.loads(copd_envelope())
    envelope["client_kind"] = "chatgpt_work"
    exact_envelope = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode()
    inputs, _ = seed_job(store, exact_envelope, job_id="job-auto-publish")
    ProposalOrchestrator(store, ROOT).run(
        inputs, bundle_path="examples/copd", workflow_run_id="run-auto", now=NOW
    )

    first = AutoPublicationOrchestrator(store, vault, ROOT).run(
        inputs.job_id, bundle_path="examples/copd"
    )
    assert first.status == "published"
    assert first.created_count == 2
    assert first.reused_count == 0
    assert first.receipt_status == "created"
    assert first.approval_id and first.publication_plan_id and first.capture_id

    second = AutoPublicationOrchestrator(store, vault, ROOT).run(
        inputs.job_id, bundle_path="examples/copd"
    )
    assert second.status == "published"
    assert second.approval_id == first.approval_id
    assert second.publication_plan_id == first.publication_plan_id
    assert second.capture_id == first.capture_id
    assert second.created_count == 0
    assert second.reused_count == 2
    assert second.receipt_status == "reused"


def test_auto_publication_returns_manual_review_without_writes() -> None:
    store = MemoryStore()
    vault = MemoryVaultStore()
    proposal_id, _, _, _ = seed_proposal(store, blocked=True)

    result = AutoPublicationOrchestrator(store, vault, ROOT).run(
        "job-approval-source", bundle_path="examples/capture/ambiguous-ms/bundle"
    )
    assert result.status == "manual_review_required"
    assert result.proposal_id == proposal_id
    assert result.manual_review_reason == "PROPOSAL_ISSUE_AMBIGUOUS_CONCEPT"
    assert not vault.objects
    assert not [key for key in store.objects if key.startswith("v1/approvals/")]


def test_auto_publish_cli_emits_compact_json(monkeypatch: pytest.MonkeyPatch) -> None:
    store = MemoryStore()
    vault = MemoryVaultStore()
    envelope = json.loads(copd_envelope())
    envelope["client_kind"] = "chatgpt_work"
    exact_envelope = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode()
    inputs, _ = seed_job(store, exact_envelope, job_id="job-auto-cli")
    ProposalOrchestrator(store, ROOT).run(
        inputs, bundle_path="examples/copd", workflow_run_id="run-auto-cli", now=NOW
    )
    monkeypatch.setattr("medlearn_vault.cli.S3ObjectStore", lambda *args: store)
    monkeypatch.setattr("medlearn_vault.vault_writer.S3VaultObjectStore", lambda *args: vault)

    result = CliRunner().invoke(
        app,
        ["workflow", "auto-publish", inputs.job_id, "--json"],
        env={
            "CONTROL_R2_ENDPOINT": "configured",
            "CONTROL_R2_ACCESS_KEY_ID": "configured",
            "CONTROL_R2_SECRET_ACCESS_KEY": "configured",
            "VAULT_R2_ENDPOINT": "configured",
            "VAULT_R2_ACCESS_KEY_ID": "configured",
            "VAULT_R2_SECRET_ACCESS_KEY": "configured",
            "MEDLEARN_PROPOSE_BUNDLE_PATH": "examples/copd",
        },
    )
    assert result.exit_code == 0
    assert result.stdout == result.stdout.strip() + "\n"
    payload = json.loads(result.stdout)
    assert payload["status"] == "published"
    assert payload["source_job_id"] == inputs.job_id
    assert payload["created_count"] == 2


def test_content_type_matches_exactly() -> None:
    store = MemoryStore()
    vault = MemoryVaultStore()
    plan, plan_body, _ = _build_plan(store)
    plan_digest = _sha256(plan_body)
    writer = VaultPublicationWriter(store, vault)
    writer.run(plan.publication_plan_id, plan_digest, "job-approval-source")
    json_key = plan.artifacts[0].path
    md_key = plan.artifacts[1].path
    assert vault.objects[json_key].content_type == "application/json; charset=utf-8"
    assert vault.objects[md_key].content_type == "text/markdown; charset=utf-8"


def test_writer_never_calls_renderer_or_bundle() -> None:
    """Writer only reads control objects via store; no bundle access."""
    store = MemoryStore()
    vault = MemoryVaultStore()
    plan, plan_body, _ = _build_plan(store)
    plan_digest = _sha256(plan_body)
    writer = VaultPublicationWriter(store, vault)
    # Writer has no bundle_path argument — it cannot access a bundle
    writer.run(plan.publication_plan_id, plan_digest, "job-approval-source")
    # 2 artifacts + 1 receipt = 3 vault creates
    assert len(vault.creates) == 3


def test_first_run_two_created() -> None:
    store = MemoryStore()
    vault = MemoryVaultStore()
    plan, plan_body, _ = _build_plan(store)
    plan_digest = _sha256(plan_body)
    writer = VaultPublicationWriter(store, vault)
    result = writer.run(
        plan.publication_plan_id, plan_digest, "job-approval-source"
    )
    assert set(result.created_paths) == {
        plan.artifacts[0].path, plan.artifacts[1].path
    }
    assert result.reused_paths == ()


def test_rerun_both_reused() -> None:
    store = MemoryStore()
    vault = MemoryVaultStore()
    plan, plan_body, _ = _build_plan(store)
    plan_digest = _sha256(plan_body)
    writer = VaultPublicationWriter(store, vault)
    first = writer.run(
        plan.publication_plan_id, plan_digest, "job-approval-source"
    )
    assert len(first.created_paths) == 2
    second = writer.run(
        plan.publication_plan_id, plan_digest, "job-approval-source"
    )
    assert len(second.created_paths) == 0
    assert len(second.reused_paths) == 2


def test_json_exists_md_missing_recovers() -> None:
    """If JSON already exists identically but Markdown is missing,
    the writer reuses JSON and creates Markdown."""
    store = MemoryStore()
    vault = MemoryVaultStore()
    plan, plan_body, _ = _build_plan(store)
    plan_digest = _sha256(plan_body)
    # Pre-seed only the JSON artifact
    json_artifact = plan.artifacts[0]
    vault.seed(
        json_artifact.path,
        json_artifact.content_utf8.encode("utf-8"),
        content_type=json_artifact.media_type,
    )
    writer = VaultPublicationWriter(store, vault)
    result = writer.run(
        plan.publication_plan_id, plan_digest, "job-approval-source"
    )
    assert set(result.reused_paths) == {json_artifact.path}
    assert set(result.created_paths) == {plan.artifacts[1].path}


def test_md_exists_json_missing_creates_in_fixed_order() -> None:
    """Markdown exists, JSON missing → writer creates JSON first, reuses MD."""
    store = MemoryStore()
    vault = MemoryVaultStore()
    plan, plan_body, _ = _build_plan(store)
    plan_digest = _sha256(plan_body)
    md_artifact = plan.artifacts[1]
    vault.seed(
        md_artifact.path,
        md_artifact.content_utf8.encode("utf-8"),
        content_type=md_artifact.media_type,
    )
    writer = VaultPublicationWriter(store, vault)
    result = writer.run(
        plan.publication_plan_id, plan_digest, "job-approval-source"
    )
    assert set(result.created_paths) == {plan.artifacts[0].path}
    assert set(result.reused_paths) == {md_artifact.path}


# ── conflict ─────────────────────────────────────────────────────────


def test_different_body_same_key_conflict() -> None:
    """JSON key exists with different body → VAULT_ARTIFACT_CONFLICT."""
    store = MemoryStore()
    vault = MemoryVaultStore()
    plan, plan_body, _ = _build_plan(store)
    plan_digest = _sha256(plan_body)
    json_key = plan.artifacts[0].path
    vault.seed(json_key, b'different bytes\n', content_type=plan.artifacts[0].media_type)
    writer = VaultPublicationWriter(store, vault)
    with pytest.raises(WorkflowError, match="VAULT_ARTIFACT_CONFLICT"):
        writer.run(
            plan.publication_plan_id, plan_digest, "job-approval-source"
        )


def test_same_body_different_content_type_conflict() -> None:
    """Same body but different ContentType → VAULT_ARTIFACT_CONFLICT."""
    store = MemoryStore()
    vault = MemoryVaultStore()
    plan, plan_body, _ = _build_plan(store)
    plan_digest = _sha256(plan_body)
    json_artifact = plan.artifacts[0]
    vault.seed(
        json_artifact.path,
        json_artifact.content_utf8.encode("utf-8"),
        content_type="application/octet-stream",
    )
    writer = VaultPublicationWriter(store, vault)
    with pytest.raises(WorkflowError, match="VAULT_ARTIFACT_CONFLICT"):
        writer.run(
            plan.publication_plan_id, plan_digest, "job-approval-source"
        )


def test_first_artifact_correct_second_conflicting_preserves_first() -> None:
    """When second artifact conflicts, the first (correct) object is kept."""
    store = MemoryStore()
    vault = MemoryVaultStore()
    plan, plan_body, _ = _build_plan(store)
    plan_digest = _sha256(plan_body)
    md_key = plan.artifacts[1].path
    vault.seed(md_key, b'different\n', content_type=plan.artifacts[1].media_type)
    writer = VaultPublicationWriter(store, vault)
    with pytest.raises(WorkflowError, match="VAULT_ARTIFACT_CONFLICT"):
        writer.run(
            plan.publication_plan_id, plan_digest, "job-approval-source"
        )
    # JSON was created before the conflict
    json_key = plan.artifacts[0].path
    assert json_key in vault.objects
    assert vault.objects[json_key].body == plan.artifacts[0].content_utf8.encode("utf-8")


# ── validation errors ────────────────────────────────────────────────


def test_plan_not_found() -> None:
    store = MemoryStore()
    vault = MemoryVaultStore()
    writer = VaultPublicationWriter(store, vault)
    with pytest.raises(WorkflowError, match="PUBLICATION_PLAN_NOT_FOUND"):
        writer.run(
            "publication_plan_00000000000000000000000000000000",
            "sha256:0000000000000000000000000000000000000000000000000000000000000000",
            "job-approval-source",
        )


def test_plan_object_digest_wrong() -> None:
    store = MemoryStore()
    vault = MemoryVaultStore()
    plan, plan_body, _ = _build_plan(store)
    wrong_digest = "sha256:" + "00" * 32
    writer = VaultPublicationWriter(store, vault)
    with pytest.raises(WorkflowError, match="PUBLICATION_PLAN_OBJECT_DIGEST_MISMATCH"):
        writer.run(
            plan.publication_plan_id, wrong_digest, "job-approval-source"
        )


def test_plan_non_canonical_json_rejected() -> None:
    """Non-canonical plan bytes fail the raw-bytes digest check first
    since the stored digest must match expected before parsing."""
    store = MemoryStore()
    vault = MemoryVaultStore()
    plan, plan_body, _ = _build_plan(store)
    # Reformat with indentation
    non_canonical = (
        json.dumps(
            json.loads(plan_body), ensure_ascii=False, indent=2
        )
        + "\n"
    ).encode()
    non_canonical_digest = _sha256(non_canonical)
    plan_key = f"v1/publication-plans/{plan.publication_plan_id}.json"
    store.objects[plan_key] = StoredObject(
        body=non_canonical, etag="bad"
    )
    writer = VaultPublicationWriter(store, vault)
    # Pass the digest of the non-canonical body so the raw check passes;
    # the parse+recanonicalize check then fires.
    with pytest.raises(WorkflowError, match="INVALID_PUBLICATION_PLAN"):
        writer.run(
            plan.publication_plan_id, non_canonical_digest, "job-approval-source"
        )


def test_plan_tampered_field_invalidates_identity() -> None:
    """When a field that binds the plan identity is tampered,
    the plan's self-validator fires and returns INVALID_PUBLICATION_PLAN."""
    store = MemoryStore()
    vault = MemoryVaultStore()
    plan, plan_body, _ = _build_plan(store)
    tampered_data = json.loads(plan_body)
    tampered_data["approval_object_digest"] = (
        "sha256:0000000000000000000000000000000000000000000000000000000000000000"
    )
    tampered = (
        json.dumps(
            tampered_data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        + "\n"
    ).encode()
    tampered_digest = _sha256(tampered)
    plan_key = f"v1/publication-plans/{plan.publication_plan_id}.json"
    store.objects[plan_key] = StoredObject(
        body=tampered, etag="tampered"
    )
    writer = VaultPublicationWriter(store, vault)
    # Plan identity validator rejects because publication_plan_id
    # no longer matches the identity bound
    with pytest.raises(WorkflowError, match="INVALID_PUBLICATION_PLAN"):
        writer.run(
            plan.publication_plan_id, tampered_digest, "job-approval-source"
        )


def test_plan_id_key_mismatch() -> None:
    """A valid plan body at a different valid key is rejected before writes."""
    store = MemoryStore()
    vault = MemoryVaultStore()
    plan, plan_body, _ = _build_plan(store)
    plan_digest = _sha256(plan_body)
    different_plan_id = "publication_plan_" + "0" * 32
    assert different_plan_id != plan.publication_plan_id
    store.objects[
        f"v1/publication-plans/{different_plan_id}.json"
    ] = StoredObject(body=plan_body, etag="wrong-key")
    writer = VaultPublicationWriter(store, vault)
    with pytest.raises(WorkflowError) as captured:
        writer.run(
            different_plan_id,
            plan_digest,
            "job-approval-source",
        )
    assert captured.value.code == "INVALID_PUBLICATION_PLAN"
    assert vault.objects == {}
    assert vault.creates == []


def test_artifact_tampered_in_stored_plan_rejected() -> None:
    """If stored plan passes initial parse but has tampered artifact digest,
    the plan validator catches it."""
    store = MemoryStore()
    vault = MemoryVaultStore()
    plan, plan_body, _ = _build_plan(store)
    tampered_data = json.loads(plan_body)
    tampered_data["artifacts"][0]["byte_length"] = 999
    tampered = (
        json.dumps(
            tampered_data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        + "\n"
    ).encode()
    tampered_digest = _sha256(tampered)
    plan_key = f"v1/publication-plans/{plan.publication_plan_id}.json"
    store.objects[plan_key] = StoredObject(
        body=tampered, etag="tampered"
    )
    writer = VaultPublicationWriter(store, vault)
    with pytest.raises(WorkflowError, match="INVALID_PUBLICATION_PLAN"):
        writer.run(
            plan.publication_plan_id, tampered_digest, "job-approval-source"
        )


# ── provenance attestation errors → zero vault writes ───────────────


def test_attestation_failure_zero_vault_writes() -> None:
    """When attestation fails (wrong source_job_id not present),
    no vault writes occur."""
    store = MemoryStore()
    vault = MemoryVaultStore()
    plan, plan_body, _ = _build_plan(store)
    plan_digest = _sha256(plan_body)
    writer = VaultPublicationWriter(store, vault)
    with pytest.raises(WorkflowError, match="JOB_NOT_FOUND"):
        writer.run(
            plan.publication_plan_id,
            plan_digest,
            "job-nonexistent",
        )
    assert len(vault.objects) == 0


def test_provenance_review_digest_mismatch_zero_vault_writes() -> None:
    """A self-valid plan with a false review digest fails only at provenance cross-check."""
    store = MemoryStore()
    vault = MemoryVaultStore()
    plan, _, _ = _build_plan(store)
    mismatched_review_digest = "sha256:" + "0" * 64
    plan_data = plan.model_dump(mode="json")
    plan_data["review_digest"] = mismatched_review_digest
    plan_data["publication_plan_id"] = publication_plan_identity(
        plan.approval_id,
        plan.approval_object_digest,
        plan.proposal_id,
        plan.proposal_object_digest,
        plan.base_bundle_digest,
        mismatched_review_digest,
    )
    mismatched_plan = VaultPublicationPlan.model_validate(plan_data)
    mismatched_body = canonical_publication_plan_json(mismatched_plan)
    assert (
        canonical_publication_plan_json(
            VaultPublicationPlan.model_validate_json(mismatched_body)
        )
        == mismatched_body
    )
    mismatched_plan_digest = publication_plan_object_digest(mismatched_plan)
    assert _sha256(mismatched_body) == mismatched_plan_digest
    store.objects[
        f"v1/publication-plans/{mismatched_plan.publication_plan_id}.json"
    ] = StoredObject(body=mismatched_body, etag="mismatched-review")

    fresh_attestation = ApprovalAttestor(store).run(
        mismatched_plan.approval_id,
        "job-approval-source",
        mismatched_plan.proposal_id,
        mismatched_plan.proposal_object_digest,
        mismatched_plan.base_bundle_digest,
        expected_decision="approved",
        expected_rejection_code=None,
        expected_approval_object_digest=mismatched_plan.approval_object_digest,
    )
    assert fresh_attestation.review_digest != mismatched_plan.review_digest

    writer = VaultPublicationWriter(store, vault)
    with pytest.raises(WorkflowError) as captured:
        writer.run(
            mismatched_plan.publication_plan_id,
            mismatched_plan_digest,
            "job-approval-source",
        )
    assert captured.value.code == "PUBLICATION_PLAN_PROVENANCE_MISMATCH"
    assert vault.objects == {}
    assert vault.creates == []


# ── store failure ────────────────────────────────────────────────────


def test_vault_create_failure() -> None:
    store = MemoryStore()
    vault = MemoryVaultStore()
    plan, plan_body, _ = _build_plan(store)
    plan_digest = _sha256(plan_body)
    vault._fail_create = plan.artifacts[0].path
    writer = VaultPublicationWriter(store, vault)
    with pytest.raises(WorkflowError, match="VAULT_STORE_FAILURE"):
        writer.run(
            plan.publication_plan_id, plan_digest, "job-approval-source"
        )


def test_vault_get_failure() -> None:
    store = MemoryStore()
    vault = MemoryVaultStore()
    plan, plan_body, _ = _build_plan(store)
    plan_digest = _sha256(plan_body)
    # Pre-seed artifact so create returns False
    json_key = plan.artifacts[0].path
    vault.seed(
        json_key,
        plan.artifacts[0].content_utf8.encode("utf-8"),
        content_type=plan.artifacts[0].media_type,
    )
    vault._fail_get = json_key
    writer = VaultPublicationWriter(store, vault)
    with pytest.raises(WorkflowError, match="VAULT_STORE_FAILURE"):
        writer.run(
            plan.publication_plan_id, plan_digest, "job-approval-source"
        )


def test_control_store_get_unexpected_failure_zero_vault_writes() -> None:
    plan_id = "publication_plan_" + "0" * 32
    plan_key = f"v1/publication-plans/{plan_id}.json"

    class BrokenControlStore:
        def __init__(self) -> None:
            self.requested_keys: list[str] = []

        def get(self, key: str) -> StoredObject | None:
            self.requested_keys.append(key)
            assert key == plan_key
            raise RuntimeError("control storage unavailable")

    control = BrokenControlStore()
    vault = MemoryVaultStore()
    writer = VaultPublicationWriter(control, vault)
    with pytest.raises(WorkflowError) as captured:
        writer.run(plan_id, "sha256:" + "0" * 64, "job-approval-source")
    assert captured.value.code == "CONTROL_STORE_FAILURE"
    assert captured.value.code != "VAULT_STORE_FAILURE"
    assert control.requested_keys == [plan_key]
    assert vault.objects == {}
    assert vault.creates == []


def test_second_artifact_create_failure_recovers_without_overwrite() -> None:
    store = MemoryStore()
    vault = MemoryVaultStore()
    plan, plan_body, _ = _build_plan(store)
    plan_digest = _sha256(plan_body)
    json_artifact, markdown_artifact = plan.artifacts
    vault._fail_create = markdown_artifact.path
    writer = VaultPublicationWriter(store, vault)

    with pytest.raises(WorkflowError) as captured:
        writer.run(
            plan.publication_plan_id, plan_digest, "job-approval-source"
        )
    assert captured.value.code == "VAULT_STORE_FAILURE"
    assert set(vault.objects) == {json_artifact.path}
    assert vault.objects[json_artifact.path].body == json_artifact.content_utf8.encode("utf-8")
    assert vault.objects[json_artifact.path].content_type == json_artifact.media_type
    assert markdown_artifact.path not in vault.objects
    json_etag = vault.objects[json_artifact.path].etag

    vault._fail_create = None
    recovered = writer.run(
        plan.publication_plan_id, plan_digest, "job-approval-source"
    )
    assert recovered.reused_paths == (json_artifact.path,)
    assert recovered.created_paths == (markdown_artifact.path,)
    assert vault.objects[json_artifact.path].etag == json_etag
    for artifact in plan.artifacts:
        assert vault.objects[artifact.path].body == artifact.content_utf8.encode("utf-8")
        assert vault.objects[artifact.path].content_type == artifact.media_type


# ── no side-effects on control objects ───────────────────────────────


def test_writer_never_writes_control_objects() -> None:
    store = MemoryStore()
    vault = MemoryVaultStore()
    plan, plan_body, _ = _build_plan(store)
    plan_digest = _sha256(plan_body)
    # Snapshot control objects
    snapshots = {
        k: v.body for k, v in store.objects.items() if k.startswith("v1/")
    }
    writer = VaultPublicationWriter(store, vault)
    writer.run(plan.publication_plan_id, plan_digest, "job-approval-source")
    # Verify no control objects changed
    for k in snapshots:
        assert store.objects[k].body == snapshots[k], f"Modified: {k}"
    # No new keys added to control store
    control_keys = {k for k in store.objects if k.startswith("v1/")}
    assert control_keys == set(snapshots)


def test_writer_creates_only_vault_keys() -> None:
    """Writer creates objects only in the vault store, never in control."""
    store = MemoryStore()
    vault = MemoryVaultStore()
    plan, plan_body, _ = _build_plan(store)
    plan_digest = _sha256(plan_body)
    writer = VaultPublicationWriter(store, vault)
    writer.run(plan.publication_plan_id, plan_digest, "job-approval-source")
    # Vault store has the two artifacts plus one receipt
    assert len(vault.creates) == 3
    artifact_creates = [k for k in vault.creates if not k.startswith("v1/")]
    receipt_creates = [k for k in vault.creates if k.startswith("v1/")]
    assert len(artifact_creates) == 2
    assert len(receipt_creates) == 1
    for key in artifact_creates:
        assert key.startswith("MedLearn/")


def test_no_overwrite_no_delete_no_rename() -> None:
    """Writer never overwrites, deletes, or renames existing objects."""
    store = MemoryStore()
    vault = MemoryVaultStore()
    plan, plan_body, _ = _build_plan(store)
    plan_digest = _sha256(plan_body)
    # First run creates both
    writer = VaultPublicationWriter(store, vault)
    writer.run(plan.publication_plan_id, plan_digest, "job-approval-source")
    first_etags = {
        k: v.etag for k, v in vault.objects.items()
    }
    # Second run reuses both
    writer.run(plan.publication_plan_id, plan_digest, "job-approval-source")
    # Etags unchanged → no overwrite
    for k, v in vault.objects.items():
        assert v.etag == first_etags[k], f"Etags changed for {k}"


def test_no_manifest_obsidian_d1_vps_llm() -> None:
    """Writer code must not reference manifest, Obsidian, D1, VPS, LLM."""
    import inspect
    import re

    import medlearn_vault.vault_writer as vw
    source = inspect.getsource(vw)
    # Word-boundary match so "fullmatch" doesn't trigger on "llm"
    banned_words = ["manifest", "obsidian", "rclone",
                    "d1", "durable", "vps", "llm", "openai", "anthropic"]
    for term in banned_words:
        pattern = re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE)
        assert not pattern.search(source), f"Banned in vault_writer: {term}"
    # The writer must not expose compare_and_swap or list
    assert "compare_and_swap" not in source
    assert "list" not in dir(vw.VaultObjectStore)


# ── input validation ─────────────────────────────────────────────────


def test_invalid_input_format() -> None:
    store = MemoryStore()
    vault = MemoryVaultStore()
    writer = VaultPublicationWriter(store, vault)
    with pytest.raises(WorkflowError, match="INVALID_VAULT_PUBLICATION_INPUT"):
        writer.run("bad-id", "bad-digest", "bad-job")


# ── Receipt ──────────────────────────────────────────────────────────


def test_receipt_canonical_bytes_deterministic() -> None:
    from medlearn_vault.publication import (
        build_vault_publication_receipt,
        canonical_vault_publication_receipt_json,
    )

    store = MemoryStore()
    plan, plan_body, _ = _build_plan(store)
    receipt = build_vault_publication_receipt(plan)
    b1 = canonical_vault_publication_receipt_json(receipt)
    b2 = canonical_vault_publication_receipt_json(receipt)
    assert b1 == b2
    assert b1.endswith(b"\n")
    assert b"\r" not in b1
    assert not b1.startswith(b"\xef\xbb\xbf")
    # No timestamps, no random IDs
    text = b1.decode("utf-8")
    assert '"decided_at"' not in text
    assert '"workflow_run_id"' not in text
    assert '"source_job_id"' not in text
    assert '"github_actor"' not in text


def test_receipt_fields_match_plan_artifacts() -> None:
    from medlearn_vault.publication import build_vault_publication_receipt

    store = MemoryStore()
    plan, plan_body, _ = _build_plan(store)
    receipt = build_vault_publication_receipt(plan)
    assert receipt.receipt_version == "0.1.0"
    assert receipt.publication_plan_id == plan.publication_plan_id
    assert receipt.capture_id == plan.capture_id
    for i, artifact in enumerate(plan.artifacts):
        assert receipt.artifacts[i].path == artifact.path
        assert receipt.artifacts[i].media_type == artifact.media_type
        assert receipt.artifacts[i].content_digest == artifact.content_digest
        assert receipt.artifacts[i].byte_length == artifact.byte_length


def test_receipt_created_after_both_artifacts() -> None:
    store = MemoryStore()
    vault = MemoryVaultStore()
    plan, plan_body, _ = _build_plan(store)
    plan_digest = _sha256(plan_body)
    writer = VaultPublicationWriter(store, vault)
    result = writer.run(plan.publication_plan_id, plan_digest, "job-approval-source")
    assert result.receipt_status == "created"
    # Receipt stored at correct key
    receipt_key = f"v1/publications/{plan.publication_plan_id}.json"
    assert receipt_key in vault.objects
    assert vault.objects[receipt_key].content_type == "application/json; charset=utf-8"


def test_receipt_not_created_when_json_artifact_fails() -> None:
    store = MemoryStore()
    vault = MemoryVaultStore()
    plan, plan_body, _ = _build_plan(store)
    plan_digest = _sha256(plan_body)
    vault._fail_create = plan.artifacts[0].path
    writer = VaultPublicationWriter(store, vault)
    with pytest.raises(WorkflowError, match="VAULT_STORE_FAILURE"):
        writer.run(plan.publication_plan_id, plan_digest, "job-approval-source")
    receipt_key = f"v1/publications/{plan.publication_plan_id}.json"
    assert receipt_key not in vault.objects


def test_receipt_not_created_when_md_artifact_fails() -> None:
    store = MemoryStore()
    vault = MemoryVaultStore()
    plan, plan_body, _ = _build_plan(store)
    plan_digest = _sha256(plan_body)
    vault._fail_create = plan.artifacts[1].path
    writer = VaultPublicationWriter(store, vault)
    with pytest.raises(WorkflowError, match="VAULT_STORE_FAILURE"):
        writer.run(plan.publication_plan_id, plan_digest, "job-approval-source")
    receipt_key = f"v1/publications/{plan.publication_plan_id}.json"
    assert receipt_key not in vault.objects


def test_receipt_not_created_on_artifact_conflict() -> None:
    store = MemoryStore()
    vault = MemoryVaultStore()
    plan, plan_body, _ = _build_plan(store)
    plan_digest = _sha256(plan_body)
    json_key = plan.artifacts[0].path
    vault.seed(json_key, b"different\n", content_type=plan.artifacts[0].media_type)
    writer = VaultPublicationWriter(store, vault)
    with pytest.raises(WorkflowError, match="VAULT_ARTIFACT_CONFLICT"):
        writer.run(plan.publication_plan_id, plan_digest, "job-approval-source")
    receipt_key = f"v1/publications/{plan.publication_plan_id}.json"
    assert receipt_key not in vault.objects


def test_receipt_created_after_partial_recovery() -> None:
    store = MemoryStore()
    vault = MemoryVaultStore()
    plan, plan_body, _ = _build_plan(store)
    plan_digest = _sha256(plan_body)
    md_artifact = plan.artifacts[1]
    vault._fail_create = md_artifact.path
    writer = VaultPublicationWriter(store, vault)
    with pytest.raises(WorkflowError):
        writer.run(plan.publication_plan_id, plan_digest, "job-approval-source")

    vault._fail_create = None
    result = writer.run(plan.publication_plan_id, plan_digest, "job-approval-source")
    assert result.receipt_status == "created"
    receipt_key = f"v1/publications/{plan.publication_plan_id}.json"
    assert receipt_key in vault.objects


def test_receipt_reused_when_exists_and_matches() -> None:
    store = MemoryStore()
    vault = MemoryVaultStore()
    plan, plan_body, _ = _build_plan(store)
    plan_digest = _sha256(plan_body)
    writer = VaultPublicationWriter(store, vault)
    first = writer.run(plan.publication_plan_id, plan_digest, "job-approval-source")
    assert first.receipt_status == "created"

    second = writer.run(plan.publication_plan_id, plan_digest, "job-approval-source")
    assert second.receipt_status == "reused"
    assert second.created_paths == ()
    assert len(second.reused_paths) == 2


def test_receipt_content_conflict() -> None:
    store = MemoryStore()
    vault = MemoryVaultStore()
    plan, plan_body, _ = _build_plan(store)
    plan_digest = _sha256(plan_body)
    receipt_key = f"v1/publications/{plan.publication_plan_id}.json"

    # Seed a receipt with wrong body
    vault.seed(
        receipt_key, b"wrong receipt bytes\n",
        content_type="application/json; charset=utf-8",
    )
    writer = VaultPublicationWriter(store, vault)
    with pytest.raises(WorkflowError, match="VAULT_PUBLICATION_RECEIPT_CONFLICT"):
        writer.run(plan.publication_plan_id, plan_digest, "job-approval-source")


def test_receipt_content_type_conflict() -> None:
    from medlearn_vault.publication import (
        build_vault_publication_receipt,
        canonical_vault_publication_receipt_json,
    )

    store = MemoryStore()
    vault = MemoryVaultStore()
    plan, plan_body, _ = _build_plan(store)
    plan_digest = _sha256(plan_body)
    receipt = build_vault_publication_receipt(plan)
    receipt_body = canonical_vault_publication_receipt_json(receipt)
    receipt_key = f"v1/publications/{plan.publication_plan_id}.json"

    # Seed same body but wrong Content-Type
    vault.seed(receipt_key, receipt_body, content_type="application/octet-stream")
    writer = VaultPublicationWriter(store, vault)
    with pytest.raises(WorkflowError, match="VAULT_PUBLICATION_RECEIPT_CONFLICT"):
        writer.run(plan.publication_plan_id, plan_digest, "job-approval-source")


def test_receipt_store_failure() -> None:
    store = MemoryStore()
    vault = MemoryVaultStore()
    plan, plan_body, _ = _build_plan(store)
    plan_digest = _sha256(plan_body)
    receipt_key = f"v1/publications/{plan.publication_plan_id}.json"
    vault._fail_create = receipt_key
    writer = VaultPublicationWriter(store, vault)
    with pytest.raises(WorkflowError, match="VAULT_STORE_FAILURE"):
        writer.run(plan.publication_plan_id, plan_digest, "job-approval-source")


def test_receipt_only_at_last_step() -> None:
    """Receipt is only created as the final step after both artifacts."""
    store = MemoryStore()
    vault = MemoryVaultStore()
    plan, plan_body, _ = _build_plan(store)
    plan_digest = _sha256(plan_body)
    # Verify receipt is created after both artifacts
    writer = VaultPublicationWriter(store, vault)
    writer.run(plan.publication_plan_id, plan_digest, "job-approval-source")
    assert len(vault.creates) == 3  # 2 artifacts + 1 receipt
    # Receipt must be the last create
    receipt_key = f"v1/publications/{plan.publication_plan_id}.json"
    assert vault.creates[-1] == receipt_key


def test_artifact_count_semantics_unchanged() -> None:
    """created_count and reused_count only count the two formal artifacts, never the receipt."""
    store = MemoryStore()
    vault = MemoryVaultStore()
    plan, plan_body, _ = _build_plan(store)
    plan_digest = _sha256(plan_body)
    writer = VaultPublicationWriter(store, vault)
    result = writer.run(plan.publication_plan_id, plan_digest, "job-approval-source")
    assert len(result.created_paths) == 2
    assert len(result.reused_paths) == 0

    result2 = writer.run(plan.publication_plan_id, plan_digest, "job-approval-source")
    assert len(result2.created_paths) == 0
    assert len(result2.reused_paths) == 2


def test_cli_output_receipt_status_created(monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    store = MemoryStore()
    vault = MemoryVaultStore()

    class FakeControlStore:
        def get(self, key: str) -> StoredObject | None:
            return store.get(key)

    class FakeVaultStore:
        def __init__(self, *args: object) -> None: ...
        def get(self, key: str) -> VaultStoredObject | None:
            return vault.get(key)
        def create(self, key: str, body: bytes, *, content_type: str) -> bool:
            return vault.create(key, body, content_type=content_type)

    plan, plan_body, _ = _build_plan(store)
    plan_digest = _sha256(plan_body)

    runner = CliRunner()
    monkeypatch.setattr(
        "medlearn_vault.cli.S3ReadOnlyObjectStore", lambda *a: FakeControlStore()
    )
    monkeypatch.setattr(
        "medlearn_vault.vault_writer.S3VaultObjectStore", lambda *a: FakeVaultStore()
    )
    result = runner.invoke(
        app,
        [
            "workflow", "publish-vault",
            plan.publication_plan_id,
            plan_digest,
            "job-approval-source",
        ],
        env={
            "CONTROL_R2_ENDPOINT": "x", "CONTROL_R2_ACCESS_KEY_ID": "x",
            "CONTROL_R2_SECRET_ACCESS_KEY": "x",
            "VAULT_R2_ENDPOINT": "x", "VAULT_R2_ACCESS_KEY_ID": "x",
            "VAULT_R2_SECRET_ACCESS_KEY": "x",
        },
    )
    assert result.exit_code == 0
    assert "receipt_status=created" in result.stdout
    assert "created_count=2" in result.stdout
    assert "reused_count=0" in result.stdout


def test_cli_output_receipt_status_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    store = MemoryStore()
    vault = MemoryVaultStore()

    class FakeControlStore:
        def get(self, key: str) -> StoredObject | None:
            return store.get(key)

    class FakeVaultStore:
        def __init__(self, *args: object) -> None: ...
        def get(self, key: str) -> VaultStoredObject | None:
            return vault.get(key)
        def create(self, key: str, body: bytes, *, content_type: str) -> bool:
            return vault.create(key, body, content_type=content_type)

    plan, plan_body, _ = _build_plan(store)
    plan_digest = _sha256(plan_body)

    # First run
    VaultPublicationWriter(store, vault).run(
        plan.publication_plan_id, plan_digest, "job-approval-source"
    )

    runner = CliRunner()
    monkeypatch.setattr(
        "medlearn_vault.cli.S3ReadOnlyObjectStore", lambda *a: FakeControlStore()
    )
    monkeypatch.setattr(
        "medlearn_vault.vault_writer.S3VaultObjectStore", lambda *a: FakeVaultStore()
    )
    result = runner.invoke(
        app,
        [
            "workflow", "publish-vault",
            plan.publication_plan_id,
            plan_digest,
            "job-approval-source",
        ],
        env={
            "CONTROL_R2_ENDPOINT": "x", "CONTROL_R2_ACCESS_KEY_ID": "x",
            "CONTROL_R2_SECRET_ACCESS_KEY": "x",
            "VAULT_R2_ENDPOINT": "x", "VAULT_R2_ACCESS_KEY_ID": "x",
            "VAULT_R2_SECRET_ACCESS_KEY": "x",
        },
    )
    assert result.exit_code == 0
    assert "receipt_status=reused" in result.stdout
    assert "created_count=0" in result.stdout
    assert "reused_count=2" in result.stdout


def test_writer_no_overwrite_delete_rename() -> None:
    """Writer protocol must not expose overwrite, delete, or rename."""
    import medlearn_vault.vault_writer as vw

    protocol_methods = {name for name in dir(vw.VaultObjectStore) if not name.startswith("_")}
    assert "get" in protocol_methods
    assert "create" in protocol_methods
    for banned in ("put", "delete", "overwrite", "rename", "copy", "compare_and_swap", "list"):
        assert banned not in protocol_methods


# ── CLI smoke ────────────────────────────────────────────────────────


def test_publish_vault_cli_help() -> None:
    import subprocess
    result = subprocess.run(
        ["medlearn", "workflow", "publish-vault", "--help"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "publication_plan_id" in result.stdout


# ── workflow YAML audit ──────────────────────────────────────────────


def test_publish_vault_workflow_is_main_only_and_scoped() -> None:
    import re
    from pathlib import Path

    import yaml

    text = Path(
        ".github/workflows/medlearn-publish-vault.yml"
    ).read_text(encoding="utf-8")
    data = yaml.load(text, Loader=yaml.BaseLoader)
    inputs = data["on"]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {
        "publication_plan_id",
        "publication_plan_object_digest",
        "source_job_id",
        "confirmation",
    }
    assert all(item["required"] == "true" for item in inputs.values())
    assert data["permissions"] == {"contents": "read"}
    assert data["concurrency"]["cancel-in-progress"] == "false"
    job = data["jobs"]["publish"]
    assert job["if"] == "github.ref == 'refs/heads/main'"
    assert job["timeout-minutes"] == "10"
    assert "env" not in job
    steps = job["steps"]
    for step in steps:
        if "uses" in step:
            assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", step["uses"])
    checkout = steps[0]
    assert checkout["with"] == {"persist-credentials": "false", "ref": "main"}
    preflight = next(
        step for step in steps
        if step.get("name") == "Validate publish-vault confirmation"
    )
    assert "CONTROL_R2_" not in str(preflight)
    assert "VAULT_R2_" not in str(preflight)
    assert "PUBLISH_VAULT_CONFIRMATION_MISMATCH" in preflight["run"]
    final = steps[-1]
    assert final["name"] == "Publish planned artifacts to medlearn-vault"
    assert "medlearn workflow publish-vault" in final["run"]
    for prefix in ("CONTROL_R2_", "VAULT_R2_"):
        assert any(key.startswith(prefix) for key in final["env"])
    assert set(
        key for key in final["env"]
        if key.startswith("CONTROL_R2_") or key.startswith("VAULT_R2_")
    ) == {
        "CONTROL_R2_ENDPOINT",
        "CONTROL_R2_ACCESS_KEY_ID",
        "CONTROL_R2_SECRET_ACCESS_KEY",
        "VAULT_R2_ENDPOINT",
        "VAULT_R2_ACCESS_KEY_ID",
        "VAULT_R2_SECRET_ACCESS_KEY",
    }
    assert "upload-artifact" not in text
    assert "GITHUB_STEP_SUMMARY" not in text and "set -x" not in text


# ── version ──────────────────────────────────────────────────────────


def test_package_version_is_0_17() -> None:
    from medlearn_vault import __version__ as v
    assert v == "0.17.0"
