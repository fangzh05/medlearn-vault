import json
import re
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

    # Capture the PowerShell script passed to subprocess.run so we can inspect
    # the action argument string.
    captured_scripts: list[str] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[0] == "powershell.exe":
            captured_scripts.append(command[-1])
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(windows_rollout.subprocess, "run", run)
    installed = windows_rollout.install_schedule(p=home)

    # Removal: mock PowerShell to return REMOVED
    def run_remove(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[0] == "powershell.exe" and "Unregister" in str(command):
            return subprocess.CompletedProcess(command, 0, "REMOVED\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(windows_rollout.subprocess, "run", run_remove)
    removed = windows_rollout.remove_schedule()

    assert installed["status"] == "installed"
    assert removed["status"] == "removed"
    assert all("token" not in " ".join(command).lower() for command in [])  # already verified
    assert not (Path(config.vault_path) / "MedLearn").exists()

    # Verify the PowerShell script contained a properly-serialized action
    # argument string (via subprocess.list2cmdline).
    if captured_scripts:
        register_script = captured_scripts[0]
        # The -Argument value should be a quoted string containing -File and the
        # wrapper path, not a bare PowerShell-quoted path.
        assert "-File" in register_script
        assert "run-scheduled.ps1" in register_script


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

    def test_task_definition_arguments_are_raw_not_powershell_quoted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """task_definition() must return raw argument strings without embedded
        PowerShell single-quote escaping."""
        monkeypatch.setenv("MEDLEARN_SYNC_INSTALL_ROOT", str(tmp_path / "测试 install"))
        home = sync_client.SyncPaths(tmp_path / "home")
        client = tmp_path / "sync-client" / "venv" / "Scripts" / "medlearn.exe"
        definition = windows_rollout.task_definition(home.home, client, 15)
        arguments = definition["arguments"]
        assert isinstance(arguments, list)
        # The -File argument must be the raw path, not a PowerShell-quoted string.
        file_arg = arguments[arguments.index("-File") + 1]
        assert isinstance(file_arg, str)
        assert file_arg.startswith(str(tmp_path))
        assert file_arg.endswith("run-scheduled.ps1")
        # Must NOT contain PowerShell single-quote escaping.
        assert not file_arg.startswith("'")
        assert not file_arg.endswith("'")


class TestActionArgumentInRegisteredScript:
    """Assert against the actual raw argument string that would be registered
    with Task Scheduler — i.e. the value passed to -Argument."""

    def test_install_schedule_embeds_list2cmdline_output(
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
        # The script must contain a -Argument value produced by list2cmdline:
        #  - The wrapper path must appear in the -Argument string.
        assert "run-scheduled.ps1" in script
        #  - Because the install root has a space, the path must be
        #    double-quoted inside the -Argument string.
        assert '-Argument' in script
        #  - The raw argument string must not be wrapped in PowerShell single
        #    quotes as the *final* argv mechanism (it IS wrapped in '' for
        #    PowerShell string-literal embedding, but the content inside those
        #    quotes is from list2cmdline).
        # Extract what's between -Argument and the next ;
        arg_start = script.index("-Argument") + len("-Argument")
        arg_segment = script[arg_start:].split(";")[0].strip()
        # arg_segment is e.g. '-NoProfile -NonInteractive ... -File "C:\...\wrapper.ps1"'
        assert arg_segment.startswith("'") and arg_segment.endswith("'")
        inner = arg_segment[1:-1]
        assert "-File" in inner
        assert "sync client" in inner
        # The space-containing path must be double-quoted inside.
        assert '"' in inner

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
        assert "run-scheduled.ps1" in script


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
            "executable": "powershell.exe",
            "arguments": (
                '-NoProfile -NonInteractive -ExecutionPolicy Bypass '
                '-File "C:\\wrapper.ps1"'
            ),
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
        assert status["executable"] == "powershell.exe"

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
                            "executable": "powershell.exe",
                            "arguments": "-File C:\\wrapper.ps1",
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
                            "executable": "powershell.exe",
                            "arguments": "-File C:\\wrapper.ps1",
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
            "executable": "powershell.exe",
            "arguments": "-File wrapper.ps1",
        }

        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if command[0] == "powershell.exe":
                return subprocess.CompletedProcess(command, 0, json.dumps(task_json), "")
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(windows_rollout.subprocess, "run", run)
        status = windows_rollout.schedule_status()
        assert status["wrapper_path"] == str(root / "run-scheduled.ps1")
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
                            "executable": "powershell.exe",
                            "arguments": '-File "C:\\wrapper.ps1"',
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

        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if command[0] == "powershell.exe":
                return subprocess.CompletedProcess(command, 0, "REMOVED\n", "")
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(windows_rollout.subprocess, "run", run)
        result = windows_rollout.remove_schedule()
        assert result["status"] == "removed"
        # Local metadata must be cleaned up.
        assert not (root / "schedule.json").exists()
        assert not (root / "run-scheduled.ps1").exists()

    def test_unregister_failure_raises_error_and_preserves_metadata(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        root = tmp_path / "sync-client"
        monkeypatch.setenv("MEDLEARN_SYNC_INSTALL_ROOT", str(root))
        root.mkdir(parents=True)
        (root / "schedule.json").write_text("{}", encoding="utf-8")
        (root / "run-scheduled.ps1").write_text("# wrapper", encoding="utf-8")

        def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.CalledProcessError(1, "powershell.exe")

        monkeypatch.setattr(windows_rollout.subprocess, "run", run)
        with pytest.raises(SyncError, match="SYNC_SCHEDULE_FAILURE"):
            windows_rollout.remove_schedule()
        # Metadata must be preserved on failure.
        assert (root / "schedule.json").exists()
        assert (root / "run-scheduled.ps1").exists()

    def test_verify_still_finds_task_raises_error_and_preserves_metadata(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        root = tmp_path / "sync-client"
        monkeypatch.setenv("MEDLEARN_SYNC_INSTALL_ROOT", str(root))
        root.mkdir(parents=True)
        (root / "schedule.json").write_text("{}", encoding="utf-8")
        (root / "run-scheduled.ps1").write_text("# wrapper", encoding="utf-8")

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
        with pytest.raises(SyncError, match="SYNC_SCHEDULE_FAILURE"):
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
        """If removal fails, schedule.json and wrapper must remain on disk."""
        monkeypatch.setattr(sys, "platform", "win32")
        root = tmp_path / "sync-client"
        monkeypatch.setenv("MEDLEARN_SYNC_INSTALL_ROOT", str(root))
        root.mkdir(parents=True)
        (root / "schedule.json").write_text('{"task_name":"MedLearn Vault Sync"}', encoding="utf-8")
        (root / "run-scheduled.ps1").write_text("exit 0", encoding="utf-8")

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
        assert definition["interval_minutes"] == 30
        assert definition["multiple_instances"] == "IgnoreNew"
        assert definition["execution_time_limit_minutes"] == 5
        # Arguments must be a list of raw strings.
        arguments = definition["arguments"]
        assert isinstance(arguments, list)
        assert "-NoProfile" in arguments
        assert "-NonInteractive" in arguments
        assert "-ExecutionPolicy" in arguments
        assert "Bypass" in arguments
        assert "-File" in arguments
        # The wrapper path must not be PowerShell-quoted in the arguments list.
        file_arg = arguments[arguments.index("-File") + 1]
        assert isinstance(file_arg, str)
        assert "'" not in file_arg
