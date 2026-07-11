# MedLearn Vault product specification

Contract version: 1.1.1

## Purpose

MedLearn Vault compiles source-backed medical knowledge and immutable learning observations
into cross-disciplinary chapter previews. It is local-first and preview-first. The current
phase defines contracts only: no LLM, database, PDF ingestion, Obsidian adapter, or real Vault
write is implemented.

## Single sources of truth

- `ConceptEntity`: permanent identity, terminology, semantic scope, lifecycle, external IDs.
- `ConceptRelation`: independent semantic edge supported by MedicalClaim IDs.
- `DisciplineLens`: one discipline's semantic view of one canonical concept.
- `SourceDocument`: source type, authority, publication date, version, and optional local path.
- `MedicalClaim`: medical assertion, typed citations, evidence state, and lifecycle.
- `ChapterDossier`: one chapter's forward concept scope and knowledge units.
- `LearningCapture`: frozen historical observations.
- `LearnerState`: rebuildable projection derived from captures.

Backlinks, source quality, and learner status are derived. They are not duplicated into the
records that they point back to.

## Identity and change detection

Persisted entities use opaque `<kind>_<32 hex>` IDs. Renaming content never changes an ID.
`match_fingerprint` finds likely duplicates from normalized identifying terms.
`content_hash` covers mutable semantic content and changes when that content changes.

## Medical evidence policy

`unverified_chat` permits only `unassessed`. `source_backed` permits supported, refuted, or
conflicting evidence and requires a citation. `verified_reference` permits supported or
refuted evidence and requires a citation. `conflicted` requires conflicting evidence.
Lifecycle is independent: active, deprecated, or superseded.

Source authority belongs to `SourceDocument`. A future repository will resolve citation IDs
and derive evidence quality; current contracts do not claim to perform that policy evaluation.

## Concept semantics

Every concept requires a `scope_note`. Definition, inclusion terms, exclusion terms, broader
concepts, aliases, and external identifiers refine its boundary. Same-name concepts may remain
separate when their scopes differ.

## Learning event policy

Captures and nested observations are append-only. A misconception observation records the
observed error and a non-authoritative proposed correction. Medical correctness is referenced
through `correction_claim_ids`. Current status lives only in rebuildable `LearnerState`.

## Chapter consistency

`ChapterDossier.concept_ids` is the chapter scope. Every knowledge unit's concept IDs must be a
subset of that scope. Reverse concept-to-chapter links are derived.

## CLI and quality gates

The P0 CLI provides version, doctor, schema export/check, and concept validation. CI runs Ruff,
strict mypy, pytest, and committed Schema drift checks using the pinned Pydantic constraint.

## Next vertical slice

P0.1.1 is followed by a preview-only GERD flow: fixture JSON -> exact alias resolution -> claim
candidates -> learner observations -> chapter preview -> Markdown in a temporary directory.
It must not write to a real Vault or call an LLM.
