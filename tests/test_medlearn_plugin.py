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


def test_plugin_assets_are_safe_and_schema_is_identical() -> None:
    plugin = ROOT / "plugins" / "medlearn"
    manifest = json.loads((plugin / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    skill_dir = plugin / "skills" / "submit-learning-handoff"
    metadata = (skill_dir / "agents" / "openai.yaml").read_text(encoding="utf-8")
    skill = (plugin / "skills" / "submit-learning-handoff" / "SKILL.md").read_text(encoding="utf-8")
    assert "apps" not in manifest
    assert "allow_implicit_invocation: false" in metadata
    assert metadata.count("https://medlearn-cloud.fzh050531.workers.dev/mcp") == 1
    assert "token" not in metadata.lower()
    assert skill.startswith("---\nname: submit-learning-handoff\n")
    reference = skill_dir / "references" / "medlearn_handoff.schema.json"
    canonical = ROOT / "schemas" / "workflow" / "current" / "medlearn_handoff.schema.json"
    assert reference.read_bytes() == canonical.read_bytes()


def test_configure_plugin_app_is_atomic_idempotent_and_rejects_invalid_ids(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    shutil.copytree(ROOT / "plugins", root / "plugins")
    app_id = "plugin_asdk_app_AbC123"
    MODULE.configure(root, app_id)
    first = (root / "plugins" / "medlearn" / ".app.json").read_bytes()
    MODULE.configure(root, app_id)
    assert first == (root / "plugins" / "medlearn" / ".app.json").read_bytes()
    app = root / "plugins" / "medlearn" / ".app.json"
    manifest = root / "plugins" / "medlearn" / ".codex-plugin" / "plugin.json"
    assert json.loads(app.read_text(encoding="utf-8")) == {"apps": [{"id": app_id}]}
    assert json.loads(manifest.read_text(encoding="utf-8"))["apps"] == "./.app.json"
    with pytest.raises(ValueError):
        MODULE.configure(root, "https://not-an-app-id")
