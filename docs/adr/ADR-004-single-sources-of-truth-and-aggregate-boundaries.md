# ADR-004: Single sources of truth and aggregate boundaries

Status: Accepted

## Decision

Each fact has one authoritative persisted owner:

- `ConceptEntity` owns identity, terminology, semantic scope, external identifiers, and
  lifecycle only.
- `ConceptRelation` is an independent edge record. It is not embedded in either concept.
- `ChapterDossier` owns its forward list of referenced concept IDs. Reverse chapter,
  knowledge-unit, and exam-point references are derived indexes and are never persisted in
  `ConceptEntity` or `DisciplineLens`.
- `MedicalClaim` owns a medical assertion and citations. Source authority and version belong
  to `SourceDocument` and are reached through citation source IDs.
- `LearningCapture` owns immutable observations. `LearnerState` is a rebuildable projection;
  lifecycle state is never written back into capture history.

## Consequences

Backlinks, evidence quality, learner status, and graph views must be computed from their
authoritative records. This trades some indexing work for deterministic rebuilds and avoids
manual synchronization of bidirectional links.
