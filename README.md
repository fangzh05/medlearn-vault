# MedLearn Vault

MedLearn Vault is a local-first contract layer for canonical medical concepts,
cross-disciplinary chapter dossiers, source-backed claims, and learner evidence.

This repository currently implements hardened contracts, validated bundles, and generic
bilingual medical previews:
permanent identifiers, matching fingerprints, versioned JSON Schema, a small CLI,
tests, and CI. It performs no Vault writes and contains no LLM, database, Obsidian,
or document-ingestion integration.

Version 0.10.0 accepts an untrusted, structured `CaptureDraft` (workflow contract 0.3.0),
reconciles it deterministically against a `ContractBundle`, and emits a reviewable
`CaptureProposal`. ChatGPT Work performs language understanding; MedLearn calls no LLM API.
Drafts contain only context, message IDs, short evidence excerpts, and extracted candidates—not
complete chat transcripts. Assertion ownership comes only from referenced message roles. Explicit
learning outcomes map to the persistent learner-evidence taxonomy, and complete proposals can be
materialized deterministically as validated `LearningCapture` records. Proposals never write the
knowledge base.

An isolated TypeScript Worker in `worker/` provides the first single-user cloud intake adapter.
`IntakeEnvelope` 0.1.0 exact bytes are stored in `medlearn-control`; a recoverable JobRecord 0.2.0
handoff uses conditional R2 writes and a dispatch lease before calling the fixed GitHub workflow
target. Python's `capture extract-intake` verifies the transport digest and emits deterministic
CaptureDraft JSON plus its distinct canonical digest. The adapter contains no medical reasoning,
approval or Vault writing.

The first idempotent `medlearn-propose.yml` workflow reads only the fixed `medlearn-control`
bucket, verifies exact intake bytes, loads a repository-controlled bundle, and create-only writes a
deterministic proposal and review. A leased `ProposalExecutionRecord` makes at-least-once dispatch
resumable without claiming exactly-once execution. It does not approve or commit a LearningCapture.

The approval boundary adds immutable `ProposalApprovalRecord` 0.1.0 objects. Approval verifies the
exact stored Proposal bytes, proposal identity and internal digest, ready status, and expected base
bundle digest before a create-only write under a fixed control key. One exact Proposal subject has
one immutable decision slot: the first create-only decision wins and an opposite decision returns
`APPROVAL_CONFLICT`. `proposal_object_digest` names the exact stored bytes; the Proposal's own
`proposal_digest` remains its separate internal semantic digest. Rejections require a sanitized
`rejection_code`; `decided_at` is the only approval timestamp, and unverified `source_job_id` is not
stored. It does not load or mutate the
bundle and still performs no LearningCapture, Vault, Obsidian, artifact, or commit write.

`medlearn-approve.yml` is the bounded, manually dispatched control-plane approval runner. It
requires an explicit decision and exact proposal-ID confirmation before exposing its fixed
control-plane credentials. This release adds no Vault access or credential.

`medlearn-verify-approval.yml` attests an existing Approval, Proposal, Job, Execution, and Review
through fixed `medlearn-control` keys. It is read-only: it writes no attestation object, does not
authorize publication, does not replace future commit-time revalidation, and does not access the
Vault, Obsidian, or Remotely Save. Its source job ID is an operational assertion verified against
the stored Job and Execution, not a field added to `ProposalApprovalRecord`.

Version 0.9.0 adds `VaultPublicationPlan` 0.1.0: a deterministic, create-only control-plane plan
containing exact `LearningCapture` JSON and Markdown bytes. It writes only `medlearn-control`;
`medlearn-vault` remains untouched. See `docs/publication-contracts.md`.

Version 0.10.0 adds the immutable medlearn-vault writer: `VaultPublicationWriter` reads a verified
`VaultPublicationPlan` from `medlearn-control`, re-attests its provenance, then writes the exact
planned artifact bytes to `medlearn-vault` R2 using create-only semantics. See
`docs/publication-contracts.md` and `docs/e2e-publication-plan-baseline.md`.

`medlearn-publish-vault.yml` runs only from `main` and scopes separate `CONTROL_R2_*` and
`VAULT_R2_*` credentials so no single step holds both control-plane and vault write access.

Version 0.11.0 adds the immutable `VaultPublicationReceipt` and authenticated read-only Worker
API. After artifact publication, the writer create-only writes a deterministic receipt at
`v1/publications/<publication_plan_id>.json` in `medlearn-vault`. The Worker exposes two new
read-only Vault endpoints: `GET /v1/vault/manifest` (deterministic listing of all published
artifacts from immutable receipts) and `GET /v1/vault/files?path=...` (download with digest,
byte-length, and media-type integrity verification). Both support `If-None-Match`/`ETag` with
SHA-256 ETags. Vault auth uses a separate `MEDLEARN_SYNC_TOKEN`; ingest and vault credentials
are fully isolated. This release does NOT implement a Windows/Obsidian sync client or any
write/delete/modify capabilities on the Vault API. See `docs/publication-contracts.md` and
`docs/migrations/0.11.0-vault-read-api.md`.

`medlearn-synthetic-intake.yml` submits a fixed, excerpt-free synthetic fixture through the real
Worker intake path, waits for Proposal completion, and reports only sanitized Proposal provenance
through the read-only inspector. It accepts no dispatch inputs and runs only from `main`.

```powershell
cd worker
npm install
npm run lint
npm run typecheck
npm test
npm run contracts:check
```

## Contract architecture

- `ConceptEntity` is one permanent medical identity with aliases, semantic scope, and external
  coding-system identifiers. Relations and discipline lenses are independent records.
- `MedicalClaim` is source-governed medical evidence. An unverified chat claim cannot be
  marked supported, and source-backed claims require citations. Evidence quality is derived
  from the cited source records rather than copied onto every claim.
- `ChapterDossier` owns only forward concept references; backlinks are derived.
- `LearningCapture` records immutable observations. `LearnerState` is a rebuildable projection.
- IDs are opaque and permanent. Computed fingerprints use mutable content only for matching.
- `SourceDocument` owns authority and version; citations carry typed page, slide, section,
  chat-message, figure, or table locators.

```powershell
python -m pip install -e ".[dev]"
medlearn doctor
medlearn schema export
medlearn schema check
medlearn concept validate concept.json
medlearn bundle validate examples/gerd
medlearn preview render examples/gerd preview.md --topic GERD
medlearn preview render examples/copd preview.md --topic COPD
medlearn capture validate-draft examples/capture/copd-session/draft.json
medlearn capture propose examples/copd examples/capture/copd-session/draft.json proposal.json
medlearn capture review examples/copd proposal.json proposal.md
pytest
```

Persistent schemas live in `schemas/current/`; workflow schemas live in
`schemas/workflow/current/`. CI regenerates each schema in memory and
fails if a model changes without an intentional snapshot and migration-note update.

Bundle warnings are printed but return success; integrity errors return nonzero. Preview topics
that are missing, ambiguous, deprecated, or pending split review also return nonzero.

## Development boundary

P0.1 intentionally contains no registry persistence, contextual resolver, typed textbook
knowledge-unit union, source ingestion, Obsidian adapter, LLM integration, or PDF pipeline.
Those capabilities belong to later phases after these contracts stabilize.
