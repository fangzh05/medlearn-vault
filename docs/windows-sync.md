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
