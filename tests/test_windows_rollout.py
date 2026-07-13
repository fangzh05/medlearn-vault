import json
import subprocess
import sys
from pathlib import Path

import pytest

from medlearn_vault import sync_client, windows_rollout
from medlearn_vault.sync_models import RolloutState, SyncError


def wheel(tmp_path: Path) -> Path:
    result = tmp_path / "wheelhouse" / "medlearn_vault-0.13.0-py3-none-any.whl"
    result.parent.mkdir(exist_ok=True)
    result.write_bytes(b"trusted local wheel")
    return result


def vault(tmp_path: Path) -> Path:
    result = tmp_path / "我的知识库"
    (result / ".obsidian").mkdir(parents=True)
    return result


def fake_windows_install(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if len(command) > 3 and command[1:3] == ["-m", "venv"]:
            staging = Path(command[3])
            (staging / "Scripts").mkdir(parents=True)
            (staging / "Scripts" / "python.exe").touch()
        elif "pip" in command:
            staging = Path(command[0]).parents[1]
            (staging / "Scripts" / "medlearn.exe").touch()
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(windows_rollout.subprocess, "run", run)


def test_install_plan_is_user_scoped_unicode_safe_and_non_mutating(tmp_path: Path) -> None:
    root = tmp_path / "用户 安装" / "MedLearn" / "sync-client"
    plan = windows_rollout.install_windows(wheel(tmp_path), root=root, dry_run=True)
    assert plan["status"] == "planned"
    assert plan["network_download"] is False
    assert "用户 安装" in str(plan["executable"])
    assert not root.exists()


def test_install_is_idempotent_and_replaces_partial_venv_safely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_windows_install(monkeypatch)
    root = tmp_path / "sync-client"
    (root / "venv").mkdir(parents=True)
    first = windows_rollout.install_windows(wheel(tmp_path), root=root)
    second = windows_rollout.install_windows(wheel(tmp_path), root=root)
    assert first["status"] == "installed"
    assert second["status"] == "reused"
    assert Path(str(first["executable"])).is_file()


def test_failed_upgrade_keeps_existing_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "sync-client"
    current = root / "venv" / "Scripts"
    current.mkdir(parents=True)
    client = current / "medlearn.exe"
    client.write_bytes(b"old client")
    (root / "install.json").write_text('{"wheel_sha256":"old"}\n', encoding="utf-8")
    monkeypatch.setattr(sys, "platform", "win32")

    def fail(_: list[str], **__: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, "pip")

    monkeypatch.setattr(windows_rollout.subprocess, "run", fail)
    with pytest.raises(SyncError, match="SYNC_INSTALL_FAILURE"):
        windows_rollout.install_windows(wheel(tmp_path), root=root)
    assert client.read_bytes() == b"old client"


def test_failed_upgrade_metadata_write_restores_old_client_and_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_windows_install(monkeypatch)
    root = tmp_path / "sync-client"
    current = root / "venv" / "Scripts"
    current.mkdir(parents=True)
    client = current / "medlearn.exe"
    client.write_bytes(b"old client")
    old_metadata = b'{"wheel_sha256":"old"}\n'
    (root / "install.json").write_bytes(old_metadata)

    def fail_metadata(*_: object, **__: object) -> None:
        raise OSError("metadata disk failure")

    monkeypatch.setattr(windows_rollout, "_atomic_json", fail_metadata)
    with pytest.raises(SyncError, match="SYNC_INSTALL_FAILURE"):
        windows_rollout.install_windows(wheel(tmp_path), root=root)
    assert client.read_bytes() == b"old client"
    assert (root / "install.json").read_bytes() == old_metadata


def test_configure_rejects_repository_install_and_state_inside_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = vault(tmp_path)
    monkeypatch.setenv("MEDLEARN_SYNC_INSTALL_ROOT", str(root))
    with pytest.raises(SyncError, match="SYNC_INVALID_VAULT"):
        sync_client.configure(
            "https://example.test", root, sync_client.SyncPaths(tmp_path / "state")
        )

    install = tmp_path / "install"
    monkeypatch.setenv("MEDLEARN_SYNC_INSTALL_ROOT", str(install))
    repository = vault(tmp_path / "repo")
    (repository / ".git").mkdir()
    with pytest.raises(SyncError, match="SYNC_INVALID_VAULT"):
        sync_client.configure(
            "https://example.test", repository, sync_client.SyncPaths(tmp_path / "state")
        )
    with pytest.raises(SyncError, match="SYNC_INVALID_VAULT"):
        sync_client.configure("https://example.test", root, sync_client.SyncPaths(root / "state"))

    install_parent = tmp_path / "install-parent"
    install_parent.mkdir()
    monkeypatch.setenv("MEDLEARN_SYNC_INSTALL_ROOT", str(install_parent))
    nested_vault = install_parent / "vault"
    (nested_vault / ".obsidian").mkdir(parents=True)
    with pytest.raises(SyncError, match="SYNC_INVALID_VAULT"):
        sync_client.configure(
            "https://example.test", nested_vault, sync_client.SyncPaths(tmp_path / "state-2")
        )


def test_install_plan_rejects_vault_and_repository_destinations(tmp_path: Path) -> None:
    target_vault = vault(tmp_path)
    with pytest.raises(SyncError, match="SYNC_INSTALL_LOCATION_INVALID"):
        windows_rollout.install_windows(wheel(tmp_path), root=target_vault, dry_run=True)

    repository = tmp_path / "repository"
    (repository / ".git").mkdir(parents=True)
    with pytest.raises(SyncError, match="SYNC_INSTALL_LOCATION_INVALID"):
        windows_rollout.install_windows(wheel(tmp_path), root=repository, dry_run=True)


def test_malformed_endpoint_is_mapped_to_stable_error(tmp_path: Path) -> None:
    with pytest.raises(SyncError, match="SYNC_INVALID_ENDPOINT"):
        sync_client.configure(
            "https://[bad", vault(tmp_path), sync_client.SyncPaths(tmp_path / "state")
        )


def test_first_pull_requires_dry_run_confirmation_and_blocks_schedule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = sync_client.SyncPaths(tmp_path / "state")
    root = vault(tmp_path)
    sync_client.configure("https://example.test", root, home)
    monkeypatch.setattr(sync_client, "load_token", lambda _: "x" * 32)
    monkeypatch.setattr(
        sync_client,
        "_manifest",
        lambda *_: (
            sync_client.Manifest(manifest_version="0.1.0", artifacts=[]),
            '"sha256:' + "a" * 64 + '"',
            "downloaded",
        ),
    )
    with pytest.raises(SyncError, match="SYNC_DRY_RUN_REQUIRED"):
        sync_client.pull(p=home)
    sync_client.pull(dry_run=True, p=home)
    with pytest.raises(SyncError, match="SYNC_FIRST_PULL_CONFIRMATION_REQUIRED"):
        sync_client.pull(p=home)
    monkeypatch.setenv("MEDLEARN_SYNC_INSTALL_ROOT", str(tmp_path / "sync-client"))
    with pytest.raises(SyncError) as schedule_error:
        windows_rollout.install_schedule(p=home)
    expected = (
        "SYNC_FIRST_PULL_REQUIRED" if sys.platform == "win32" else "SYNC_UNSUPPORTED_PLATFORM"
    )
    assert schedule_error.value.code == expected
    sync_client.pull(confirm_first_pull=True, p=home)
    rollout = sync_client.load_rollout(sync_client.load_config(home), home)
    assert rollout and rollout.first_pull_completed


def test_schedule_what_if_is_safe_and_uses_stable_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEDLEARN_SYNC_INSTALL_ROOT", str(tmp_path / "sync client"))
    home = sync_client.SyncPaths(tmp_path / "状态")
    plan = windows_rollout.install_schedule(what_if=True, p=home)
    assert plan["status"] == "planned"
    assert plan["task_name"] == "MedLearn Vault Sync"
    assert plan["multiple_instances"] == "IgnoreNew"
    assert plan["execution_time_limit_minutes"] == 5
    serialized = json.dumps(plan["arguments"]).lower()
    assert "token" not in serialized
    assert "authorization" not in serialized
    wrapper = windows_rollout.scheduled_wrapper(home.home, Path(str(plan["client_executable"])))
    assert "MEDLEARN_HOME" in wrapper
    assert "Authorization" not in wrapper


def test_schedule_registers_and_removes_without_vault_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "sync-client"
    monkeypatch.setenv("MEDLEARN_SYNC_INSTALL_ROOT", str(root))
    monkeypatch.setattr(sys, "platform", "win32")
    home = sync_client.SyncPaths(tmp_path / "state")
    sync_client.configure("https://example.test", vault(tmp_path), home)
    config = sync_client.load_config(home)
    sync_client._atomic_json(
        home.rollout,
        RolloutState(
            endpoint=config.endpoint,
            vault_path=config.vault_path,
            dry_run_succeeded=True,
            first_pull_completed=True,
        ),
    )
    client = windows_rollout.executable(root)
    client.parent.mkdir(parents=True)
    client.touch()
    monkeypatch.setattr(windows_rollout, "load_token", lambda _: "x" * 32)
    calls: list[list[str]] = []

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(windows_rollout.subprocess, "run", run)
    installed = windows_rollout.install_schedule(p=home)
    removed = windows_rollout.remove_schedule()
    assert installed["status"] == "installed"
    assert removed["status"] == "removed"
    assert all("token" not in " ".join(command).lower() for command in calls)
    assert not (Path(config.vault_path) / "MedLearn").exists()


def test_failed_schedule_registration_restores_previous_definition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "sync-client"
    monkeypatch.setenv("MEDLEARN_SYNC_INSTALL_ROOT", str(root))
    monkeypatch.setattr(sys, "platform", "win32")
    home = sync_client.SyncPaths(tmp_path / "state")
    sync_client.configure("https://example.test", vault(tmp_path), home)
    config = sync_client.load_config(home)
    sync_client._atomic_json(
        home.rollout,
        RolloutState(
            endpoint=config.endpoint,
            vault_path=config.vault_path,
            dry_run_succeeded=True,
            first_pull_completed=True,
        ),
    )
    client = windows_rollout.executable(root)
    client.parent.mkdir(parents=True)
    client.touch()
    monkeypatch.setattr(windows_rollout, "load_token", lambda _: "x" * 32)
    metadata = root / "schedule.json"
    wrapper = root / "run-scheduled.ps1"
    old_metadata = b'{"old":true}\n'
    old_wrapper = b"old wrapper\n"
    metadata.parent.mkdir(parents=True, exist_ok=True)
    metadata.write_bytes(old_metadata)
    wrapper.write_bytes(old_wrapper)

    def fail(*_: object, **__: object) -> None:
        raise subprocess.CalledProcessError(1, "powershell.exe")

    monkeypatch.setattr(windows_rollout.subprocess, "run", fail)
    with pytest.raises(SyncError, match="SYNC_SCHEDULE_FAILURE"):
        windows_rollout.install_schedule(p=home)
    assert metadata.read_bytes() == old_metadata
    assert wrapper.read_bytes() == old_wrapper


def test_scheduled_log_is_sanitized_and_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = sync_client.SyncPaths(tmp_path / "state")
    result = {
        "status": "synced",
        "manifest_status": "downloaded",
        "remote_count": 1,
        "downloaded_count": 1,
        "unchanged_count": 0,
        "conflict_count": 1,
        "conflict_paths": ["MedLearn/Captures/2026/07/capture_safe.md", "not-safe\npath"],
        "would_download_count": 0,
    }
    monkeypatch.setattr(sync_client, "pull", lambda **_: result)
    for _ in range(51):
        sync_client.scheduled_pull(p=home)
    records = [
        json.loads(line) for line in home.scheduled_log.read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 50
    assert records[-1]["conflict_paths"] == ["MedLearn/Captures/2026/07/capture_safe.md"]
    assert "authorization" not in home.scheduled_log.read_text(encoding="utf-8").lower()


def test_rollout_state_remains_token_free() -> None:
    assert "token" not in RolloutState.model_json_schema()
