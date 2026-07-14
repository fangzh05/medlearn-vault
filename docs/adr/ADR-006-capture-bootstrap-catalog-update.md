# ADR-006: Capture bootstrap uses a separately reviewed catalog update

## Decision

The minimal safe solution is **B: a separate catalog-update workflow**.  It
does not extend `VaultPublicationPlan` artifacts.

`VaultPublicationPlan` remains the immutable two-artifact Capture contract:
canonical `LearningCapture` JSON followed by its Markdown rendering.  Adding
source or catalog artifacts there would make capture publication both mutate
the repository-owned catalog boundary and implicitly promote concepts.  That
would weaken the existing approval and exact-byte guarantees.

## Bootstrap lifecycle

1. A Handoff derives a deterministic `learning_chat` source candidate from its
   `CaptureContext`.  The candidate has `authority: 0`; it is provenance for
   learner observations, never an authoritative medical source.
2. A Proposal whose derived source or complete concept candidates are absent
   remains blocked with `CATALOG_UPDATE_REQUIRED`.  Candidate identifiers are
   never persistent concept identifiers, and learner evidence remains limited
   to one resolved, active `ConceptEntity` ID.
3. `medlearn capture catalog-update --bundle <bundle>` produces a deterministic
   review document and JSON repository-patch proposal bound to the exact Capture
   Proposal bytes, object digest, semantic digest, base bundle digest, and target
   bundle path. `medlearn catalog prepare-patch <proposal> --bundle <bundle>
   --output <directory>` writes proposed `sources.json`, `concepts.json`, a
   digest manifest, and review Markdown without touching the bundle.
4. A reviewer completes missing metadata, reviews the proposed source and
   concepts, then manually copies the prepared files and any separately completed
   concepts onto a review branch. This merge is the catalog-update approval and
   is the only persistence mechanism for bootstrap records; it does not access R2.
5. The semantically equivalent Handoff is run again against that updated
   repository bundle.  It now resolves only persistent source and concept IDs,
   produces a ready Capture Proposal, and follows the existing immutable
   approval/publication path.

The catalog-update proposal binds the source candidate and promotions to the
Capture Proposal ID and digest.  The subsequent Capture Approval binds the
fresh Proposal to the promoted source in the normal way.  There is no automatic
medical-claim verification, concept merge, source-authority invention, D1,
VPS, LLM API, or bidirectional synchronization.
