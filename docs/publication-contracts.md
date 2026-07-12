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

The later writer must freshly revalidate the Approval and plan digest, then commit the planned bytes
without recomputing or altering them.
