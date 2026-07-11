# Cross-disciplinary architecture

## Aggregate boundaries

```text
SourceDocument <- SourceCitation <- MedicalClaim
                                      |
ConceptEntity <- ConceptRelation -----+
      ^
      +-- DisciplineLens
      +-- ChapterDossier.concept_ids
      +-- KnowledgeUnit.concept_ids
      +-- LearningCapture observations
```

The arrow means "references by opaque ID". Reverse arrows are indexes, not stored fields.

## Canonical concepts and lenses

A concept is not owned by a course. GERD has one `ConceptEntity`; internal medicine, surgery,
and pathology each have an independent `DisciplineLens` pointing to it. Chapters in those
disciplines reuse the same concept ID.

`scope_note` decides whether a same-name term is the same concept. Alias resolution never merges
records. Blank terms do not resolve; merged concepts redirect; deprecated concepts are skipped;
split-pending concepts require review.

## Relations and evidence

Relations do not own citations. A relation lists `supporting_claim_ids`; each claim owns typed
citations to source records. This keeps medical provenance in one place and allows graph edges to
be rebuilt from verified claims.

## Chapters

A chapter owns a forward concept scope. Knowledge units may reference only concepts in that
scope. Concept backlinks and cross-disciplinary views are derived by indexing chapters and
lenses.

## Learning

Learning captures preserve what happened at a time. Proposed corrections are not medical truth.
Learner state is computed from observations and can be rebuilt when mastery rules change.

## Persistence rule

P0.1.1 defines JSON contracts and examples only. Repository, index, preview, and adapter behavior
must depend on these aggregates without introducing a second persisted owner for the same fact.

## Display terminology

Terminology formatting is a presentation concern. Plans carry concept IDs; the renderer obtains
English abbreviations from concept aliases and Chinese explanations from canonical names. It does
not rewrite stored claims, citations, source titles, or learning-message text.
