# MedLearn Vault

MedLearn Vault is a local-first contract layer for canonical medical concepts,
cross-disciplinary chapter dossiers, source-backed claims, and learner evidence.

This repository currently implements **P0/P0.1 only**: hardened domain models,
permanent identifiers, matching fingerprints, versioned JSON Schema, a small CLI,
tests, and CI. It performs no Vault writes and contains no LLM, database, Obsidian,
or document-ingestion integration.

## Contract architecture

- `ConceptEntity` is one permanent medical identity with aliases, external coding-system
  identifiers, relations, and multiple discipline lenses.
- `MedicalClaim` is source-governed medical evidence. An unverified chat claim cannot be
  marked supported, and verified claims require citations and sufficient authority.
- `ChapterDossier` references canonical concepts and uses typed cross-discipline links.
- `LearningCapture` records learner evidence separately from medical truth.
- `concept_id(identity_key)` uses an immutable creation key. `concept_fingerprint(...)`
  uses mutable names and aliases only for duplicate-candidate matching.

```powershell
python -m pip install -e ".[dev]"
medlearn doctor
medlearn doctor --vault C:\path\to\MedLearnVault
medlearn schema export
medlearn schema check
medlearn concept validate concept.json
pytest
```

Committed schemas live in `schemas/current/`. CI regenerates each schema in memory and
fails if a model changes without an intentional snapshot and migration-note update.

## Development boundary

P0.1 intentionally contains no registry persistence, contextual resolver, typed textbook
knowledge-unit union, source ingestion, Obsidian adapter, LLM integration, or PDF pipeline.
Those capabilities belong to later phases after these contracts stabilize.
