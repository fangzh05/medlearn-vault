"""User-scoped Windows installation and Scheduled Task helpers for sync."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from medlearn_vault import __version__
from medlearn_vault.sync_client import SyncPaths, load_config, load_rollout, paths
from medlearn_vault.sync_models import SyncError
from medlearn_vault.windows_secrets import load_token

TASK_NAME = "MedLearn Vault Sync"
DEFAULT_INTERVAL_MINUTES = 15
LOG_RETENTION_COUNT = 50
_ACCESS_DENIED_EXIT = 5


@dataclass(frozen=True)
class InstallPlan:
    root: str
    venv: str
    executable: str
    wheel: str
    wheel_sha256: str


def install_root() -> Path:
    override = os.environ.get("MEDLEARN_SYNC_INSTALL_ROOT")
    if override:
        return Path(override)
    local_value = os.environ.get("LOCALAPPDATA")
    if not local_value:
        raise SyncError("SYNC_INSTALL_LOCATION_INVALID")
    local = Path(local_value)
    return local / "MedLearn" / "sync-client"


def executable(root: Path | None = None) -> Path:
    base = root or install_root()
    return base / "venv" / "Scripts" / "medlearn.exe"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _validate_install_destination(destination: Path) -> None:
    current = destination
    while True:
        if (current / ".git").exists() or (current / ".obsidian").is_dir():
            raise SyncError("SYNC_INSTALL_LOCATION_INVALID")
        if current.parent == current:
            break
        current = current.parent


def _restore_file(path: Path, original: bytes | None) -> None:
    if original is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(original)


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _executable_works(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        _run_installer_command([str(path), "--version"])
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def _run_installer_command(
    command: list[str], *, cwd: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run an installer child without leaking output into the CLI contract."""
    return subprocess.run(
        command,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def install_plan(wheel: Path, root: Path | None = None) -> InstallPlan:
    try:
        source = wheel.resolve(strict=True)
    except OSError as exc:
        raise SyncError("SYNC_INSTALL_ARTIFACT_INVALID") from exc
    if source.suffix != ".whl" or not source.is_file():
        raise SyncError("SYNC_INSTALL_ARTIFACT_INVALID")
    try:
        wheel_sha256 = _sha256(source)
        destination = (root or install_root()).resolve()
    except OSError as exc:
        raise SyncError("SYNC_INSTALL_ARTIFACT_INVALID") from exc
    _validate_install_destination(destination)
    return InstallPlan(
        root=str(destination),
        venv=str(destination / "venv"),
        executable=str(executable(destination)),
        wheel=str(source),
        wheel_sha256=wheel_sha256,
    )


def _atomic_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def install_windows(
    wheel: Path, *, root: Path | None = None, dry_run: bool = False
) -> dict[str, object]:
    """Install a trusted local wheel without downloading any package from the network."""

    plan = install_plan(wheel, root)
    if dry_run:
        return {"status": "planned", **asdict(plan), "network_download": False}
    if sys.platform != "win32":
        raise SyncError("SYNC_UNSUPPORTED_PLATFORM")
    destination = Path(plan.root)
    current = Path(plan.venv)
    metadata = destination / "install.json"
    try:
        installed = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        installed = {}
    if not isinstance(installed, dict):
        installed = {}
    if installed.get("wheel_sha256") == plan.wheel_sha256 and _executable_works(
        Path(plan.executable)
    ):
        return {"status": "reused", **asdict(plan), "network_download": False}

    destination.mkdir(parents=True, exist_ok=True)
    staging = destination / f".venv-staging-{uuid.uuid4().hex}"
    backup = destination / f".venv-backup-{uuid.uuid4().hex}"
    original_metadata = metadata.read_bytes() if metadata.is_file() else None
    swapped = False
    try:
        _run_installer_command([sys.executable, "-m", "venv", str(staging)])
        python = staging / "Scripts" / "python.exe"
        _run_installer_command(
            [
                str(python),
                "-m",
                "pip",
                "--isolated",
                "install",
                "--no-index",
                "--no-cache-dir",
                "--find-links",
                str(Path(plan.wheel).parent),
                Path(plan.wheel).name,
            ],
            cwd=str(Path(plan.wheel).parent),
        )
        staged_executable = staging / "Scripts" / "medlearn.exe"
        _run_installer_command([str(staged_executable), "--version"])
        if current.exists():
            os.replace(current, backup)
        try:
            os.replace(staging, current)
            swapped = True
        except OSError:
            if backup.exists() and not current.exists():
                os.replace(backup, current)
            raise
        _run_installer_command(
            [
                str(current / "Scripts" / "python.exe"),
                "-m",
                "pip",
                "--isolated",
                "install",
                "--no-index",
                "--no-cache-dir",
                "--no-deps",
                "--force-reinstall",
                "--find-links",
                str(Path(plan.wheel).parent),
                Path(plan.wheel).name,
            ],
            cwd=str(Path(plan.wheel).parent),
        )
        if not _executable_works(current / "Scripts" / "medlearn.exe"):
            raise subprocess.CalledProcessError(1, str(current / "Scripts" / "medlearn.exe"))
        _atomic_json(
            metadata,
            {
                "version": __version__,
                "wheel_sha256": plan.wheel_sha256,
                "wheel_name": Path(plan.wheel).name,
            },
        )
        shutil.rmtree(backup, ignore_errors=True)
    except (OSError, subprocess.SubprocessError) as exc:
        if swapped:
            shutil.rmtree(current, ignore_errors=True)
        if backup.exists() and not current.exists():
            try:
                os.replace(backup, current)
            except OSError:
                pass
        try:
            _restore_file(metadata, original_metadata)
        except OSError:
            pass
        raise SyncError("SYNC_INSTALL_FAILURE") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return {"status": "installed", **asdict(plan), "network_download": False}


# ---------------------------------------------------------------------------
# PowerShell source-code quoting
# ---------------------------------------------------------------------------
# Escapes a Python string for safe embedding inside a PowerShell single-quoted
# string literal.  Single quotes are doubled; the result is wrapped in ''.
# Use this ONLY when generating .ps1 source where the value must appear as a
# PowerShell string (e.g. $env:MEDLEARN_HOME = '...' or & '...').


def _powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


# ---------------------------------------------------------------------------
# Windows process command-line serialization
# ---------------------------------------------------------------------------
# Serializes a list of argv tokens into a single command-line string using the
# standard Windows quoting rules (double-quotes arguments containing spaces or
# tabs; backslash-escapes trailing backslashes before a double quote).
#
# This is the correct serializer for the argument list that will be consumed by
# the eventual powershell.exe child process.  It is intentionally *not*
# _powershell_quote — PowerShell single-quote rules are not Windows argv rules.


def _windows_action_args(arguments: list[str]) -> str:
    """Return the Windows command-line string for a powershell.exe argument list."""
    return subprocess.list2cmdline(arguments)


# ---------------------------------------------------------------------------
# VBScript string literal encoding
# ---------------------------------------------------------------------------
# Encodes a Python string as a safe VBScript double-quoted string literal.
# VBScript escapes a literal double-quote by doubling it ("").
# This function wraps the result in double quotes.
#
# Used ONLY when generating .vbs source where a command line must appear as a
# VBScript string (e.g. command = "<safe encoded command>").


def _vbs_string_literal(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


# ---------------------------------------------------------------------------
# Scheduled Task wrapper (.ps1)
# ---------------------------------------------------------------------------


def scheduled_wrapper(home: Path, client: Path) -> str:
    return (
        "$ErrorActionPreference = 'Stop'\n"
        f"$env:MEDLEARN_HOME = {_powershell_quote(str(home))}\n"
        f"& {_powershell_quote(str(client))} sync pull --scheduled --timeout 60\n"
        "exit $LASTEXITCODE\n"
    )


# ---------------------------------------------------------------------------
# Scheduled Task VBScript launcher (.vbs)
# ---------------------------------------------------------------------------


def scheduled_vbs_wrapper(ps1_path: Path) -> str:
    """Return the source code for run-scheduled.vbs.

    The VBS launcher invokes powershell.exe with window style 0 (hidden) and
    waits for it to complete, then propagates the real exit code.

    The PowerShell command is built via subprocess.list2cmdline (correct
    Windows argv serialization) and then safely encoded as a VBScript string
    literal.  No token, credential path, or environment variable content is
    embedded.
    """
    powershell_args = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(ps1_path),
    ]
    ps_command = subprocess.list2cmdline(powershell_args)
    vbs_command = _vbs_string_literal(ps_command)
    return (
        "Option Explicit\n"
        "\n"
        "Dim shell\n"
        "Dim exitCode\n"
        "Dim command\n"
        "\n"
        "Set shell = CreateObject(\"WScript.Shell\")\n"
        f"command = {vbs_command}\n"
        "exitCode = shell.Run(command, 0, True)\n"
        "WScript.Quit exitCode\n"
    )


# ---------------------------------------------------------------------------
# Task definition (pure data — no quoting applied to the arguments list)
# ---------------------------------------------------------------------------


def task_definition(home: Path, client: Path, interval_minutes: int) -> dict[str, object]:
    if not 5 <= interval_minutes <= 1440:
        raise SyncError("SYNC_INVALID_SCHEDULE")
    root = install_root()
    wrapper = root / "run-scheduled.ps1"
    launcher = root / "run-scheduled.vbs"
    return {
        "task_name": TASK_NAME,
        "command": "wscript.exe",
        "arguments": [
            "//B",
            "//Nologo",
            str(launcher),
        ],
        "client_executable": str(client),
        "medlearn_home": str(home),
        "wrapper_path": str(wrapper),
        "launcher_path": str(launcher),
        "interval_minutes": interval_minutes,
        "triggers": ["AtLogOn", f"Every {interval_minutes} minutes"],
        "multiple_instances": "IgnoreNew",
        "execution_time_limit_minutes": 5,
        "principal": "current_user/limited",
        "log": str(home / "scheduled-results.jsonl"),
    }


# ---------------------------------------------------------------------------
# Schedule prerequisites
# ---------------------------------------------------------------------------


def _schedule_ready(item: SyncPaths) -> None:
    config = load_config(item)
    rollout = load_rollout(config, item)
    if rollout is None or not rollout.dry_run_succeeded or not rollout.first_pull_completed:
        raise SyncError("SYNC_FIRST_PULL_REQUIRED")
    load_token(item.credential)


def _is_access_denied(exc: BaseException) -> bool:
    if getattr(exc, "winerror", None) == _ACCESS_DENIED_EXIT:
        return True
    if isinstance(exc, subprocess.CalledProcessError):
        return exc.returncode == _ACCESS_DENIED_EXIT
    return False


def _run_task_script(
    script: str, root: Path, *, elevated: bool
) -> subprocess.CompletedProcess[str]:
    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"]
    if not elevated:
        return subprocess.run(command + [script], capture_output=True, text=True, check=True)

    helper = root / f".medlearn-schedule-{uuid.uuid4().hex}.ps1"
    _atomic_text(helper, script)
    arguments = _windows_action_args(
        ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(helper)]
    )
    launcher = (
        "$ErrorActionPreference = 'Stop'; $process = Start-Process -FilePath 'powershell.exe'"
        f" -ArgumentList {_powershell_quote(arguments)} -Verb RunAs -Wait -PassThru;"
        " exit $process.ExitCode"
    )
    try:
        return subprocess.run(command + [launcher], capture_output=True, text=True, check=True)
    finally:
        helper.unlink(missing_ok=True)


def _registration_script(executable: str, action_args: str, interval_minutes: int) -> str:
    return (
        "$ErrorActionPreference = 'Stop'; $action = New-ScheduledTaskAction"
        f" -Execute {_powershell_quote(executable)}"
        " -Argument "
        + _powershell_quote(action_args)
        + "; $logon = New-ScheduledTaskTrigger -AtLogOn"
        + "; $start = (Get-Date).AddMinutes(1)"
        + "; $repeat = New-ScheduledTaskTrigger -Once -At $start"
        + f" -RepetitionInterval (New-TimeSpan -Minutes {interval_minutes})"
        + " -RepetitionDuration (New-TimeSpan -Days 3650)"
        + "; $settings = New-ScheduledTaskSettingsSet"
        + " -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5)"
        + "; $user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name"
        + "; try { Register-ScheduledTask -TaskName 'MedLearn Vault Sync' -Action $action"
        + " -Trigger @($logon,$repeat) -Settings $settings -User $user"
        + " -RunLevel Limited -Force | Out-Null; exit 0 } catch {"
        + " if ($_.FullyQualifiedErrorId -match '0x80070005') { exit 5 }; exit 1 }"
    )


# ---------------------------------------------------------------------------
# install_schedule — uses subprocess.list2cmdline for the action argument
# ---------------------------------------------------------------------------


def install_schedule(
    *,
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES,
    what_if: bool = False,
    elevated: bool = False,
    p: SyncPaths | None = None,
) -> dict[str, object]:
    item = p or paths()
    definition = task_definition(item.home, executable(), interval_minutes)
    if what_if:
        return {"status": "planned", **definition}
    if sys.platform != "win32":
        raise SyncError("SYNC_UNSUPPORTED_PLATFORM")
    _schedule_ready(item)
    client = Path(str(definition["client_executable"]))
    if not client.is_file():
        raise SyncError("SYNC_INSTALL_INCOMPLETE")
    wrapper = install_root() / "run-scheduled.ps1"
    launcher = install_root() / "run-scheduled.vbs"
    schedule_metadata = install_root() / "schedule.json"
    old_metadata = schedule_metadata.read_bytes() if schedule_metadata.is_file() else None
    old_wrapper = wrapper.read_bytes() if wrapper.is_file() else None
    old_launcher = launcher.read_bytes() if launcher.is_file() else None
    arguments = definition["arguments"]
    command = definition["command"]
    if not isinstance(arguments, list) or not isinstance(command, str):
        raise SyncError("SYNC_STATE_FAILURE")

    # 1.  Serialize for the Windows command line (wscript.exe argv).
    action_args = _windows_action_args([str(v) for v in arguments])

    script = _registration_script(str(command), action_args, interval_minutes)
    try:
        _atomic_json(schedule_metadata, definition)
        _atomic_text(wrapper, scheduled_wrapper(item.home, client))
        _atomic_text(launcher, scheduled_vbs_wrapper(wrapper))
        _run_task_script(script, wrapper.parent, elevated=elevated)
    except (OSError, subprocess.SubprocessError) as exc:
        try:
            _restore_file(schedule_metadata, old_metadata)
            _restore_file(wrapper, old_wrapper)
            _restore_file(launcher, old_launcher)
        except OSError:
            pass
        code = (
            "SYNC_SCHEDULE_ELEVATION_REQUIRED"
            if _is_access_denied(exc) and not elevated
            else "SYNC_SCHEDULE_FAILURE"
        )
        raise SyncError(code) from exc
    return {"status": "installed", **definition}


# ---------------------------------------------------------------------------
# schedule_status — locale-independent structured inspection via PowerShell
# ---------------------------------------------------------------------------


def _schedule_status_script() -> str:
    """PowerShell script that returns JSON describing the scheduled task.

    Returns a JSON object whether or not the task exists.  Never parses
    localized ``schtasks.exe`` table output.
    """
    return rf"""
$ErrorActionPreference = 'Stop'
$task = Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue
if (-not $task) {{
    $result = @{{
        task_name = '{TASK_NAME}'
        registered = $false
    }}
    $result | ConvertTo-Json -Compress
    exit 0
}}
$info = Get-ScheduledTaskInfo -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue
$action = $task.Actions | Select-Object -First 1
$result = @{{
    task_name = $task.TaskName
    registered = $true
    state = $task.State.ToString()
    last_run_time = $null
    next_run_time = $null
    last_task_result = 0
    executable = $action.Execute
    arguments = $action.Arguments
    principal_user_id = $task.Principal.UserId
    principal_logon_type = $task.Principal.LogonType.ToString()
    principal_run_level = $task.Principal.RunLevel.ToString()
    trigger_count = @($task.Triggers).Count
}}
if ($info) {{
    if ($info.LastRunTime -and $info.LastRunTime.Year -gt 2000) {{
        $result.last_run_time = $info.LastRunTime.ToString('o')
    }}
    if ($info.NextRunTime -and $info.NextRunTime.Year -lt 9999) {{
        $result.next_run_time = $info.NextRunTime.ToString('o')
    }}
    $result.last_task_result = $info.LastTaskResult
}}
$result | ConvertTo-Json -Compress
"""


def schedule_status() -> dict[str, object]:
    if sys.platform != "win32":
        raise SyncError("SYNC_UNSUPPORTED_PLATFORM")

    # Read local metadata for configured values that survive task absence.
    schedule_meta_path = install_root() / "schedule.json"
    try:
        meta_raw = json.loads(schedule_meta_path.read_text(encoding="utf-8"))
        meta: dict[str, object] = meta_raw if isinstance(meta_raw, dict) else {}
    except (OSError, ValueError):
        meta = {}

    try:
        ps_cmd = [
            "powershell.exe", "-NoProfile", "-NonInteractive",
            "-Command", _schedule_status_script(),
        ]
        proc = subprocess.run(
            ps_cmd, capture_output=True, text=True, check=True, timeout=15,
        )
        result: dict[str, object] = json.loads(proc.stdout)
    except subprocess.CalledProcessError as exc:
        raise SyncError("SYNC_SCHEDULE_FAILURE") from exc
    except json.JSONDecodeError as exc:
        raise SyncError("SYNC_SCHEDULE_FAILURE") from exc

    if not isinstance(result, dict):
        raise SyncError("SYNC_SCHEDULE_FAILURE")

    # Merge locally configured values where the task may not carry them.
    if result.get("registered") and meta:
        for key in (
            "wrapper_path",
            "launcher_path",
            "interval_minutes",
            "medlearn_home",
            "client_executable",
        ):
            value = meta.get(key)
            if value is not None and key not in result:
                result[key] = value

    return result


# ---------------------------------------------------------------------------
# remove_schedule — explicit idempotent removal with verification
# ---------------------------------------------------------------------------


def _remove_schedule_script() -> str:
    """PowerShell script: check existence → unregister → verify absence.

    Returns a single token on stdout: ABSENT | REMOVED | VERIFY_FAILED.
    """
    return rf"""
$ErrorActionPreference = 'Stop'
$task = Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue
if (-not $task) {{
    Write-Output 'ABSENT'
    exit 0
}}
try {{
    Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false
}} catch {{
    if ($_.FullyQualifiedErrorId -match '0x80070005') {{ exit 5 }}
    Write-Output 'VERIFY_FAILED'
    exit 2
}}
$verify = Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue
if ($verify) {{
    Write-Output 'VERIFY_FAILED'
    exit 2
}}
Write-Output 'REMOVED'
exit 0
"""


def remove_schedule(*, elevated: bool = False) -> dict[str, object]:
    if sys.platform != "win32":
        raise SyncError("SYNC_UNSUPPORTED_PLATFORM")

    schedule_metadata_path = install_root() / "schedule.json"
    wrapper_path = install_root() / "run-scheduled.ps1"
    launcher_path = install_root() / "run-scheduled.vbs"

    if elevated:
        try:
            was_registered = bool(schedule_status().get("registered"))
            _run_task_script(_remove_schedule_script(), wrapper_path.parent, elevated=True)
            if schedule_status().get("registered"):
                raise SyncError("SYNC_SCHEDULE_FAILURE")
        except SyncError:
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            raise SyncError("SYNC_SCHEDULE_FAILURE") from exc
        status = "REMOVED" if was_registered else "ABSENT"
    else:
        try:
            proc = _run_task_script(_remove_schedule_script(), wrapper_path.parent, elevated=False)
            status = proc.stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            code = (
                "SYNC_SCHEDULE_ELEVATION_REQUIRED"
                if _is_access_denied(exc)
                else "SYNC_SCHEDULE_FAILURE"
            )
            raise SyncError(code) from exc

    if status == "ABSENT":
        # Task was never registered or already removed — clean up stale local
        # files but do not treat this as an error.
        schedule_metadata_path.unlink(missing_ok=True)
        wrapper_path.unlink(missing_ok=True)
        launcher_path.unlink(missing_ok=True)
        return {"status": "already_absent", "task_name": TASK_NAME}

    if status == "VERIFY_FAILED":
        # Do NOT delete local metadata — the task may still be present and we
        # cannot confirm removal.
        raise SyncError("SYNC_SCHEDULE_FAILURE")

    if status == "REMOVED":
        # Only now is it safe to delete local schedule state.
        schedule_metadata_path.unlink(missing_ok=True)
        wrapper_path.unlink(missing_ok=True)
        launcher_path.unlink(missing_ok=True)
        return {"status": "removed", "task_name": TASK_NAME}

    # Unknown output — treat as failure and preserve local state.
    raise SyncError("SYNC_SCHEDULE_FAILURE")
