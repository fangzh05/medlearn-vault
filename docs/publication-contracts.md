# Vault publication-plan contract

`VaultPublicationPlan` 0.1.0 is a control-plane contract, not a persistent medical aggregate and
not final authorization to write a Vault. It is stored create-only at
`v1/publication-plans/<publication_plan_id>.json` in `medlearn-control`; `medlearn-vault` is
untouched and no synchronization capability exists.

After fresh Approval, Proposal, Job, Execution, Review, and bundle verification, the plan fixes
exactly two Vault-relative byte sequences:

- `MedLearn/Data/Captures/<capture_id>.json`
- `MedLearn/Captures/<YYYY>/<MM>/<capture_id>.md`

Both JSON documents are key-sorted compact UTF-8, no BOM, with exactly one LF. `capture_id` is
`capture_` plus the first 32 SHA-256 hex characters of exact canonical LearningCapture JSON.
`publication_plan_id` binds plan version, Approval ID/object digest, Proposal ID/object digest,
base bundle digest, and Review digest; artifact digests are deliberately excluded.

An identical rerun reads and validates the exact canonical winner without rewriting it. Malformed
or different bytes at the same key return `PUBLICATION_PLAN_CONFLICT`. No generated time, host,
workflow-run, source-job, environment, or filesystem values appear in the plan.

The later writer must freshly revalidate the Approval and plan digest, then write the exact planned
bytes without regenerating, re-rendering, or re-materializing artifact content.

## Vault writer contract

`VaultPublicationWriter` (0.10.0) reads a verified `VaultPublicationPlan` from `medlearn-control`,
re-attests provenance through `ApprovalAttestor`, then writes the exact planned artifact bytes to
`medlearn-vault` R2 using create-only semantics. It never regenerates, re-renders, or
re-materializes artifact content; it only verifies identities and digests before writing the exact
planned bytes.

### Validation order

1. Input format validation (plan ID, digest, job ID patterns).
2. Read plan from `v1/publication-plans/<publication_plan_id>.json` in `medlearn-control`.
3. Verify raw stored bytes digest matches expected digest.
4. Parse as `VaultPublicationPlan`; verify canonical form matches stored bytes; verify plan ID
   matches key. The plan's own model-validator checks artifacts, capture_id, paths, digests,
   byte_lengths, identity, and ordering.
5. Recompute plan object digest from the parsed model; cross-check against expected digest.
6. Fresh `ApprovalAttestor` run using plan fields; cross-check all provenance fields against plan.
7. Write artifacts in fixed plan order (JSON first, Markdown second).

### Write semantics

- `key` = `artifact.path`, `body` = `artifact.content_utf8.encode("utf-8")`,
  `ContentType` = `artifact.media_type`.
- `If-None-Match: *` on create → 409/412 = existing key.
- Existing key with identical body AND identical ContentType → `reused`.
- Existing key with different body or different ContentType → `VAULT_ARTIFACT_CONFLICT`, no overwrite.
- Safe recovery: already-written artifacts are reused on rerun; unwritten artifacts are created.

### Stable error codes

- `INVALID_VAULT_PUBLICATION_INPUT`
- `PUBLICATION_PLAN_NOT_FOUND`
- `INVALID_PUBLICATION_PLAN`
- `PUBLICATION_PLAN_OBJECT_DIGEST_MISMATCH`
- `PUBLICATION_PLAN_PROVENANCE_MISMATCH`
- `VAULT_ARTIFACT_CONFLICT`
- `VAULT_STORE_FAILURE`
- `CONTROL_STORE_FAILURE`

### Store boundary

- `control_store: ReadOnlyObjectStore` — reads only `medlearn-control`.
- `vault_store: VaultObjectStore` — only `get` and `create` on `medlearn-vault`.
- The two stores are distinct interfaces; no single object can write to both buckets.

## Production baseline (2026-07-12)

First `medlearn-plan-publication` run from main (squash merge of PR #17):

- **Workflow run**: `29202779036`
- **publication_plan_id**: `publication_plan_f292a00d10d8ea0fc750577cf1823fe3`
- **publication_plan_object_digest**: `sha256:964db58e3792844695c22f4c45b0bd04eb4a5695e0923e1f68d5d688a8413071`
- **capture_id**: `capture_a0a71c75e894b2c358e9bb62b242b6ee`
- **capture_object_digest**: `sha256:a0a71c75e894b2c358e9bb62b242b6eef279f75b03b6c82b993a0dacd30e446e`
- **markdown_digest**: `sha256:e458a4bd6e260e1ad3b53227e9b341ef1cb27cb219054d724133d4f8a95d75ba`
- **reused**: `false`
