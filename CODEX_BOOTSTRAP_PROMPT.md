# Codex Bootstrap Prompt — MedLearn Vault v2.1

Read these files first:

- `PROJECT_SPEC.md`
- `docs/CROSS_DISCIPLINARY_ARCHITECTURE.md`
- `schemas/concept_entity.schema.json`
- `schemas/chapter_dossier.schema.json`
- `schemas/learning_capture.schema.json`
- `examples/gerd_cross_discipline.json`

This development round implements only **P0: contracts and scaffold**.

## Required deliverables

1. Initialize Python 3.12+ project using Pydantic v2 and Typer.
2. Implement domain models:
   - ConceptEntity
   - ConceptAlias
   - ConceptRelation
   - DisciplineLens
   - SourceCitation
   - MedicalClaim
   - KnowledgeUnit
   - ChapterDossier
   - LearnerEvidence
   - Misconception
   - LearningCapture
3. Implement deterministic identifiers:
   - concept ID;
   - claim ID;
   - relation ID;
   - knowledge unit ID.
4. Implement validation:
   - timezone-aware datetime;
   - Vault-relative paths only;
   - concept aliases normalized but original display form retained;
   - relation source and target cannot be empty;
   - a DisciplineLens must reference a valid concept ID;
   - learner evidence cannot modify the truth value of a MedicalClaim.
5. Implement CLI:
   - `medlearn --version`
   - `medlearn doctor`
   - `medlearn schema export`
   - `medlearn concept validate <json>`
6. Create tests covering:
   - GERD English abbreviation and Chinese full name resolve to one concept;
   - one concept has multiple discipline lenses;
   - internal medicine and surgery chapters share the same concept ID;
   - ambiguous terms produce candidates instead of auto-merging;
   - deterministic IDs are stable;
   - Chinese serialization round-trip;
   - absolute paths and `..` are rejected;
   - all datetimes require timezone;
   - learning evidence remains separate from medical claims.
7. Create GitHub Actions for lint, type check and tests.
8. Create ADR:
   - `ADR-001-local-first.md`
   - `ADR-002-canonical-concepts-multiple-discipline-lenses.md`
   - `ADR-003-medical-authority-vs-course-relevance.md`
9. Do not implement LLM calls, Obsidian REST, databases, PDF parsing or real Vault writes.
10. Do not introduce LangChain, LlamaIndex, Neo4j or a vector database.

## Completion report

Return:

- final file tree;
- key design decisions;
- schema deviations;
- commands run and results;
- next PR proposal;
- unresolved risks.

Do not implement later phases in this PR.
