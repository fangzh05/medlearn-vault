# MedLearn Vault product specification

Contract version: 1.2.0

Workflow contract version: 0.3.0

## Purpose

MedLearn Vault compiles source-backed medical knowledge and immutable learning observations
into cross-disciplinary chapter previews. It is local-first and preview-first. The current
phase defines contracts plus one replaceable single-user transport adapter: no LLM, database, PDF
ingestion, Obsidian adapter, or real Vault write is implemented.

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

Canonical entities (`concept`, `claim`, `source`, `relation`, `lens`, `unit`) use opaque
`<kind>_<32 hex>` IDs. Scoped external records (`chapter`, `course`, `discipline`, `session`,
`message`) use stable readable namespaced strings. Renaming content never changes an ID.
`match_fingerprint` only finds likely duplicate entities from normalized identifying terms.
No record hash is persisted before repository and incremental-sync semantics exist.

## Medical evidence policy

`unverified_chat` permits only `unassessed`. `source_backed` permits supported, refuted, or
conflicting evidence and requires a citation. `verified_reference` permits supported or
refuted evidence and requires a citation. Conflict workflow belongs to `review_status`.
Lifecycle is independent: active, deprecated, or superseded; superseded claims identify their
replacement claims.

Source authority belongs to `SourceDocument`. A future repository will resolve citation IDs
and derive evidence quality; current contracts do not claim to perform that policy evaluation.

## Concept semantics

Every concept requires a `scope_note`. Definition, inclusion terms, exclusion terms, broader
concepts, aliases, and external identifiers refine its boundary. Same-name concepts may remain
separate when their scopes differ.

## Learning event policy

All contract records are frozen and replaced as whole validated records. Captures and nested
observations are append-only. A misconception observation records the
observed error and a non-authoritative proposed correction. Medical correctness is referenced
through `correction_claim_ids`. Current status lives only in rebuildable `LearnerState`.

## Chapter consistency

`ChapterDossier.concept_ids` is the chapter scope and `anchor_concept_ids` identifies the
chapter's organizing concepts. Every knowledge unit's concept IDs and every anchor must be a
subset of the scope. Reverse concept-to-chapter links are derived.

## CLI and quality gates

The P0 CLI provides version, doctor, schema export/check, and concept validation. CI runs Ruff,
strict mypy, pytest, and committed Schema drift checks using the pinned Pydantic constraint.

## Next vertical slice

`ContractBundle` is the preview boundary: fixture JSON -> cross-record validation -> exact alias
resolution -> claim selection -> learner observations -> `PreviewPlan` -> deterministic Markdown.
It does not write to a real Vault or call an LLM.

## Reviewable capture proposals

ChatGPT Work's built-in model performs natural-language understanding and emits an untrusted
`CaptureDraft`; it does not export the full conversation. MedLearn performs exact parsing,
alias matching, validation, deduplication, and proposal generation without OpenAI or another
LLM API. A `CaptureProposal` is bound to the draft and accepted `ContractBundle` state with
SHA-256 digests. User statements remain learner observations, never `MedicalClaim` records.
This phase generates proposals and deterministic review Markdown only; it cannot approve,
commit, or write Vault data. Assertion ownership is derived exclusively from referenced evidence
message roles. Explicit learner outcomes use the persistent `LearnerEvidence` taxonomy; correctness
is never inferred from agreement with an assistant. A complete, current, untampered and unblocked
proposal can be deterministically materialized as a validated `LearningCapture` without I/O.

## Terminology and generic previews

`PreviewRequest.topic` accepts a ConceptId, canonical name, preferred English term, or registered
alias. Missing, ambiguous, deprecated, and split-pending topics fail explicitly. Preview plans
store concept IDs and source text; the Markdown renderer alone formats user-visible labels.

English abbreviations come only from English `ConceptAlias` records whose type is
`abbreviation`; their Chinese explanation comes only from `ConceptEntity.canonical_name`.
Abbreviations expand once per document using ASCII token boundaries and only for concepts linked
to the displayed claim. Citations, source titles, and raw learning observations are never edited.
