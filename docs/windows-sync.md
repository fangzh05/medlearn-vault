# Windows Obsidian sync

MedLearn sync is a Windows 10/11 one-way remote-to-local client. It writes only files declared by
the authenticated Worker manifest under `MedLearn/` in one existing Obsidian Vault. It never manages
the whole Vault, uploads, deletes, bidirectionally syncs, force-overwrites, or changes `.obsidian`,
attachments, plugins, themes, workspace state, iCloud metadata, or unrelated notes.

Back up the real Vault before first production use. Use a temporary Vault while learning this flow.
iCloud is external to MedLearn: a successful pull means the local iCloud-backed Vault was written,
not that iCloud has already delivered the file to an iPhone or iPad.

## User-scoped locations

The stable client is `%LOCALAPPDATA%\MedLearn\sync-client\venv\Scripts\medlearn.exe`; normal
installation needs no Administrator privileges. Configuration, DPAPI ciphertext, first-pull state,
lock, and scheduled results live outside the Vault in `%LOCALAPPDATA%\MedLearn\sync` (or the advanced
`MEDLEARN_HOME` override). Unicode and space-containing paths are supported.

Configuration requires an existing directory with `.obsidian`, normalizes a HTTPS endpoint, and
rejects malformed URLs, repository roots, the installation directory, state directories inside the
Vault, and unsafe reparse points. `medlearn sync login` prompts without echoing and stores credentials
only with Current User Windows DPAPI. It reports `status=authenticated credential=windows_dpapi`.
Tokens are never CLI arguments or configuration, task, log, environment-file, stdout, or stderr data.
If the token is lost, an authorized server operator must rotate the server secret, then login again.

## Install and upgrade

The installer accepts only a local wheel from a prepared wheelhouse and performs no network download.
Build the trusted, pinned release bundle, then install or upgrade:

```powershell
New-Item -ItemType Directory -Force dist\wheelhouse | Out-Null
python -m pip wheel --require-hashes -r requirements\ci.txt --wheel-dir dist\wheelhouse
python -m pip wheel --no-build-isolation --no-deps --wheel-dir dist\wheelhouse .
$wheel = Get-ChildItem dist\wheelhouse\medlearn_vault-0.13.0-*.whl | Select-Object -First 1
medlearn sync install-windows --wheel $wheel.FullName --json
```

Identical reruns reuse the client. Upgrades build and smoke-test a staging virtual environment before
replacing the stable one, so a failed upgrade leaves the prior usable installation intact. Use
`--dry-run --json` to inspect without changing the machine. To uninstall, remove the optional task,
then remove `%LOCALAPPDATA%\MedLearn\sync-client`; no Vault artifacts are deleted.

## First production pull

Replace placeholders only after the real Vault is backed up. These commands never change the process
working directory:

```powershell
$client = "$env:LOCALAPPDATA\MedLearn\sync-client\venv\Scripts\medlearn.exe"
& $client sync configure --endpoint "https://medlearn-cloud.<subdomain>.workers.dev" --vault "C:\Users\<user>\iCloudDrive\<ObsidianVault>"
& $client sync login
& $client sync pull --dry-run --json
& $client sync pull --confirm-first-pull --json
```

Dry-run prints exact remote, planned download, and conflict counts without creating `MedLearn/` or
writing a Vault file. A first real pull requires a successful dry-run and `--confirm-first-pull`.
Conflicts preserve local files and exit `3`; network, configuration, and credential failures exit `1`.
After a successful first pull, use `& $client sync pull --json` for ordinary manual pulls.

## Optional Scheduled Task

`MedLearn Vault Sync` runs at user logon and every 15 minutes by default. It invokes the stable
client with the configured `MEDLEARN_HOME`, limits runs to five minutes, ignores overlapping task
starts, and also relies on the per-state-directory client lock. Registration is blocked until
configuration, DPAPI authentication, a successful dry-run, and a successful first real pull exist.

```powershell
& $client sync schedule install --what-if --json
& $client sync schedule install --interval-minutes 15 --json
& $client sync schedule status --json
& $client sync schedule remove --json
```

Removing the task never deletes Vault content. The latest 50 UTF-8-without-BOM JSON lines are retained
in `%LOCALAPPDATA%\MedLearn\sync\scheduled-results.jsonl`; records contain only timestamp, client
version, status, manifest status, counts, sanitized conflict paths, or stable error codes. They never
contain tokens, Authorization headers, DPAPI ciphertext, R2 credentials, environment dumps, artifacts,
or server response bodies. No Rclone, Remotely Save, D1, VPS, Obsidian plugin, or LLM API is added.

## Structured task status

`medlearn sync schedule status --json` reports locale-independent structured data using PowerShell
ScheduledTasks cmdlets and JSON serialization. It never parses localized `schtasks.exe` table output.

When the task is absent:

```json
{"task_name": "MedLearn Vault Sync", "registered": false}
```

When the task is present:

| Field | Description |
|---|---|
| `task_name` | `"MedLearn Vault Sync"` |
| `registered` | `true` |
| `state` | `Ready`, `Running`, `Disabled`, or `Queued` |
| `last_run_time` | ISO 8601, or `null` if never run |
| `next_run_time` | ISO 8601, or `null` if none scheduled |
| `last_task_result` | HRESULT of the last execution (`0` = success) |
| `executable` | `"powershell.exe"` |
| `arguments` | The Windows command-line argument string |
| `wrapper_path` | Configured path to `run-scheduled.ps1` (from local metadata) |
| `interval_minutes` | Configured repetition interval (from local metadata) |

Status never returns secrets, environment dumps, token data, or DPAPI content.
An inspection failure (PowerShell error, malformed JSON) produces the stable
`SYNC_SCHEDULE_FAILURE` error rather than silently pretending the task is absent.

A registered task (`registered: true`) and a successfully executed task
(`last_task_result: 0`) are distinct states. A task can be `Ready` but have
never run (`last_run_time: null`), or have a non-zero `last_task_result`
indicating a prior execution failure.

## Safe task removal

Task removal is explicit and idempotent:

1. Check whether the exact task exists (locale-independent PowerShell).
2. If absent, return `{"status": "already_absent", "task_name": "MedLearn Vault Sync"}`.
3. If present, unregister the exact task.
4. Query again and verify that it is absent.
5. Only then delete local schedule metadata (`schedule.json`) and the wrapper script.
6. On any failure (unregister error, verification still finds the task, access denied,
   Task Scheduler service failure), preserve local metadata and raise
   `SYNC_SCHEDULE_FAILURE`.

Removal never reports `"status": "removed"` when Task Scheduler still contains the
task. It never deletes Vault content, Obsidian configuration, or user files.

## Command-line quoting

The scheduled task action arguments are serialized using the standard Windows
`subprocess.list2cmdline` convention (double-quotes around space-containing
arguments; backslash-escaping for trailing backslashes before a double quote).
This is distinct from PowerShell single-quote source-string escaping used inside
the generated `.ps1` wrapper.

Paths containing spaces, Chinese characters, apostrophes, parentheses, and
ampersands are correctly preserved through the serialization round-trip.
`task_definition()` returns a raw argument list; `install_schedule()` serializes
it with `subprocess.list2cmdline()` before embedding in the PowerShell
registration command.

## Manual acceptance gate

CI validates task definition generation, argument serialization, dry-run
registration, and structured status/removal logic, but does **not** prove actual
Task Scheduler execution, because normal CI runners cannot register real Windows
Scheduled Tasks.

A Windows manual acceptance gate is required before merge. Run the isolated
acceptance script from a real Windows 10/11 machine:

```powershell
# CI-safe validation (no task registration, no Worker contact):
powershell -NoProfile -NonInteractive -File scripts/acceptance/windows_sync_rollout.ps1 -ValidateOnly

# Full isolated acceptance (interactive — prompts for sync token):
powershell -NoProfile -NonInteractive -File scripts/acceptance/windows_sync_rollout.ps1 `
    -Endpoint "https://medlearn-cloud.<subdomain>.workers.dev" `
    -Wheel "dist/wheelhouse/medlearn_vault-0.13.0-py3-none-any.whl"
```

The acceptance script:
- Creates an isolated temporary root whose path contains spaces and Chinese characters.
- Uses a temporary `MEDLEARN_HOME` and a throwaway Vault containing only `.obsidian`.
- Never accepts the sync token as a command-line parameter.
- Never writes the token to disk, logs, or PowerShell command history.
- Exercises the full lifecycle: dry-run install, real install, configure, login,
  dry-run pull, explicit confirmation, first real pull, schedule install,
  structured status, Start-ScheduledTask, bounded wait, LastTaskResult check,
  scheduled log verification, token scan (task args, wrapper, configs, rollout,
  state, logs, credential DPAPI check), schedule remove, absence verification,
  Vault integrity check, and cleanup.
- Supports `-KeepArtifacts` to preserve the temporary root for diagnostics.
- The command shown above does **not** contain a token.

## Inspecting task execution results

Programmatic inspection:

```powershell
$status = & $client sync schedule status --json | ConvertFrom-Json
$status.last_task_result  # 0 = success; non-zero HRESULT = failure
$status.last_run_time     # ISO 8601 when the task last completed
$status.state             # Ready, Running, Disabled, or Queued
```

Manual inspection via the Task Scheduler UI or PowerShell:

```powershell
Get-ScheduledTaskInfo -TaskName 'MedLearn Vault Sync' | Format-List LastRunTime, LastTaskResult
```

A successful scheduled execution has `last_task_result: 0` and a recent
`last_run_time`. A non-zero `last_task_result` is an HRESULT; common values
include `0x80070005` (access denied) and `0x800704DD` (not logged on).

Scheduled results are also written to the sanitized JSONL log at
`%LOCALAPPDATA%\MedLearn\sync\scheduled-results.jsonl`.

## iCloud limitations

iCloud delivery to another device (iPhone, iPad, another PC) remains outside
MedLearn's guarantees. A successful pull writes the local iCloud-backed Vault;
MedLearn does not control when or whether iCloud propagates those files.
