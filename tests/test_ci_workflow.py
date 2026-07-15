import re
from pathlib import Path

import yaml


def test_ci_workflow_is_pinned_reproducible_and_secret_free() -> None:
    text = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    data = yaml.load(text, Loader=yaml.BaseLoader)
    assert data["permissions"] == {"contents": "read"}
    assert re.search(r"\$\{\{\s*secrets\.", text) is None
    assert "actions/upload-artifact" not in text
    assert "GITHUB_STEP_SUMMARY" not in text
    assert "set -x" not in text

    for job in data["jobs"].values():
        assert job["timeout-minutes"]
        for step in job["steps"]:
            if "uses" in step:
                assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", step["uses"])
            if step.get("uses", "").startswith("actions/checkout@"):
                assert step["with"]["persist-credentials"] == "false"

    quality = data["jobs"]["quality"]
    commands = "\n".join(step.get("run", "") for step in quality["steps"])
    assert "pip install --require-hashes -r requirements/ci.txt" in commands
    assert "pip install --no-build-isolation --no-deps ." in commands
    assert "medlearn bundle validate examples/gerd" in commands
    assert "medlearn bundle validate examples/copd" in commands
    assert "pip wheel --no-build-isolation --no-deps" in commands
    assert "medlearn --help" in commands and "medlearn doctor" in commands

    windows = data["jobs"]["windows-sync-quality"]
    windows_commands = "\n".join(step.get("run", "") for step in windows["steps"])
    targeted = (
        "tests/test_sync_client.py tests/test_sync_cli.py tests/test_windows_secrets.py "
        "tests/test_windows_rollout.py"
    )
    assert targeted in windows_commands
    assert "Get-ChildItem wheelhouse\\medlearn_vault-0.16.1-*.whl" in windows_commands
    assert "$installed --version" in windows_commands
    assert "Isolated Windows self-upgrade bootstrap acceptance" in text
    assert "SYNC_INSTALL_SELF_UPGRADE_REQUIRES_BOOTSTRAP" in windows_commands
    assert "scripts\\install_windows_client.ps1" in windows_commands
    assert 'Join-Path $env:RUNNER_TEMP "medlearn sync 用户"' in windows_commands
    assert "$env:PYTHONIOENCODING = 'cp1252:strict'" in windows_commands
    assert "$rawText | ConvertFrom-Json" in windows_commands
    assert "installer JSON output was not ASCII-safe" in windows_commands
    assert "installer wrote a traceback to stderr" in windows_commands
    assert "Remove-Item Env:PYTHONIOENCODING" in windows_commands
    assert "Remove-Item -LiteralPath $installRoot -Recurse -Force" in windows_commands
    assert "medlearn sync schedule install --what-if --json" in windows_commands

    worker = data["jobs"]["worker-quality"]
    worker_commands = "\n".join(step.get("run", "") for step in worker["steps"])
    assert "npx --no-install wrangler deploy --dry-run" in worker_commands

    lock = Path("requirements/ci.txt").read_text(encoding="utf-8")
    requirement_lines = [
        line for line in lock.splitlines() if line and not line.startswith((" ", "#"))
    ]
    assert requirement_lines and all(
        "==" in line and line.endswith(chr(92)) for line in requirement_lines
    )
    assert "--hash=sha256:" in lock


def test_publication_workflow_is_main_only_and_scopes_control_credentials() -> None:
    text = Path(".github/workflows/medlearn-plan-publication.yml").read_text(encoding="utf-8")
    data = yaml.load(text, Loader=yaml.BaseLoader)
    inputs = data["on"]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {
        "approval_id", "approval_object_digest", "source_job_id", "proposal_id",
        "proposal_object_digest", "expected_base_bundle_digest", "confirmation",
    }
    assert all(item["required"] == "true" for item in inputs.values())
    assert data["permissions"] == {"contents": "read"}
    assert data["concurrency"]["cancel-in-progress"] == "false"
    job = data["jobs"]["plan"]
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
        step for step in steps if step.get("name") == "Validate publication confirmation"
    )
    assert "CONTROL_R2_" not in str(preflight)
    assert "PUBLICATION_CONFIRMATION_MISMATCH" in preflight["run"]
    final = steps[-1]
    assert final["name"] == "Build or reuse publication plan"
    assert "medlearn workflow plan-publication" in final["run"]
    assert set(key for key in final["env"] if key.startswith("CONTROL_R2_")) == {
        "CONTROL_R2_ENDPOINT", "CONTROL_R2_ACCESS_KEY_ID", "CONTROL_R2_SECRET_ACCESS_KEY",
    }
    assert "MEDLEARN_PROPOSE_BUNDLE_PATH" in final["env"]
    assert "medlearn-vault" not in text and "upload-artifact" not in text
    assert "GITHUB_STEP_SUMMARY" not in text and "set -x" not in text


def test_auto_publish_workflow_is_reusable_and_propose_calls_it() -> None:
    text = Path(".github/workflows/medlearn-auto-publish.yml").read_text(encoding="utf-8")
    data = yaml.load(text, Loader=yaml.BaseLoader)
    assert set(data["on"]) == {"workflow_call", "workflow_dispatch"}
    for trigger in data["on"].values():
        assert trigger["inputs"]["source_job_id"]["required"] == "true"
    assert data["permissions"] == {"contents": "read"}
    assert data["concurrency"]["group"] == "medlearn-auto-publish-${{ inputs.source_job_id }}"
    job = data["jobs"]["auto-publish"]
    assert job["if"] == "github.ref == 'refs/heads/main'"
    assert job["timeout-minutes"] == "15"
    assert "env" not in job
    for step in job["steps"]:
        if "uses" in step:
            assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", step["uses"])
    assert job["steps"][0]["with"] == {"persist-credentials": "false", "ref": "main"}
    final = job["steps"][-1]
    assert "medlearn workflow auto-publish" in final["run"]
    assert set(key for key in final["env"] if key.endswith("SECRET_ACCESS_KEY")) == {
        "CONTROL_R2_SECRET_ACCESS_KEY",
        "VAULT_R2_SECRET_ACCESS_KEY",
    }

    propose = Path(".github/workflows/medlearn-propose.yml").read_text(encoding="utf-8")
    assert "MEDLEARN_AUTO_PUBLISH_DISABLED != 'true'" in propose
    assert "./.github/workflows/medlearn-auto-publish.yml" in propose
    assert "secrets: inherit" in propose


def test_export_proposal_workflow_is_main_only_and_artifact_scoped() -> None:
    text = Path(".github/workflows/medlearn-export-proposal.yml").read_text(encoding="utf-8")
    data = yaml.load(text, Loader=yaml.BaseLoader)
    inputs = data["on"]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"source_job_id", "proposal_id", "confirmation"}
    assert all(item["required"] == "true" for item in inputs.values())
    assert data["permissions"] == {"contents": "read"}
    assert data["concurrency"] == {
        "group": "medlearn-export-proposal-${{ inputs.source_job_id }}-${{ inputs.proposal_id }}",
        "cancel-in-progress": "false",
    }
    job = data["jobs"]["export"]
    assert job["if"] == "github.ref == 'refs/heads/main'"
    assert job["timeout-minutes"] == "10"
    assert "env" not in job
    for step in job["steps"]:
        if "uses" in step:
            assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", step["uses"])
    assert job["steps"][1]["with"] == {"persist-credentials": "false", "ref": "main"}
    preflight = job["steps"][0]
    assert "CONTROL_R2_" not in str(preflight)
    assert "PROPOSAL_EXPORT_CONFIRMATION_MISMATCH" in preflight["run"]
    export = next(step for step in job["steps"] if step.get("name", "").startswith("Export exact"))
    assert "medlearn workflow export-proposal" in export["run"]
    assert "medlearn capture catalog-update" in export["run"]
    assert set(key for key in export["env"] if key.startswith("CONTROL_R2_")) == {
        "CONTROL_R2_ENDPOINT", "CONTROL_R2_ACCESS_KEY_ID", "CONTROL_R2_SECRET_ACCESS_KEY",
    }
    assert "MEDLEARN_PROPOSE_BUNDLE_PATH" in export["env"]
    upload = job["steps"][-1]
    assert upload["uses"] == "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"
    assert upload["with"]["if-no-files-found"] == "error"
