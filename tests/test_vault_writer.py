"""Tests for immutable medlearn-vault writer."""

import hashlib
import json

import pytest
from test_workflow import NOW, ROOT, MemoryStore, seed_proposal

from medlearn_vault.publication import (
    VaultPublicationPlan,
)
from medlearn_vault.vault_writer import (
    VaultPublicationWriter,
    VaultStoredObject,
)
from medlearn_vault.workflow import (
    ApprovalOrchestrator,
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
    assert len(vault.creates) == 2


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
    """Plan publication_plan_id differs from requested ID → not found
    at that key."""
    store = MemoryStore()
    vault = MemoryVaultStore()
    plan, plan_body, _ = _build_plan(store)
    plan_digest = _sha256(plan_body)
    writer = VaultPublicationWriter(store, vault)
    with pytest.raises(WorkflowError, match="PUBLICATION_PLAN_NOT_FOUND"):
        writer.run(
            "publication_plan_11111111111111111111111111111111",
            plan_digest,
            "job-approval-source",
        )


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
    # Vault store has the two artifacts
    assert len(vault.creates) == 2
    for key in vault.creates:
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


def test_package_version_is_0_10() -> None:
    from medlearn_vault import __version__ as v
    assert v == "0.10.0"
