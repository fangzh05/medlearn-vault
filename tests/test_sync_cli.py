import hashlib
import json
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from medlearn_vault import cli, sync_client
from medlearn_vault.cli import app
from medlearn_vault.sync_models import Manifest, ManifestArtifact, SyncError

runner = CliRunner()


def test_sync_help() -> None:
    for command in (
        [],
        ["configure"],
        ["login"],
        ["logout"],
        ["status"],
        ["pull"],
        ["install-windows"],
        ["schedule"],
        ["schedule", "install"],
        ["schedule", "status"],
        ["schedule", "remove"],
    ):
        result = runner.invoke(app, ["sync", *command, "--help"])
        assert result.exit_code == 0
    assert "--token" not in runner.invoke(app, ["sync", "login", "--help"]).stdout


def test_sync_configure_json(tmp_path: Path) -> None:
    vault = tmp_path / "用户 vault"
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
    payload = json.loads(result.stdout)
    assert result.stdout.encode("ascii")
    assert result.stdout.count("\n") == 1
    assert payload["status"] == "configured"
    assert payload["vault"] == str(vault.resolve())


def test_sync_output_is_compact_ascii_json_and_round_trips(
    capsys: pytest.CaptureFixture[str],
) -> None:
    executable = r"C:\Temp\medlearn sync 用户\venv\Scripts\medlearn.exe"
    cli._sync_output({"status": "installed", "executable": executable}, True)

    output = capsys.readouterr().out
    assert output.count("\n") == 1
    assert output.endswith("\n")
    assert "\ufffd" not in output
    assert output.encode("ascii")
    assert json.loads(output) == {"executable": executable, "status": "installed"}


def test_sync_error_json_is_compact_ascii_without_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(typer.Exit) as caught:
        cli._sync_error(SyncError("SYNC_INSTALL_FAILURE"), True)

    output = capsys.readouterr().out
    assert caught.value.exit_code == 1
    assert output.count("\n") == 1
    assert "Traceback" not in output
    assert "\ufffd" not in output
    assert output.encode("ascii")
    assert json.loads(output) == {"error_code": "SYNC_INSTALL_FAILURE", "status": "error"}


def test_sync_schedule_elevation_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    def install(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return {"status": "installed"}

    monkeypatch.setattr(cli, "install_schedule", install)
    assert runner.invoke(app, ["sync", "schedule", "install", "--json"]).exit_code == 0
    assert runner.invoke(
        app, ["sync", "schedule", "install", "--elevated", "--json"]
    ).exit_code == 0
    assert calls == [
        {"interval_minutes": 15, "what_if": False, "elevated": False},
        {"interval_minutes": 15, "what_if": False, "elevated": True},
    ]


def test_sync_schedule_elevation_required_json(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(**_: object) -> dict[str, object]:
        raise SyncError("SYNC_SCHEDULE_ELEVATION_REQUIRED")

    monkeypatch.setattr(cli, "install_schedule", fail)
    result = runner.invoke(app, ["sync", "schedule", "install", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "error_code": "SYNC_SCHEDULE_ELEVATION_REQUIRED",
        "status": "error",
    }


@pytest.mark.parametrize(
    ("command", "service"),
    [
        (["sync", "status", "--json"], "sync_status_service"),
        (["sync", "pull", "--json"], "sync_pull_service"),
        (["sync", "install-windows", "--wheel", "wheel.whl", "--json"], "install_windows"),
        (["sync", "schedule", "install", "--json"], "install_schedule"),
        (["sync", "schedule", "status", "--json"], "schedule_status_service"),
        (["sync", "schedule", "remove", "--json"], "remove_schedule"),
    ],
)
def test_sync_json_commands_round_trip_unicode_values(
    command: list[str], service: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    unicode_path = r"C:\Temp\medlearn sync 用户\venv\Scripts\medlearn.exe"
    payload: dict[str, object] = {"status": "ok", "path": unicode_path, "conflict_count": 0}
    monkeypatch.setattr(cli, service, lambda *args, **kwargs: payload)

    result = runner.invoke(app, command)

    assert result.exit_code == 0
    assert result.stdout.count("\n") == 1
    assert result.stdout.encode("ascii")
    assert json.loads(result.stdout)["path"] == unicode_path


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
    dry_run = runner.invoke(app, ["sync", "pull", "--dry-run", "--json"])
    assert dry_run.exit_code == 3
    result = runner.invoke(app, ["sync", "pull", "--confirm-first-pull", "--json"])
    assert result.exit_code == 3
    assert '"downloaded_count":1' in result.stdout
    assert f'"conflict_paths":["{conflict.path}"]' in result.stdout
