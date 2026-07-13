# Windows Obsidian sync

Install with an isolated `pipx` or virtual-environment installation. Configure an existing
Obsidian Vault (it must already contain `.obsidian`):

```powershell
medlearn sync configure `
  --endpoint "https://medlearn-cloud.<subdomain>.workers.dev" `
  --vault "C:\Users\<user>\iCloudDrive\<ObsidianVault>"
medlearn sync login
medlearn sync pull
```

`login` hides token input. Windows stores the credential using Current User DPAPI in
`%LOCALAPPDATA%\MedLearn\sync\credential.bin`; it never enters the Obsidian Vault or iCloud.
`MEDLEARN_SYNC_TOKEN` is an optional non-interactive temporary override for CI.

Use `medlearn sync pull --dry-run` to inspect required downloads without changing files, and
`medlearn sync status` (or `--json`) for local configuration/state only.

The client writes only `MedLearn/`, never modifies `.obsidian`, never deletes local files, never
overwrites user changes, and does not upload, merge, or perform bidirectional sync. iCloud syncs
the local Vault only; it is not involved in API authentication.
