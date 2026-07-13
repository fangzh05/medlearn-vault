import hashlib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from medlearn_vault import sync_client
from medlearn_vault.cli import app
from medlearn_vault.sync_models import Manifest, ManifestArtifact

runner = CliRunner()


def test_sync_help() -> None:
    for command in ([], ["configure"], ["login"], ["logout"], ["status"], ["pull"]):
        result = runner.invoke(app, ["sync", *command, "--help"])
        assert result.exit_code == 0


def test_sync_configure_json(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    result = runner.invoke(
        app,
        [
            "sync",
            "configure",
            "--endpoint",
            "https://example.test",
            "--vault",
            str(vault),
            "--json",
        ],
        env={"MEDLEARN_HOME": str(tmp_path / "home")},
    )
    assert result.exit_code == 0
    assert '"status": "configured"' in result.stdout


def test_sync_pull_conflicts_keep_successes_and_exit_three(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = "capture_" + "a" * 32
    plan = "publication_plan_" + "b" * 32

    def item(path: str, body: bytes) -> ManifestArtifact:
        media_type = (
            "text/markdown; charset=utf-8"
            if path.endswith(".md")
            else "application/json; charset=utf-8"
        )
        return ManifestArtifact(
            path=path,
            media_type=media_type,
            content_digest="sha256:" + hashlib.sha256(body).hexdigest(),
            byte_length=len(body),
            capture_id=capture,
            publication_plan_id=plan,
        )

    root = tmp_path / "vault"
    (root / ".obsidian").mkdir(parents=True)
    monkeypatch.setenv("MEDLEARN_HOME", str(tmp_path / "home"))
    sync_client.configure("https://example.test", root)
    conflict = item(f"MedLearn/Data/Captures/{capture}.json", b"remote conflict\n")
    success = item(f"MedLearn/Captures/2026/07/{capture}.md", b"remote success\n")
    conflict_target = root / conflict.path
    conflict_target.parent.mkdir(parents=True)
    conflict_target.write_bytes(b"local edit\n")
    manifest = Manifest(manifest_version="0.1.0", artifacts=[success, conflict])
    monkeypatch.setattr(sync_client, "load_token", lambda _: "x" * 32)
    monkeypatch.setattr(
        sync_client, "_manifest", lambda *_: (manifest, '"sha256:' + "c" * 64 + '"', "downloaded")
    )
    bodies = {success.path: b"remote success\n"}
    monkeypatch.setattr(
        sync_client, "_download", lambda _, __, artifact, ___, ____: bodies[artifact.path]
    )
    result = runner.invoke(app, ["sync", "pull", "--json"])
    assert result.exit_code == 3
    assert '"downloaded_count": 1' in result.stdout
    assert f'"conflict_paths": ["{conflict.path}"]' in result.stdout
