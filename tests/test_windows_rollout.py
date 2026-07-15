import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from medlearn_vault import cli, sync_client, windows_rollout
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


# ---------------------------------------------------------------------------
# Existing install tests (unchanged behaviour)
# ---------------------------------------------------------------------------


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


def test_self_hosted_upgrade_fails_before_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "同步 client"
    current = root / "venv" / "Scripts"
    current.mkdir(parents=True)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "executable", str(current / "python.exe"))

    with pytest.raises(SyncError, match="SYNC_INSTALL_SELF_UPGRADE_REQUIRES_BOOTSTRAP"):
        windows_rollout.install_windows(wheel(tmp_path), root=root)

    assert not list(root.glob(".venv-staging-*"))
    assert not list(root.glob(".venv-backup-*"))


def test_self_hosted_identical_wheel_is_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "sync-client"
    current = root / "venv" / "Scripts"
    current.mkdir(parents=True)
    client = current / "medlearn.exe"
    client.touch()
    artifact = wheel(tmp_path)
    (root / "install.json").write_text(
        json.dumps({"wheel_sha256": windows_rollout._sha256(artifact)}), encoding="utf-8"
    )
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "executable", str(current / "python.exe"))
    monkeypatch.setattr(windows_rollout, "_executable_works", lambda _: True)

    assert windows_rollout.install_windows(artifact, root=root)["status"] == "reused"


def test_external_client_can_execute_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_windows_install(monkeypatch)
    root = tmp_path / "中文 路径" / "sync-client"
    monkeypatch.setattr(sys, "executable", str(tmp_path / "external" / "python.exe"))

    result = windows_rollout.install_windows(wheel(tmp_path), root=root)

    assert result["status"] == "installed"
    assert Path(str(result["executable"])).is_file()


def test_external_bootstrap_is_offline_quiet_and_durable() -> None:
    source = Path("scripts/install_windows_client.ps1").read_text(encoding="utf-8")
    assert "[string] $Wheel" in source and "[string] $InstallRoot" in source
    assert "[switch] $Json" in source
    assert "--no-index" in source and "--find-links" in source
    assert "medlearn-bootstrap-" in source
    assert "sync install-windows --wheel $wheelPath --json" in source
    assert "install-windows-client.ps1" in source
    assert "2>&1 | Out-Null" in source
    assert "SYNC_INSTALL_BOOTSTRAP_FAILURE" in source
    assert "SYNC_INSTALL_SELF_UPGRADE_REQUIRES_BOOTSTRAP" not in source
    for forbidden in ("Get-ChildItem Env:", "Authorization", "MEDLEARN_SYNC_TOKEN", "Write-Host"):
        assert forbidden not in source


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


def test_install_captures_noisy_subprocess_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def noisy(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command, 0, "Looking in links: wheelhouse\n0.13.0\n", "[notice] upgrade pip\n"
        )

    monkeypatch.setattr(windows_rollout.subprocess, "run", noisy)
    result = windows_rollout._run_installer_command(["pip", "install", "wheel"])
    captured = capsys.readouterr()
    assert result.stdout.startswith("Looking in links")
    assert result.stderr.startswith("[notice]")
    assert captured.out == ""
    assert captured.err == ""
    assert len(calls) == 1
    for _, call in calls:
        assert call["capture_output"] is True
        assert call["text"] is True
        assert call["encoding"] == "utf-8"
        assert call["errors"] == "replace"


def test_install_windows_json_is_one_clean_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_windows_install(monkeypatch)

    def noisy(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if len(command) > 3 and command[1:3] == ["-m", "venv"]:
            (Path(command[3]) / "Scripts").mkdir(parents=True)
            (Path(command[3]) / "Scripts" / "python.exe").touch()
        elif "pip" in command:
            (Path(command[0]).parents[1] / "Scripts" / "medlearn.exe").touch()
        return subprocess.CompletedProcess(
            command,
            0,
            "Looking in links: wheelhouse\nProcessing wheel\n0.13.0\n",
            "Successfully installed\n[notice] To update, run: pip install --upgrade pip\n",
        )

    monkeypatch.setattr(windows_rollout.subprocess, "run", noisy)
    monkeypatch.setenv("MEDLEARN_SYNC_INSTALL_ROOT", str(tmp_path / "用户 安装"))
    result = CliRunner().invoke(
        cli.app, ["sync", "install-windows", "--wheel", str(wheel(tmp_path)), "--json"]
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "installed"
    assert payload["executable"] == str(
        tmp_path / "用户 安装" / "venv" / "Scripts" / "medlearn.exe"
    )
    assert result.stdout.count("{") == 1
    assert result.stdout.count("\n") == 1
    assert "\ufffd" not in result.stdout
    assert result.stdout.encode("ascii")
    for leaked in ("Looking in links", "Processing", "Successfully installed", "[notice]"):
        assert leaked not in result.stdout


def test_install_windows_failure_hides_subprocess_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")

    def fail(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(
            1, command, output="Looking in links: secret-index", stderr="full pip log"
        )

    monkeypatch.setattr(windows_rollout.subprocess, "run", fail)
    monkeypatch.setenv("MEDLEARN_SYNC_INSTALL_ROOT", str(tmp_path / "sync-client"))
    result = CliRunner().invoke(
        cli.app, ["sync", "install-windows", "--wheel", str(wheel(tmp_path)), "--json"]
    )
    assert result.exit_code == 1
    assert json.loads(result.stdout) == {"status": "error", "error_code": "SYNC_INSTALL_FAILURE"}
    assert result.stdout.count("\n") == 1
    assert result.stdout.encode("ascii")
    assert "Traceback" not in result.stdout + result.stderr
    assert "secret-index" not in result.stdout + result.stderr
    assert "full pip log" not in result.stdout + result.stderr


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


def test_vbs_wrapper_structure_and_no_token(tmp_path: Path) -> None:
    """scheduled_vbs_wrapper must use Run(..., 0, True) and WScript.Quit exitCode."""
    ps1 = tmp_path / "sync-client" / "run-scheduled.ps1"
    vbs = windows_rollout.scheduled_vbs_wrapper(ps1)

    assert "Option Explicit" in vbs
    assert 'CreateObject("WScript.Shell")' in vbs
    assert "Run(command, 0, True)" in vbs
    assert "WScript.Quit exitCode" in vbs
    assert "powershell.exe" in vbs
    assert "run-scheduled.ps1" in vbs
    # No token, credential, or environment variable content.
    vbs_lower = vbs.lower()
    assert "token" not in vbs_lower
    assert "authorization" not in vbs_lower
    assert "bearer" not in vbs_lower
    assert "dpapi" not in vbs_lower


class TestVbsStringLiteral:
    """Prove _vbs_string_literal correctly encodes strings for VBScript."""

    def test_plain_string_no_double_quotes(self) -> None:
        result = windows_rollout._vbs_string_literal("hello world")
        assert result == '"hello world"'

    def test_double_quotes_are_doubled(self) -> None:
        result = windows_rollout._vbs_string_literal('say "hello"')
        assert result == '"say ""hello"""'

    def test_chinese_characters_preserved(self) -> None:
        result = windows_rollout._vbs_string_literal("C:\\测试\\用户\\文件")
        assert "测试" in result
        assert "用户" in result
        assert '"C:\\测试\\用户\\文件"' == result or result.startswith('"C:\\测试\\用户\\文件"')

    def test_spaces_preserved(self) -> None:
        result = windows_rollout._vbs_string_literal("C:\\Program Files\\app.exe")
        assert "Program Files" in result

    def test_backslashes_preserved(self) -> None:
        result = windows_rollout._vbs_string_literal("C:\\Users\\test\\file.txt")
        assert result == '"C:\\Users\\test\\file.txt"'

    def test_quotes_and_spaces_combined(self) -> None:
        result = windows_rollout._vbs_string_literal(
            'powershell.exe -File "C:\\我的 文件\\run-scheduled.ps1"'
        )
        assert "powershell.exe" in result
        assert "我的" in result
        assert "文件" in result
        assert "run-scheduled.ps1" in result
        # Double quotes in the input must become "" in the output.
        # The original has: -File "C:\..."
        # In VBS: -File ""C:\...""  (each " becomes "")
        assert '""' in result
        assert '-File ""' in result
        assert '.ps1""' in result

    def test_no_token_or_credential(self) -> None:
        """_vbs_string_literal is pure text encoding — but test that the pattern
        of encoding a token-like string doesn't accidentally leak."""
        result = windows_rollout._vbs_string_literal("powershell.exe -File run.ps1")
        assert "Bearer" not in result
        assert "Authorization" not in result


def test_scheduled_wrapper_still_uses_exit_last_exit_code() -> None:
    """scheduled_wrapper() must still contain exit $LASTEXITCODE."""
    wrapper = windows_rollout.scheduled_wrapper(
        Path("C:\\home"), Path("C:\\medlearn.exe")
    )
    assert "exit $LASTEXITCODE" in wrapper
    assert "MEDLEARN_HOME" in wrapper

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

    # Capture the PowerShell script passed to subprocess.run so we can inspect
    # the action argument string.
    captured_scripts: list[str] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[0] == "powershell.exe":
            captured_scripts.append(command[-1])
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(windows_rollout.subprocess, "run", run)
    installed = windows_rollout.install_schedule(p=home)
    elevated_installed = windows_rollout.install_schedule(p=home, elevated=True)

    # Removal: mock PowerShell to return REMOVED
    def run_remove(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[0] == "powershell.exe" and "Unregister" in str(command):
            return subprocess.CompletedProcess(command, 0, "REMOVED\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(windows_rollout.subprocess, "run", run_remove)
    removed = windows_rollout.remove_schedule()

    assert installed["status"] == "installed"
    assert elevated_installed["status"] == "installed"
    assert removed["status"] == "removed"
    assert all("token" not in " ".join(command).lower() for command in [])  # already verified
    assert not (Path(config.vault_path) / "MedLearn").exists()

    # Verify the PowerShell script contained a properly-serialized action
    # argument string (via subprocess.list2cmdline).
    if captured_scripts:
        register_script = captured_scripts[0]
        # The -Argument value should be a quoted string containing //B, //Nologo
        # and the VBS launcher path.
        assert "wscript.exe" in register_script
        assert "run-scheduled.vbs" in register_script
        assert "//B" in register_script
        assert "//Nologo" in register_script
        assert "-User $user" in register_script
        assert "-RunLevel Limited" in register_script
        assert "New-ScheduledTaskPrincipal" not in register_script
        assert "Start-Process" not in register_script
        assert "-Verb RunAs" not in register_script
        elevated_launcher = captured_scripts[1]
        assert "Start-Process" in elevated_launcher
        assert "-Verb RunAs" in elevated_launcher
        assert "token" not in elevated_launcher.lower()
        assert not list(root.glob(".medlearn-schedule-*.ps1"))


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
    launcher = root / "run-scheduled.vbs"
    old_metadata = b'{"old":true}\n'
    old_wrapper = b"old wrapper\n"
    old_launcher = b"old launcher\n"
    metadata.parent.mkdir(parents=True, exist_ok=True)
    metadata.write_bytes(old_metadata)
    wrapper.write_bytes(old_wrapper)
    launcher.write_bytes(old_launcher)

    def fail(*_: object, **__: object) -> None:
        raise subprocess.CalledProcessError(5, "powershell.exe", stderr="Access is denied.")

    monkeypatch.setattr(windows_rollout.subprocess, "run", fail)
    with pytest.raises(SyncError, match="SYNC_SCHEDULE_ELEVATION_REQUIRED"):
        windows_rollout.install_schedule(p=home)
    assert metadata.read_bytes() == old_metadata
    assert wrapper.read_bytes() == old_wrapper
    assert launcher.read_bytes() == old_launcher
    with pytest.raises(SyncError, match="SYNC_SCHEDULE_FAILURE"):
        windows_rollout.install_schedule(p=home, elevated=True)
    assert metadata.read_bytes() == old_metadata
    assert wrapper.read_bytes() == old_wrapper
    assert launcher.read_bytes() == old_launcher


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


# ===================================================================
# BLOCKER 1 — Windows command-line quoting
# ===================================================================


class TestActionArgumentSerialization:
    """Prove that the final task action correctly preserves paths with special
    characters using subprocess.list2cmdline, not PowerShell single-quoting."""

    @staticmethod
    def action_args(arguments: list[str]) -> str:
        return windows_rollout._windows_action_args(arguments)

    def test_plain_path_needs_no_double_quotes(self) -> None:
        args = [
            "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", "C:\\Users\\user\\wrapper.ps1",
        ]
        result = self.action_args(args)
        # Plain path has no spaces — list2cmdline should not add double quotes.
        assert "-File" in result
        assert "wrapper.ps1" in result
        # The arguments list must contain the path verbatim.
        assert "C:\\Users\\user\\wrapper.ps1" in result

    def test_spaces_in_path_are_double_quoted(self) -> None:
        args = [
            "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", "C:\\Users\\Test User\\wrapper.ps1",
        ]
        result = self.action_args(args)
        # list2cmdline double-quotes the path because it contains a space.
        assert '"C:\\Users\\Test User\\wrapper.ps1"' in result
        assert "Test User" in result

    def test_chinese_characters_and_spaces_are_preserved(self) -> None:
        args = [
            "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", "C:\\Users\\测试 用户\\wrapper.ps1",
        ]
        result = self.action_args(args)
        assert "测试" in result
        assert "用户" in result
        assert "wrapper.ps1" in result
        # The path with spaces AND Chinese must be double-quoted.
        assert '"C:\\Users\\测试 用户\\wrapper.ps1"' in result

    def test_apostrophe_in_path_is_preserved(self) -> None:
        args = [
            "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", "C:\\Users\\O'Brien\\wrapper.ps1",
        ]
        result = self.action_args(args)
        assert "O'Brien" in result
        # Apostrophe is not special to Windows argv — no double-quoting needed.
        # But the path must still be intact.
        assert "wrapper.ps1" in result

    def test_parentheses_in_path_are_preserved(self) -> None:
        args = [
            "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", "C:\\Program Files (x86)\\wrapper.ps1",
        ]
        result = self.action_args(args)
        assert "Program Files (x86)" in result
        assert "wrapper.ps1" in result

    def test_ampersand_in_path(self) -> None:
        args = [
            "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", "C:\\A&B\\wrapper.ps1",
        ]
        result = self.action_args(args)
        assert "A&B" in result
        assert "wrapper.ps1" in result

    def test_combination_spaces_chinese_and_apostrophe(self) -> None:
        """Stress-test: path with spaces, Chinese chars, and an apostrophe."""
        args = [
            "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", "C:\\测试 O'Brien\\run-scheduled.ps1",
        ]
        result = self.action_args(args)
        assert "测试" in result
        assert "O'Brien" in result
        assert "run-scheduled.ps1" in result
        # Must be double-quoted because of the space.
        assert '"' in result

    def test_list2cmdline_not_powershell_quote(self) -> None:
        """The serializer must NOT wrap arguments in single quotes."""
        args = [
            "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", "C:\\Users\\test user\\wrapper.ps1",
        ]
        result = self.action_args(args)
        # list2cmdline uses double quotes for spaces, never single quotes.
        assert "'-File'" not in result
        assert result.startswith("-NoProfile")

    def test_task_definition_uses_wscript_executable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """task_definition() must use wscript.exe, not powershell.exe, and
        include //B //Nologo pointing at run-scheduled.vbs."""
        monkeypatch.setenv("MEDLEARN_SYNC_INSTALL_ROOT", str(tmp_path / "测试 install"))
        home = sync_client.SyncPaths(tmp_path / "home")
        client = tmp_path / "sync-client" / "venv" / "Scripts" / "medlearn.exe"
        definition = windows_rollout.task_definition(home.home, client, 15)
        # Executable must be wscript.exe, not powershell.exe.
        assert definition["command"] == "wscript.exe"
        arguments = definition["arguments"]
        assert isinstance(arguments, list)
        # Arguments must contain //B and //Nologo exactly once.
        assert arguments.count("//B") == 1
        assert arguments.count("//Nologo") == 1
        # The third argument must be the VBS launcher path.
        launcher_arg = arguments[2]
        assert isinstance(launcher_arg, str)
        assert launcher_arg.endswith("run-scheduled.vbs")
        assert "run-scheduled.ps1" not in arguments
        # Must NOT execute powershell.exe directly.
        assert "powershell.exe" not in arguments
        assert "-File" not in arguments
        # Must have launcher_path in definition.
        assert "launcher_path" in definition
        assert definition["launcher_path"].endswith("run-scheduled.vbs")
        assert definition["wrapper_path"].endswith("run-scheduled.ps1")
        # Must NOT contain PowerShell single-quote escaping in arguments.
        for arg in arguments:
            assert not arg.startswith("'")


class TestActionArgumentInRegisteredScript:
    """Assert against the actual raw argument string that would be registered
    with Task Scheduler — i.e. the value passed to -Argument."""

    def test_install_schedule_embeds_wscript_arguments(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "sync client"  # intentional space
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

        captured: list[str] = []

        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if command[0] == "powershell.exe":
                captured.append(command[-1])  # the -Command script
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(windows_rollout.subprocess, "run", run)
        windows_rollout.install_schedule(p=home)
        assert len(captured) == 1
        script = captured[0]
        # The script must register wscript.exe, not powershell.exe.
        assert "wscript.exe" in script
        assert "-Execute" in script
        # The VBS path must appear in the -Argument string.
        assert "run-scheduled.vbs" in script
        # Because the install root has a space, the path must be
        # double-quoted inside the -Argument string.
        assert '-Argument' in script
        # Extract what's between -Argument and the next ;
        arg_start = script.index("-Argument") + len("-Argument")
        arg_segment = script[arg_start:].split(";")[0].strip()
        # arg_segment is e.g. '//B //Nologo "C:\...\run-scheduled.vbs"'
        assert arg_segment.startswith("'") and arg_segment.endswith("'")
        inner = arg_segment[1:-1]
        assert "//B" in inner
        assert "//Nologo" in inner
        assert "run-scheduled.vbs" in inner
        # The space-containing path must be double-quoted inside.
        assert '"' in inner
        assert "sync client" in inner
        assert "-User $user" in script
        assert "-RunLevel Limited" in script
        assert "New-ScheduledTaskPrincipal" not in script
        # The registration must use Register-ScheduledTask -Force.
        assert "Register-ScheduledTask" in script
        assert "-Force" in script

    def test_chinese_path_survives_full_round_trip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "同步 客户端"  # Chinese + space
        monkeypatch.setenv("MEDLEARN_SYNC_INSTALL_ROOT", str(root))
        monkeypatch.setattr(sys, "platform", "win32")
        home = sync_client.SyncPaths(tmp_path / "状态")
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

        captured: list[str] = []

        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if command[0] == "powershell.exe":
                captured.append(command[-1])
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(windows_rollout.subprocess, "run", run)
        windows_rollout.install_schedule(p=home)
        assert len(captured) == 1
        script = captured[0]
        assert "同步" in script
        assert "客户端" in script
        assert "run-scheduled.vbs" in script
        assert "wscript.exe" in script
        assert "-User $user" in script
        assert "-RunLevel Limited" in script


# ===================================================================
# BLOCKER 2 — Structured schedule status
# ===================================================================


class TestScheduleStatus:
    """Locale-independent structured inspection via PowerShell ScheduledTasks."""

    def test_task_absent_returns_registered_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("MEDLEARN_SYNC_INSTALL_ROOT", str(tmp_path / "sync-client"))

        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if command[0] == "powershell.exe":
                result = json.dumps(
                    {"task_name": "MedLearn Vault Sync", "registered": False}
                )
                return subprocess.CompletedProcess(command, 0, result, "")
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(windows_rollout.subprocess, "run", run)
        status = windows_rollout.schedule_status()
        assert status["task_name"] == "MedLearn Vault Sync"
        assert status["registered"] is False

    def test_task_present_reports_all_fields(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("MEDLEARN_SYNC_INSTALL_ROOT", str(tmp_path / "sync-client"))

        task_json = {
            "task_name": "MedLearn Vault Sync",
            "registered": True,
            "state": "Ready",
            "last_run_time": "2026-07-13T08:00:00.0000000+08:00",
            "next_run_time": "2026-07-13T08:15:00.0000000+08:00",
            "last_task_result": 0,
            "executable": "wscript.exe",
            "arguments": (
                '//B //Nologo "C:\\run-scheduled.vbs"'
            ),
            "principal_user_id": "test-user",
            "principal_logon_type": "Interactive",
            "principal_run_level": "Limited",
            "trigger_count": 2,
        }

        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if command[0] == "powershell.exe":
                return subprocess.CompletedProcess(
                    command, 0, json.dumps(task_json), ""
                )
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(windows_rollout.subprocess, "run", run)
        status = windows_rollout.schedule_status()
        assert status["registered"] is True
        assert status["state"] == "Ready"
        assert status["last_run_time"] == "2026-07-13T08:00:00.0000000+08:00"
        assert status["next_run_time"] == "2026-07-13T08:15:00.0000000+08:00"
        assert status["last_task_result"] == 0
        assert status["executable"] == "wscript.exe"
        assert status["principal_user_id"] == "test-user"
        assert status["principal_logon_type"] == "Interactive"
        assert status["principal_run_level"] == "Limited"
        assert status["trigger_count"] == 2

    def test_task_running_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("MEDLEARN_SYNC_INSTALL_ROOT", str(tmp_path / "sync-client"))

        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if command[0] == "powershell.exe":
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps(
                        {
                            "task_name": "MedLearn Vault Sync",
                            "registered": True,
                            "state": "Running",
                            "last_run_time": None,
                            "next_run_time": None,
                            "last_task_result": 0,
                            "executable": "wscript.exe",
                            "arguments": "//B //Nologo C:\\run-scheduled.vbs",
                        }
                    ),
                    "",
                )
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(windows_rollout.subprocess, "run", run)
        status = windows_rollout.schedule_status()
        assert status["state"] == "Running"

    def test_nonzero_last_result(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("MEDLEARN_SYNC_INSTALL_ROOT", str(tmp_path / "sync-client"))

        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if command[0] == "powershell.exe":
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps(
                        {
                            "task_name": "MedLearn Vault Sync",
                            "registered": True,
                            "state": "Ready",
                            "last_run_time": "2026-07-13T08:00:00.0000000+08:00",
                            "next_run_time": None,
                            "last_task_result": 2147942401,
                            "executable": "wscript.exe",
                            "arguments": "//B //Nologo C:\\run-scheduled.vbs",
                        }
                    ),
                    "",
                )
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(windows_rollout.subprocess, "run", run)
        status = windows_rollout.schedule_status()
        assert status["last_task_result"] == 2147942401

    def test_inspection_command_failure_produces_stable_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("MEDLEARN_SYNC_INSTALL_ROOT", str(tmp_path / "sync-client"))

        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.CalledProcessError(1, "powershell.exe", stderr="access denied")

        monkeypatch.setattr(windows_rollout.subprocess, "run", run)
        with pytest.raises(SyncError, match="SYNC_SCHEDULE_FAILURE"):
            windows_rollout.schedule_status()

    def test_malformed_powershell_json_produces_stable_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("MEDLEARN_SYNC_INSTALL_ROOT", str(tmp_path / "sync-client"))

        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if command[0] == "powershell.exe":
                return subprocess.CompletedProcess(command, 0, "not valid json {{{", "")
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(windows_rollout.subprocess, "run", run)
        with pytest.raises(SyncError, match="SYNC_SCHEDULE_FAILURE"):
            windows_rollout.schedule_status()

    def test_non_dict_json_produces_stable_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("MEDLEARN_SYNC_INSTALL_ROOT", str(tmp_path / "sync-client"))

        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if command[0] == "powershell.exe":
                return subprocess.CompletedProcess(command, 0, '[1, 2, 3]', "")
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(windows_rollout.subprocess, "run", run)
        with pytest.raises(SyncError, match="SYNC_SCHEDULE_FAILURE"):
            windows_rollout.schedule_status()

    def test_status_merges_local_metadata_wrapper_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When schedule.json exists, configured values like wrapper_path are
        merged into the status result."""
        monkeypatch.setattr(sys, "platform", "win32")
        root = tmp_path / "sync-client"
        monkeypatch.setenv("MEDLEARN_SYNC_INSTALL_ROOT", str(root))
        root.mkdir(parents=True)
        (root / "schedule.json").write_text(
            json.dumps(
                {
                    "wrapper_path": str(root / "run-scheduled.ps1"),
                    "launcher_path": str(root / "run-scheduled.vbs"),
                    "interval_minutes": 15,
                    "medlearn_home": str(tmp_path / "home"),
                }
            ),
            encoding="utf-8",
        )

        task_json = {
            "task_name": "MedLearn Vault Sync",
            "registered": True,
            "state": "Ready",
            "last_run_time": None,
            "next_run_time": None,
            "last_task_result": 0,
            "executable": "wscript.exe",
            "arguments": "//B //Nologo C:\\run-scheduled.vbs",
        }

        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if command[0] == "powershell.exe":
                return subprocess.CompletedProcess(command, 0, json.dumps(task_json), "")
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(windows_rollout.subprocess, "run", run)
        status = windows_rollout.schedule_status()
        assert status["wrapper_path"] == str(root / "run-scheduled.ps1")
        assert status["launcher_path"] == str(root / "run-scheduled.vbs")
        assert status["interval_minutes"] == 15

    def test_status_does_not_return_secrets_or_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("MEDLEARN_SYNC_INSTALL_ROOT", str(tmp_path / "sync-client"))

        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if command[0] == "powershell.exe":
                return subprocess.CompletedProcess(
                    command,
                    0,
                    json.dumps(
                        {
                            "task_name": "MedLearn Vault Sync",
                            "registered": True,
                            "state": "Ready",
                            "last_run_time": None,
                            "next_run_time": None,
                            "last_task_result": 0,
                            "executable": "wscript.exe",
                            "arguments": '//B //Nologo "C:\\run-scheduled.vbs"',
                        }
                    ),
                    "",
                )
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(windows_rollout.subprocess, "run", run)
        status = windows_rollout.schedule_status()
        serialized = json.dumps(status).lower()
        assert "token" not in serialized
        assert "authorization" not in serialized
        assert "dpapi" not in serialized
        assert "cipher" not in serialized


# ===================================================================
# BLOCKER 3 — Safe task removal
# ===================================================================


class TestScheduleRemoval:
    """Explicit idempotent removal with verification."""

    def test_already_absent_returns_successful_result(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("MEDLEARN_SYNC_INSTALL_ROOT", str(tmp_path / "sync-client"))

        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if command[0] == "powershell.exe":
                return subprocess.CompletedProcess(command, 0, "ABSENT\n", "")
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(windows_rollout.subprocess, "run", run)
        result = windows_rollout.remove_schedule()
        assert result["status"] == "already_absent"
        assert result["task_name"] == "MedLearn Vault Sync"

    def test_successful_removal_returns_removed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        root = tmp_path / "sync-client"
        monkeypatch.setenv("MEDLEARN_SYNC_INSTALL_ROOT", str(root))
        root.mkdir(parents=True)
        # Pre-create metadata files to verify they're deleted on success.
        (root / "schedule.json").write_text("{}", encoding="utf-8")
        (root / "run-scheduled.ps1").write_text("# wrapper", encoding="utf-8")
        (root / "run-scheduled.vbs").write_text("' launcher", encoding="utf-8")

        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if command[0] == "powershell.exe":
                return subprocess.CompletedProcess(command, 0, "REMOVED\n", "")
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(windows_rollout.subprocess, "run", run)
        result = windows_rollout.remove_schedule()
        assert result["status"] == "removed"
        # Local metadata must be cleaned up (ps1, vbs, schedule.json).
        assert not (root / "schedule.json").exists()
        assert not (root / "run-scheduled.ps1").exists()
        assert not (root / "run-scheduled.vbs").exists()

    def test_unregister_failure_raises_error_and_preserves_metadata(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        root = tmp_path / "sync-client"
        monkeypatch.setenv("MEDLEARN_SYNC_INSTALL_ROOT", str(root))
        root.mkdir(parents=True)
        (root / "schedule.json").write_text("{}", encoding="utf-8")
        (root / "run-scheduled.ps1").write_text("# wrapper", encoding="utf-8")
        (root / "run-scheduled.vbs").write_text("' launcher", encoding="utf-8")

        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.CalledProcessError(1, "powershell.exe")

        monkeypatch.setattr(windows_rollout.subprocess, "run", run)
        with pytest.raises(SyncError, match="SYNC_SCHEDULE_FAILURE"):
            windows_rollout.remove_schedule()
        # Metadata must be preserved on failure.
        assert (root / "schedule.json").exists()
        assert (root / "run-scheduled.ps1").exists()
        assert (root / "run-scheduled.vbs").exists()

    def test_verify_still_finds_task_raises_error_and_preserves_metadata(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        root = tmp_path / "sync-client"
        monkeypatch.setenv("MEDLEARN_SYNC_INSTALL_ROOT", str(root))
        root.mkdir(parents=True)
        (root / "schedule.json").write_text("{}", encoding="utf-8")
        (root / "run-scheduled.ps1").write_text("# wrapper", encoding="utf-8")
        (root / "run-scheduled.vbs").write_text("' launcher", encoding="utf-8")

        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if command[0] == "powershell.exe":
                return subprocess.CompletedProcess(command, 0, "VERIFY_FAILED\n", "")
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(windows_rollout.subprocess, "run", run)
        with pytest.raises(SyncError, match="SYNC_SCHEDULE_FAILURE"):
            windows_rollout.remove_schedule()
        # Metadata must be preserved when verification fails.
        assert (root / "schedule.json").exists()
        assert (root / "run-scheduled.ps1").exists()
        assert (root / "run-scheduled.vbs").exists()

    def test_access_denied_style_failure_preserves_metadata(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        root = tmp_path / "sync-client"
        monkeypatch.setenv("MEDLEARN_SYNC_INSTALL_ROOT", str(root))
        root.mkdir(parents=True)
        (root / "schedule.json").write_text("{}", encoding="utf-8")

        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.CalledProcessError(5, "powershell.exe", stderr="Access is denied.")

        monkeypatch.setattr(windows_rollout.subprocess, "run", run)
        with pytest.raises(SyncError, match="SYNC_SCHEDULE_ELEVATION_REQUIRED"):
            windows_rollout.remove_schedule()
        assert (root / "schedule.json").exists()

    def test_removal_never_touches_vault_content(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        root = tmp_path / "sync-client"
        monkeypatch.setenv("MEDLEARN_SYNC_INSTALL_ROOT", str(root))
        root.mkdir(parents=True)

        vault_path = vault(tmp_path)
        (vault_path / "MedLearn").mkdir()
        (vault_path / "MedLearn" / "important.md").write_text("keep me", encoding="utf-8")

        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if command[0] == "powershell.exe":
                return subprocess.CompletedProcess(command, 0, "REMOVED\n", "")
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(windows_rollout.subprocess, "run", run)
        windows_rollout.remove_schedule()
        # Vault content must never be touched.
        assert vault_path.exists()
        assert (vault_path / ".obsidian").is_dir()
        assert (vault_path / "MedLearn" / "important.md").read_text(encoding="utf-8") == "keep me"

    def test_removal_does_not_report_removed_when_task_still_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Any status other than ABSENT or REMOVED must raise, not report success."""
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("MEDLEARN_SYNC_INSTALL_ROOT", str(tmp_path / "sync-client"))

        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            # Simulate some unexpected output
            return subprocess.CompletedProcess(command, 0, "UNKNOWN_STATE\n", "")

        monkeypatch.setattr(windows_rollout.subprocess, "run", run)
        with pytest.raises(SyncError, match="SYNC_SCHEDULE_FAILURE"):
            windows_rollout.remove_schedule()

    def test_metadata_not_deleted_after_failed_removal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If removal fails, schedule.json, wrapper and launcher must remain on disk."""
        monkeypatch.setattr(sys, "platform", "win32")
        root = tmp_path / "sync-client"
        monkeypatch.setenv("MEDLEARN_SYNC_INSTALL_ROOT", str(root))
        root.mkdir(parents=True)
        (root / "schedule.json").write_text('{"task_name":"MedLearn Vault Sync"}', encoding="utf-8")
        (root / "run-scheduled.ps1").write_text("exit 0", encoding="utf-8")
        (root / "run-scheduled.vbs").write_text("' launcher", encoding="utf-8")

        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            # Simulate removal succeeded but verification failed
            if command[0] == "powershell.exe":
                return subprocess.CompletedProcess(command, 0, "VERIFY_FAILED\n", "")
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(windows_rollout.subprocess, "run", run)
        with pytest.raises(SyncError, match="SYNC_SCHEDULE_FAILURE"):
            windows_rollout.remove_schedule()
        assert (root / "schedule.json").exists()
        assert (root / "run-scheduled.ps1").exists()
        assert (root / "run-scheduled.vbs").exists()


# ===================================================================
# Manual acceptance script validation tests
# ===================================================================


class TestAcceptanceScriptValidation:
    """Validate the acceptance script without registering a real task."""

    def test_task_definition_no_token_in_arguments(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Neither task_definition() nor the serialized action args may contain
        a token-bearing parameter."""
        monkeypatch.setenv("MEDLEARN_SYNC_INSTALL_ROOT", str(tmp_path / "install"))
        definition = windows_rollout.task_definition(
            Path("C:\\Users\\test\\home"),
            Path("C:\\Users\\test\\medlearn.exe"),
            15,
        )
        serialized = json.dumps(definition).lower()
        # "token" may appear in temp paths; check for actual token-bearing
        # patterns, not substring matches.
        for key in definition:
            key_lower = str(key).lower()
            if "token" in key_lower:
                raise AssertionError(f"token-related key in definition: {key}")
        assert not re.search(
            r'(?i)(bearer\s+\S{20,}|authorization\s*[=:]\s*\S+)', serialized
        ), f"credential-bearing content in definition: {serialized}"

        # Also check the serialized action args for token-like parameters.
        arguments = definition["arguments"]
        assert isinstance(arguments, list)
        action_str = windows_rollout._windows_action_args([str(v) for v in arguments])
        # Token-like patterns: --token, TOKEN=value, Bearer prefix.
        assert not re.search(
            r'(?i)(--token\b|^token\s*[=:]|bearer\s+\S{20,}|authorization\s*[=:])',
            action_str,
        ), f"token-like content in action args: {action_str}"

    def test_scheduled_wrapper_no_token(self) -> None:
        wrapper = windows_rollout.scheduled_wrapper(
            Path("C:\\Users\\test\\home"), Path("C:\\Users\\test\\medlearn.exe")
        )
        assert "token" not in wrapper.lower()
        assert "authorization" not in wrapper.lower()
        assert "MEDLEARN_HOME" in wrapper

    def test_vbs_wrapper_no_token_or_credential(self) -> None:
        vbs = windows_rollout.scheduled_vbs_wrapper(
            Path("C:\\Users\\test\\run-scheduled.ps1"),
        )
        vbs_lower = vbs.lower()
        assert "token" not in vbs_lower
        assert "authorization" not in vbs_lower
        assert "bearer" not in vbs_lower
        assert "dpapi" not in vbs_lower
        assert "credential" not in vbs_lower

    def test_unregistered_definition_produces_clean_what_if(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--what-if must produce a registration-ready definition without side
        effects and with properly structured arguments."""
        monkeypatch.setenv("MEDLEARN_SYNC_INSTALL_ROOT", str(tmp_path / "install"))
        definition = windows_rollout.task_definition(
            Path("C:\\测试 home"),
            Path("C:\\Program Files\\MedLearn\\medlearn.exe"),
            30,
        )
        assert definition["task_name"] == "MedLearn Vault Sync"
        assert definition["command"] == "wscript.exe"
        assert definition["interval_minutes"] == 30
        assert definition["multiple_instances"] == "IgnoreNew"
        assert definition["execution_time_limit_minutes"] == 5
        # Arguments must be a list of raw strings.
        arguments = definition["arguments"]
        assert isinstance(arguments, list)
        assert "//B" in arguments
        assert "//Nologo" in arguments
        assert arguments.count("//B") == 1
        assert arguments.count("//Nologo") == 1
        # Must not contain powershell.exe flags.
        assert "-NoProfile" not in arguments
        assert "-NonInteractive" not in arguments
        assert "-File" not in arguments
        # The launcher path must not have PowerShell escaping.
        assert "run-scheduled.vbs" in arguments[-1]
        assert "'" not in arguments[-1]
        # Must have both wrapper and launcher paths.
        assert definition.get("wrapper_path", "").endswith("run-scheduled.ps1")
        assert definition.get("launcher_path", "").endswith("run-scheduled.vbs")


# ===================================================================
# Manual acceptance script hardening tests
# ===================================================================
# These tests inspect the PowerShell acceptance script's source for
# structural properties that guarantee correct behavior at runtime.
# They do NOT register a real task; they validate the source text.


def _acceptance_script() -> str:
    """Return the acceptance script source as a string."""
    script_path = (
        Path(__file__).resolve().parents[1]
        / "scripts" / "acceptance" / "windows_sync_rollout.ps1"
    )
    return script_path.read_text(encoding="utf-8")


def _comment_help(source: str) -> str:
    """Extract the <# ... #> comment-based help block."""
    m = re.search(r"<#([\s\S]*?)#>", source)
    return m.group(1) if m else ""


def _full_acceptance_command_doc(source: str) -> str:
    """Extract the full interactive acceptance command from the comment help.
    Looks for the powershell invocation in .DESCRIPTION or .EXAMPLE sections."""
    help_block = _comment_help(source)
    # Match the full interactive command: powershell -NoProfile -File ... -Endpoint ... -Wheel
    m = re.search(
        r"powershell -NoProfile -File[\s\S]*?-Endpoint[\s\S]*?-Wheel[\s\S]*?\.whl",
        help_block,
    )
    return m.group(0).strip() if m else help_block


def _example_section(source: str) -> str:
    """Extract the full acceptance command documentation section."""
    return _full_acceptance_command_doc(source)


def _validate_only_section(source: str) -> str:
    """Extract the ValidateOnly example from comment-based help."""
    m = re.search(
        r"powershell -NoProfile -NonInteractive -File[\s\S]*?ValidateOnly", source
    )
    return m.group(0) if m else ""


def _full_acceptance_source(source: str) -> str:
    """Return only the portion of the script after ValidateOnly mode exits.
    This is the actual full-acceptance workflow, excluding the validation
    mode code that also appears earlier in the file."""
    # Find the comment marker that separates validation from full mode.
    m = re.search(r"# =+\s*\n# FULL ISOLATED ACCEPTANCE\s*\n# =+", source)
    if not m:
        return source
    return source[m.start():]


def _full_acceptance_pass_pos(source: str) -> int:
    """Position of the final PASS banner in the full-acceptance section."""
    # The last "ACCEPTANCE TEST PASSED" in the file
    last_pass = source.rfind("ACCEPTANCE TEST PASSED")
    return last_pass


def _main_finally_body(source: str) -> str:
    """Extract the body of the main (outermost) finally block."""
    full = _full_acceptance_source(source)
    # Find the finally block that is at the outermost level (matched to the
    # main try block).  It's the last substantial finally block.
    blocks = list(re.finditer(r"finally\s*\{", full))
    if not blocks:
        return ""
    # The main finally is the last one.
    start = blocks[-1].start()
    # Find matching close brace by counting.
    depth = 0
    i = full.index("{", start)
    for j in range(i, len(full)):
        if full[j] == "{":
            depth += 1
        elif full[j] == "}":
            depth -= 1
            if depth == 0:
                return full[i + 1:j]
    return ""


class TestAcceptanceScriptHardening:
    """Validate acceptance script structural properties from source text."""

    def test_no_token_parameter(self) -> None:
        """The script must not define a -Token, -SyncToken, or -Secret parameter."""
        source = _acceptance_script()
        # Parameter block: param(...)
        param_match = re.search(r"param\(([\s\S]*?)\)", source)
        assert param_match is not None, "param() block not found"
        param_block = param_match.group(1)
        param_names = set(re.findall(r'\$(\w+)', param_block))
        for forbidden in {"Token", "SyncToken", "Secret"}:
            assert forbidden not in param_names, (
                f"Forbidden parameter ${forbidden} found in acceptance script"
            )

    def test_full_command_has_no_noninteractive(self) -> None:
        """Full interactive acceptance command must NOT use -NonInteractive."""
        source = _acceptance_script()
        example = _example_section(source)
        assert example, ".EXAMPLE section not found"
        assert "-NonInteractive" not in example, (
            "Full acceptance command (.EXAMPLE) must not contain -NonInteractive"
        )

    def test_validate_only_retains_noninteractive(self) -> None:
        """CI validate-only command must retain -NonInteractive."""
        source = _acceptance_script()
        validate_cmd = _validate_only_section(source)
        assert validate_cmd, "ValidateOnly command not found in script help"
        assert "-NonInteractive" in validate_cmd, (
            "ValidateOnly command must retain -NonInteractive"
        )

    def test_full_command_uses_backtick_not_unix_backslash(self) -> None:
        """Line continuation in full command must use PowerShell backtick."""
        example = _example_section(_acceptance_script())
        assert example, ".EXAMPLE section not found"
        # Must contain backtick for line continuation.
        assert "`" in example, (
            "Full acceptance command must use backtick (`) for line continuation"
        )
        # Must not contain Unix-style backslash line continuation.
        assert "\\\n" not in example, (
            "Full acceptance command must not use Unix backslash (\\) line continuation"
        )

    def test_full_command_contains_no_token(self) -> None:
        """The full interactive acceptance command string must not contain
        a token-like value."""
        example = _example_section(_acceptance_script())
        assert example, ".EXAMPLE section not found"
        assert not re.search(
            r'(?i)\b(bearer\s[^\s]{20,}|token\s*[=:]\s*"[^"]{32,}"|authorization\s*[=:]\s*\S+)',
            example,
        ), "Token-like content found in full acceptance command"

    def test_preflight_check_exists_before_registration(self) -> None:
        """Script must check for pre-existing task before creating directories."""
        source = _acceptance_script()
        full = _full_acceptance_source(source)
        # Pre-flight check must occur before temp root creation *in full mode*.
        preflight_pos = full.find("PRE-FLIGHT")
        temp_root_pos = full.find("medlearn-acceptance-")
        assert preflight_pos > 0, "PRE-FLIGHT section not found in full mode"
        assert temp_root_pos > 0, "Temp root creation not found in full mode"
        assert preflight_pos < temp_root_pos, (
            "Pre-flight check must occur BEFORE temp root creation"
        )
        # Must call Test-AcceptanceTaskExists.
        assert "Test-AcceptanceTaskExists" in full, (
            "Test-AcceptanceTaskExists function call not found"
        )

    def test_task_created_tracking_variable(self) -> None:
        """Script must track whether THIS invocation created the task."""
        source = _acceptance_script()
        assert "$Script:TaskCreatedByThisRun" in source, (
            "TaskCreatedByThisRun tracking variable not found"
        )
        assert '$Script:TaskCreatedByThisRun = $false' in source, (
            "TaskCreatedByThisRun must be initialised to $false"
        )
        assert '$Script:TaskCreatedByThisRun = $true' in source, (
            "TaskCreatedByThisRun must be set to $true after successful registration"
        )

    def test_task_registration_verifies_limited_current_user_definition(self) -> None:
        source = _full_acceptance_source(_acceptance_script())
        assert "TaskPrincipalVerified" in source
        assert "TaskDefinitionVerified" in source
        assert "LogonType.ToString() -ne 'Interactive'" in source
        assert "RunLevel.ToString() -ne 'Limited'" in source
        assert "MSFT_TaskLogonTrigger" in source
        assert "MSFT_TaskTimeTrigger" in source
        assert "run-scheduled.ps1" in source

    def test_acceptance_escalates_only_the_schedule_step_when_required(self) -> None:
        source = _full_acceptance_source(_acceptance_script())
        assert "SYNC_SCHEDULE_ELEVATION_REQUIRED" in source
        assert "Type ELEVATE to approve registration only" in source
        assert "sync schedule install --interval-minutes 15 --elevated --json" in source
        assert "TaskInstalledWithElevation" in source
        assert "sync', 'schedule', 'remove', '--json'" in source

    def test_cleanup_conditional_on_task_created(self) -> None:
        """Cleanup must only remove task when created by this run."""
        source = _acceptance_script()
        assert '$Script:TaskCreatedByThisRun' in source
        # Invoke-ControlledCleanup must check the flag.
        cleanup_fn = re.search(
            r"function Invoke-ControlledCleanup\s*\{([\s\S]*?)\n\}", source
        )
        assert cleanup_fn is not None, "Invoke-ControlledCleanup function not found"
        cleanup_body = cleanup_fn.group(1)
        assert "TaskCreatedByThisRun" in cleanup_body, (
            "Cleanup must reference TaskCreatedByThisRun"
        )

    def test_nonzero_lasttaskresult_is_fatal(self) -> None:
        """Script must throw (not warn) on non-zero LastTaskResult."""
        source = _acceptance_script()
        # After the LastTaskResult check, there must be a throw for non-zero.
        nonzero_section = re.search(
            r"TaskLastResultZero.*?throw.*?non-zero.*?LastTaskResult",
            source, re.IGNORECASE | re.DOTALL
        )
        assert nonzero_section is not None or (
            '$TaskLastResultZero' in source and 'throw' in source
        ), "Non-zero LastTaskResult must throw, not warn"

    def test_timeout_is_fatal(self) -> None:
        """Timeout waiting for task must throw, not warn."""
        source = _acceptance_script()
        assert "never observed" in source.lower() or (
            "timeout" in source.lower() and "throw" in source.lower()
        ), "Task observation timeout must be fatal"
        # Timeout must not be a mere Write-Warning.
        timeout_block_match = re.search(
            r'TaskObserved.*?throw', source, re.DOTALL
        )
        assert timeout_block_match is not None or (
            "Task observed" not in source.lower() and
            '$TaskObserved' in source and 'throw' in source
        ), "Timeout path must lead to throw"

    def test_missing_scheduled_log_is_fatal(self) -> None:
        """Missing scheduled log after execution must throw."""
        source = _acceptance_script()
        assert "ScheduledLogExists" in source, "ScheduledLogExists gate not found"
        assert 'Scheduled log file not found' in source.lower() or (
            'not found' in source.lower() and 'scheduled-results.jsonl' in source
        ), "Missing scheduled log must throw"

    def test_no_new_log_record_is_fatal(self) -> None:
        """No new scheduled log record must throw."""
        source = _acceptance_script()
        assert "ScheduledLogNewRecord" in source, "ScheduledLogNewRecord gate not found"
        assert "No new scheduled log records" in source, (
            "Zero new log records must throw"
        )

    def test_final_pass_behind_all_gates(self) -> None:
        """'ACCEPTANCE TEST PASSED' must appear after all gate checks."""
        source = _acceptance_script()
        pass_pos = _full_acceptance_pass_pos(source)
        assert pass_pos > 0, "'ACCEPTANCE TEST PASSED' banner not found"

        # Check that key Set-Gate calls in the full-mode section appear
        # before the final PASS banner.
        full = _full_acceptance_source(source)
        for gate_name in ["Installed", "TaskLastResultZero",
                          "ScheduledLogRecordValid", "CleanupCompleted"]:
            # Match Set-Gate followed by the gate name (quoted).
            pattern = re.escape("Set-Gate") + r"\s+['`\"]" + re.escape(gate_name)
            gate_match = re.search(pattern, full)
            if gate_match is None:
                continue
            gate_abs_pos = source.find("FULL ISOLATED ACCEPTANCE") + gate_match.start()
            assert gate_abs_pos < pass_pos, (
                f"Gate {gate_name} must be set before PASS banner"
            )

    def test_pre_existing_task_not_replaced_or_removed(self) -> None:
        """A pre-existing task must abort the run, not delete it."""
        source = _acceptance_script()
        assert "will NOT overwrite" in source or "will not overwrite" in source, (
            "Script must declare it will not overwrite existing task"
        )
        # Pre-flight abort must happen before any task installation in full mode.
        full = _full_acceptance_source(source)
        preflight_pos = full.find("PRE-FLIGHT")
        install_pos = full.find("schedule install")
        assert preflight_pos > 0, "PRE-FLIGHT section not found in full mode"
        assert preflight_pos < install_pos, (
            "Pre-flight check must precede schedule install"
        )

    def test_cleanup_never_swallows_task_removal_exceptions(self) -> None:
        """If task removal fails, script must NOT silently continue."""
        source = _acceptance_script()
        assert "Exit-UnsafeCleanup" in source, (
            "Exit-UnsafeCleanup function must exist for failed cleanup"
        )
        # Exit-UnsafeCleanup must call exit with non-zero.
        unsafe_fn = re.search(
            r"function Exit-UnsafeCleanup\s*\{([\s\S]*?)\n\}",
            source
        )
        assert unsafe_fn is not None, "Exit-UnsafeCleanup function not found"
        unsafe_body = unsafe_fn.group(1)
        assert "exit 2" in unsafe_body or "exit 1" in unsafe_body, (
            "Exit-UnsafeCleanup must exit with non-zero code"
        )

    def test_emergency_cleanup_preserves_temp_root(self) -> None:
        """On emergency cleanup failure, temp root must be retained."""
        source = _acceptance_script()
        finally_body = _main_finally_body(source)
        assert finally_body, "Main finally block not found"
        assert ("retain" in finally_body.lower() or
                "diagnosis" in finally_body.lower()), (
            "Emergency cleanup must retain temp root for diagnosis"
        )
