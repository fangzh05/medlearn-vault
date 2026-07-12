# Optional cloud-assisted capture architecture

The MedLearn core is local-first. Its required boundary is:

```text
CaptureDraft → deterministic local reconciliation → reviewable CaptureProposal
→ validated LearningCapture
```

The first replaceable, single-user intake adapter is:

```text
IntakeEnvelope → Cloudflare Worker → medlearn-control R2
→ fixed GitHub workflow dispatch → idempotent proposal producer
```

ChatGPT Work's built-in model owns language understanding and structured extraction. Its
`CaptureDraft` is untrusted and intentionally carries only session context, referenced-message
metadata, short review evidence, and extracted candidates—not a complete transcript.

MedLearn calls no LLM API. It deterministically validates the draft, uses the exact alias
resolver, reconciles it with the accepted `ContractBundle`, and emits a versioned
`CaptureProposal`. `error` and `review` issues block; warnings remain visible but permit
`ready_for_review`. User assertions and errors become learner observations, never medical fact.

The Worker exposes only `GET /`, `GET /health`, `POST /v1/captures`,
`GET /v1/jobs/:job_id`, and `GET /v1/proposals/:proposal_id`. Public health routes are unauthenticated;
all `/v1/*` routes use one bearer token stored as a Worker secret. It binds only the
`medlearn-control` bucket and dispatches fixed server-side repository, workflow, and ref values.
The exact request bytes are content-addressed as an `intake_digest` under
`v1/intakes/sha256/<digest>.json`; this is deliberately distinct from the canonical CaptureDraft
digest used by proposals. JobRecord 0.2.0 carries only control metadata.

An idempotency claim fixes one `job_id` and intake digest. Every retry repairs a missing intake or
job before continuing. Dispatch uses a conditional 30-second lease: one concurrent caller owns the
attempt, expired/interrupted leases and failed attempts are retryable, and accepted handoffs advance
to `dispatched` with compare-and-swap. This is at-least-once dispatch, not an exactly-once claim;
the proposal workflow deduplicates by `job_id`. Allowed forward status transitions are
`received → dispatched|failed|expired`, `dispatched → running|failed|expired`, and
`running → succeeded|blocked|failed|expired`; `failed → received → dispatched|failed` supports
recovery without creating an invalid leased failure record.
Terminal `succeeded`, `blocked`, and `expired` records reject stale overwrites.

All `/v1/*` responses are `Cache-Control: no-store` and vary on Authorization. Missing or weak
secrets/bindings fail closed with sanitized `503 SERVICE_MISCONFIGURED`.

These services are replaceable adapters, not core dependencies. This slice intentionally contains
one proposal-only GitHub workflow but no approval, commit, Vault write, Obsidian integration, Skill,
iOS Shortcut, MCP, user accounts, tenants, D1, or Durable Objects. The `medlearn-vault` bucket is
not bound or accessed.

## Proposal workflow

`medlearn-propose.yml` accepts only `job_id`, `intake_object_key`, and `intake_digest`. Repository,
ref, bucket, bundle path, workflow, and permissions are not client inputs. It validates the stored
JobRecord, exact IntakeEnvelope bytes, and configured ContractBundle before leasing
`v1/executions/<job_id>.json`.

The bundle path comes only from repository variable `MEDLEARN_PROPOSE_BUNDLE_PATH`. Empty,
absolute, traversing, escaping-symlink, missing, and invalid bundle directories fail closed. There
is deliberately no GERD or COPD production fallback.

The workflow advances `dispatched → running`, then create-only writes proposal JSON and review
Markdown. Ready proposals finish as `succeeded`; review-blocked proposals finish as `blocked`
while the workflow itself succeeds. It uses no Actions artifact or step summary.
