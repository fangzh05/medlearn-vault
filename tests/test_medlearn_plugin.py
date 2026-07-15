from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "configure_medlearn_plugin_app.py"
SPEC = importlib.util.spec_from_file_location("configure_plugin", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_plugin_does_not_declare_skills() -> None:
    plugin = ROOT / "plugins" / "medlearn"
    manifest = json.loads((plugin / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert "skills" not in manifest
    assert not (plugin / "skills").exists()


def test_plugin_directly_contains_full_invocation_policy() -> None:
    plugin = ROOT / "plugins" / "medlearn"
    manifest = json.loads((plugin / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    default_prompt = manifest["interface"]["defaultPrompt"]
    assert isinstance(default_prompt, list)
    prompt_text = " ".join(default_prompt).lower()

    assert "exactly one" in prompt_text
    assert "medlearnhandoff 0.1.0" in prompt_text
    assert "do not search" in prompt_text
    assert "project memory" in prompt_text
    assert "infer" in prompt_text
    assert "rewrite" in prompt_text
    assert "learning_goals" in prompt_text
    assert "unfinished_topics" in prompt_text
    assert "submit_learning_handoff" in prompt_text
    assert "unchanged" in prompt_text
    assert "auto-approve" in prompt_text
    assert "auto-publish" in prompt_text
    assert "stable error code" in prompt_text


def test_manifest_does_not_contain_sensitive_config() -> None:
    plugin = ROOT / "plugins" / "medlearn"
    manifest_text = (plugin / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8").lower()
    sensitive_keys = [
        "client_secret",
        "access_token",
        "refresh_token",
        "allowed_subject",
        "github_actions_dispatch_token",
        "control_r2_secret",
        "auth0|"
    ]
    for secret in sensitive_keys:
        assert secret not in manifest_text


def test_configure_plugin_app_is_atomic_idempotent_and_rejects_invalid_ids(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    shutil.copytree(ROOT / "plugins", root / "plugins")
    app_id = "asdk_app_6a5610bb8abc81919fef8bb21a1ef0e3"

    manifest_path = root / "plugins" / "medlearn" / ".codex-plugin" / "plugin.json"
    orig_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    orig_default_prompt = orig_manifest["interface"]["defaultPrompt"]
    orig_long_desc = orig_manifest["interface"]["longDescription"]

    MODULE.configure(root, app_id)
    app = root / "plugins" / "medlearn" / ".app.json"
    first_app_bytes = app.read_bytes()
    first_manifest_bytes = manifest_path.read_bytes()

    MODULE.configure(root, app_id)
    assert first_app_bytes == app.read_bytes()
    assert first_manifest_bytes == manifest_path.read_bytes()

    new_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert json.loads(app.read_text(encoding="utf-8")) == {"apps": [{"id": app_id}]}
    assert new_manifest["apps"] == "./.app.json"
    assert new_manifest["interface"]["defaultPrompt"] == orig_default_prompt
    assert new_manifest["interface"]["longDescription"] == orig_long_desc

    with pytest.raises(ValueError):
        MODULE.configure(root, "https://not-an-app-id")

    with pytest.raises(ValueError, match="versioned"):
        MODULE.configure(root, "asdk_app_v_6a5610bb8abc81919fef8bb21a1ef0e3")

    MODULE.configure(root, "plugin_asdk_app_AbC123")
    assert json.loads(app.read_text(encoding="utf-8"))["apps"][0]["id"] == "plugin_asdk_app_AbC123"
