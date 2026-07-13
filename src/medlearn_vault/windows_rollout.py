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
        subprocess.run(
            [str(path), "--version"], check=True, capture_output=True, text=True
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


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
        subprocess.run([sys.executable, "-m", "venv", str(staging)], check=True)
        python = staging / "Scripts" / "python.exe"
        subprocess.run(
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
            check=True,
        )
        staged_executable = staging / "Scripts" / "medlearn.exe"
        subprocess.run([str(staged_executable), "--version"], check=True)
        if current.exists():
            os.replace(current, backup)
        try:
            os.replace(staging, current)
            swapped = True
        except OSError:
            if backup.exists() and not current.exists():
                os.replace(backup, current)
            raise
        subprocess.run(
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
            check=True,
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


def _powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def scheduled_wrapper(home: Path, client: Path) -> str:
    return (
        "$ErrorActionPreference = 'Stop'\n"
        f"$env:MEDLEARN_HOME = {_powershell_quote(str(home))}\n"
        f"& {_powershell_quote(str(client))} sync pull --scheduled --timeout 60\n"
        "exit $LASTEXITCODE\n"
    )


def task_definition(home: Path, client: Path, interval_minutes: int) -> dict[str, object]:
    if not 5 <= interval_minutes <= 1440:
        raise SyncError("SYNC_INVALID_SCHEDULE")
    wrapper = install_root() / "run-scheduled.ps1"
    return {
        "task_name": TASK_NAME,
        "command": "powershell.exe",
        "arguments": [
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            _powershell_quote(str(wrapper)),
        ],
        "client_executable": str(client),
        "medlearn_home": str(home),
        "triggers": ["AtLogOn", f"Every {interval_minutes} minutes"],
        "multiple_instances": "IgnoreNew",
        "execution_time_limit_minutes": 5,
        "principal": "current_user/limited",
        "log": str(home / "scheduled-results.jsonl"),
    }


def _schedule_ready(item: SyncPaths) -> None:
    config = load_config(item)
    rollout = load_rollout(config, item)
    if rollout is None or not rollout.dry_run_succeeded or not rollout.first_pull_completed:
        raise SyncError("SYNC_FIRST_PULL_REQUIRED")
    load_token(item.credential)


def install_schedule(
    *,
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES,
    what_if: bool = False,
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
    schedule_metadata = install_root() / "schedule.json"
    old_metadata = schedule_metadata.read_bytes() if schedule_metadata.is_file() else None
    old_wrapper = wrapper.read_bytes() if wrapper.is_file() else None
    arguments = definition["arguments"]
    if not isinstance(arguments, list):
        raise SyncError("SYNC_STATE_FAILURE")
    script = (
        "$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "
        + _powershell_quote(" ".join(str(value) for value in arguments))
        + "; $logon = New-ScheduledTaskTrigger -AtLogOn"
        + "; $start = (Get-Date).AddMinutes(1)"
        + "; $repeat = New-ScheduledTaskTrigger -Once -At $start"
        + f" -RepetitionInterval (New-TimeSpan -Minutes {interval_minutes})"
        + " -RepetitionDuration (New-TimeSpan -Days 3650)"
        + "; $settings = New-ScheduledTaskSettingsSet"
        + " -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5)"
        + "; $principal = New-ScheduledTaskPrincipal -UserId "
        + "([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)"
        + " -LogonType Interactive -RunLevel Limited"
        + "; Register-ScheduledTask -TaskName 'MedLearn Vault Sync' -Action $action"
        + " -Trigger @($logon,$repeat) -Settings $settings -Principal $principal -Force | Out-Null"
    )
    try:
        _atomic_json(schedule_metadata, definition)
        _atomic_text(wrapper, scheduled_wrapper(item.home, client))
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], check=True
        )
    except (OSError, subprocess.SubprocessError) as exc:
        try:
            _restore_file(schedule_metadata, old_metadata)
            _restore_file(wrapper, old_wrapper)
        except OSError:
            pass
        raise SyncError("SYNC_SCHEDULE_FAILURE") from exc
    return {"status": "installed", **definition}


def schedule_status() -> dict[str, object]:
    if sys.platform != "win32":
        raise SyncError("SYNC_UNSUPPORTED_PLATFORM")
    result = subprocess.run(
        ["schtasks.exe", "/Query", "/TN", TASK_NAME], capture_output=True, text=True, check=False
    )
    return {"task_name": TASK_NAME, "registered": result.returncode == 0}


def remove_schedule() -> dict[str, object]:
    if sys.platform != "win32":
        raise SyncError("SYNC_UNSUPPORTED_PLATFORM")
    result = subprocess.run(
        ["schtasks.exe", "/Delete", "/TN", TASK_NAME, "/F"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in {0, 1}:
        raise SyncError("SYNC_SCHEDULE_FAILURE")
    install_root().joinpath("schedule.json").unlink(missing_ok=True)
    install_root().joinpath("run-scheduled.ps1").unlink(missing_ok=True)
    return {"status": "removed", "task_name": TASK_NAME}
