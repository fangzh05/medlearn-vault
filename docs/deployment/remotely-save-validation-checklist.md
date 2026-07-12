# Remotely Save R2 validation checklist

All listed items are MANUAL GATES. Do not enter credentials, encryption passwords, or Obsidian
configuration into this repository or GitHub Actions.

## Cloudflare

- [ ] Back up the existing production Vault outside the synchronization path.
- [ ] Create or verify the separate `medlearn-vault` R2 bucket.
- [ ] Keep `medlearn-control` limited to workflow records; do not place Vault files there.
- [ ] Create a distinct `medlearn-vault` API token restricted to Object Read & Write for that bucket.
- [ ] Choose a disposable plaintext test prefix and a separate encrypted production prefix.
- [ ] Confirm no plaintext object exists under the encrypted production prefix, or vice versa.
- [ ] Store the token and encryption password only in approved private storage.

## GitHub

- [ ] Add only `CONTROL_R2_ENDPOINT`, `CONTROL_R2_ACCESS_KEY_ID`, and
      `CONTROL_R2_SECRET_ACCESS_KEY` as repository secrets for the approval workflow.
- [ ] Confirm those credentials cannot access `medlearn-vault`.
- [ ] Review and merge the approval-workflow PR after CI passes; do not add Vault credentials.
- [ ] Dispatch a non-production approval only after control-plane secrets and a valid Proposal exist.

## Windows Obsidian

- [ ] Install and enable Remotely Save in the separate non-iCloud test Vault.
- [ ] Configure the `medlearn-vault` test prefix without saving `data.json` to Git.
- [ ] Create `sync-test.md`, upload it, and record the result privately.
- [ ] Download the mobile edit, run a conflict test, then run an offline/retry test.
- [ ] Create the production Vault only after the test passes; use the encrypted production prefix.
- [ ] Create the required top-level folders: `Captures`, `Data`, `Reviews`, and `System`.

## iPhone Obsidian

- [ ] Install and enable Remotely Save for the separate test Vault.
- [ ] Configure the identical test bucket, prefix, encryption, and crypt settings.
- [ ] Download `sync-test.md`, edit it, upload it, and verify desktop receives the edit.
- [ ] Participate in the conflict and offline/retry tests.
- [ ] Configure production only after the test passes, with identical production crypt settings.

## iPad Obsidian

- [ ] Install and enable Remotely Save for the separate test Vault.
- [ ] Configure the identical test bucket, prefix, encryption, and crypt settings.
- [ ] Download `sync-test.md` and confirm its content is current.
- [ ] Participate in a conflict or offline/retry test.
- [ ] Configure production only after the test passes, with identical production crypt settings.
