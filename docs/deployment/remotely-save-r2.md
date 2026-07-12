# Remotely Save with Cloudflare R2

## Production ownership

Remotely Save is the only synchronization engine for the production MedLearn Vault. Do not store
the same production Vault in iCloud, OneDrive, Dropbox, Syncthing, or Obsidian Sync. Back up the
existing Vault before migration.

## Bucket separation

`medlearn-control` stores workflow records only. `medlearn-vault` stores approved Vault files only.
Use a separate R2 API token for `medlearn-vault`, scoped to that bucket with Object Read & Write
only. Never reuse the control-plane token.

## Prefix separation

Use one disposable plaintext prefix for the initial test, for example `tests/plaintext/`, and one
encrypted production prefix, for example `production/encrypted/`. Never mix plaintext and encrypted
objects under either prefix. Do not promote the test Vault into production.

## Remotely Save configuration

Create the configuration only in Obsidian on each device. Only
`**/.obsidian/plugins/remotely-save/data.json` is ignored; arbitrary `data.json` files remain
trackable. Do not commit that Remotely Save configuration, access keys, or an encryption password.
Store the password outside this repository. Every device
must use identical encryption and crypt settings, bucket, endpoint, and prefix.

## Initial cross-device test

1. Create a separate non-iCloud test Vault.
2. Add `sync-test.md` and upload it from desktop.
3. Download it on mobile.
4. Edit it on mobile, then download the edit on desktop.
5. Make competing edits to exercise conflict behavior.
6. Test an offline edit and a retry after reconnecting.

Record the outcome in a private operational log, not in this repository.
