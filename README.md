# MedLearn Vault

MedLearn Vault is a local-first contract layer for canonical medical concepts,
cross-disciplinary chapter dossiers, source-backed claims, and learner evidence.

This repository currently implements **P0 through PR 2B**: hardened domain models,
permanent identifiers, matching fingerprints, versioned JSON Schema, a small CLI,
tests, and CI. It performs no Vault writes and contains no LLM, database, Obsidian,
or document-ingestion integration.

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
medlearn preview render examples/gerd preview.md
pytest
```

Committed schemas live in `schemas/current/`. CI regenerates each schema in memory and
fails if a model changes without an intentional snapshot and migration-note update.

## Development boundary

P0.1 intentionally contains no registry persistence, contextual resolver, typed textbook
knowledge-unit union, source ingestion, Obsidian adapter, LLM integration, or PDF pipeline.
Those capabilities belong to later phases after these contracts stabilize.
