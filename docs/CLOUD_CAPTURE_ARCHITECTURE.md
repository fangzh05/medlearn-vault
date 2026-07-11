# Optional cloud-assisted capture architecture

The MedLearn core is local-first. Its required boundary is:

```text
CaptureDraft → deterministic local reconciliation → reviewable CaptureProposal
→ validated LearningCapture
```

The first replaceable, single-user intake adapter is:

```text
IntakeEnvelope → Cloudflare Worker → medlearn-control R2
→ fixed GitHub workflow dispatch → future proposal producer
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
The exact request bytes are content-addressed and idempotency claims use conditional R2 creation.

These services are replaceable adapters, not core dependencies. This slice intentionally contains
no GitHub workflow implementation, approval, commit, Vault write, Obsidian integration, Skill,
iOS Shortcut, MCP, user accounts, tenants, D1, or Durable Objects. The `medlearn-vault` bucket is
not bound or accessed.
