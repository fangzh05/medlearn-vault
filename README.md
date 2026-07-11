# MedLearn Vault

MedLearn Vault is a local-first contract layer for canonical medical concepts,
cross-disciplinary chapter dossiers, source-backed claims, and learner evidence.

This repository currently implements hardened contracts, validated bundles, and generic
bilingual medical previews:
permanent identifiers, matching fingerprints, versioned JSON Schema, a small CLI,
tests, and CI. It performs no Vault writes and contains no LLM, database, Obsidian,
or document-ingestion integration.

Version 0.5.1 accepts an untrusted, structured `CaptureDraft` (workflow contract 0.3.0),
reconciles it deterministically against a `ContractBundle`, and emits a reviewable
`CaptureProposal`. ChatGPT Work performs language understanding; MedLearn calls no LLM API.
Drafts contain only context, message IDs, short evidence excerpts, and extracted candidates—not
complete chat transcripts. Assertion ownership comes only from referenced message roles. Explicit
learning outcomes map to the persistent learner-evidence taxonomy, and complete proposals can be
materialized deterministically as validated `LearningCapture` records. Proposals never write the
knowledge base.

An isolated TypeScript Worker in `worker/` provides the first single-user cloud intake adapter.
It stores immutable intake bytes, jobs, and idempotency records only in the `medlearn-control` R2
bucket, then dispatches a fixed GitHub workflow target. It does not contain medical reasoning,
approval, Vault writing, or the workflow itself.

```powershell
cd worker
npm install
npm run lint
npm run typecheck
npm test
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
