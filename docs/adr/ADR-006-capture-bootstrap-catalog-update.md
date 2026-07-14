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
5. The catalog patch PR is reviewed and merged to main.  This updates the
   repository-controlled contract bundle so that the previously-missing source
   and concepts now resolve.
6. **Explicit reproposal**: After the catalog merge, the blocked Job is **not**
   resubmitted.  Instead, an explicit, bounded reproposal workflow runs:
   `.github/workflows/medlearn-repropose.yml`.  It reuses the exact immutable
   Intake bytes but creates a new immutable Job/Execution/Proposal/Review
   against the current (post-merge) bundle.
   - Required inputs: `source_job_id`, `blocked_proposal_id`,
     `catalog_update_id`, `previous_base_bundle_digest`, `confirmation`.
   - The workflow verifies all existing control-plane records, confirms the
     prior Proposal contains `CATALOG_UPDATE_REQUIRED`, confirms the current
     bundle digest differs from the blocked one, rebuilds from the same Intake
     bytes, and create-only writes the new records.
   - It is idempotent for the same `catalog_update_id` and current bundle
     digest — repeated invocations return the same new Job.
   - The blocked Job, Proposal, Intake, and idempotency records remain
     immutable and untouched.
7. The new Job's Proposal, if `ready_for_review`, follows the existing
   approval and publication path (`medlearn-approve.yml` →
   `medlearn-plan-publication.yml` → `medlearn-publish-vault.yml`).

## Converter namespace versioning

The Handoff-to-Intake converter uses an explicit, immutable conversion version:

```
HANDOFF_CONVERSION_VERSION = "medlearn.handoff_to_intake.v4"
```

The MCP idempotency key includes this version:

```
medlearn-handoff-v4-${handoffDigest}
```

This ensures that converter changes create a separate idempotency namespace.
Old `v1` (`medlearn-handoff-${digest}`), `v2`
(`medlearn-handoff-v2-${digest}`), and `v3`
(`medlearn-handoff-v3-${digest}`) records remain untouched.  The same Handoff
under the new converter creates one new Intake/Job; repeated submissions under
the same converter remain idempotent.

The v4 converter keeps the stable v2 learning-chat source identity and
additionally splits multi-term learner evidence into separate single-term
candidates before proposal production. This preserves the invariant that every
persisted learner-evidence record points to exactly one persistent concept. It
does not create concepts, select ambiguous concepts, or promote chat claims into
authoritative medical facts.

## Reproposal identity algorithm

A reproposal Job ID is derived deterministically from:

```
reproposal_ + sha256(
  "0.2.0" +
  source_job_id +
  blocked_proposal_id +
  catalog_update_id +
  receipt_digest +
  current_base_bundle_digest +
  intake_digest
)[:32]
```

The new Job record carries provenance fields:

- `reproposal_of_job_id` — the original blocked Job
- `reproposal_of_proposal_id` — the blocked Proposal
- `catalog_update_id` — the merged catalog update identity

These fields are immutable, optional, and preserved through the control-plane
read path.

## Control-plane record changes

- `JobRecord` gains optional `reproposal_of_job_id`, `reproposal_of_proposal_id`,
  and `catalog_update_id` fields.  Existing Jobs are unaffected.
- A new `ReproposalOrchestrator` (`medlearn_vault.workflow`) implements the
  bounded reproposal logic.  It reads existing control-plane records, verifies
  the catalog merge, rebuilds against the current bundle, and create-only writes
  new records.
- The new `medlearn workflow repropose` CLI command and
  `medlearn-repropose.yml` workflow expose this to CI.

## Lifecycle summary

```
Handoff submission (v4 converter)
  → new Job + Intake (versioned idempotency namespace)
  → bootstrap Proposal (blocked: CATALOG_UPDATE_REQUIRED)
  → catalog patch PR (manual review and merge)
  → explicit reproposal (medlearn-repropose.yml)
  → new immutable Job + ready_for_review Proposal
  → approval → publication plan → vault publish
```

The catalog-update proposal binds the source candidate and promotions to the
Capture Proposal ID and digest.  The subsequent Capture Approval binds the
fresh Proposal to the promoted source in the normal way.  There is no automatic
medical-claim verification, concept merge, source-authority invention, D1,
VPS, LLM API, or bidirectional synchronization.
