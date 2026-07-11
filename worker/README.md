# medlearn-cloud Worker

Single-user transport adapter for IntakeEnvelope 0.1.0 containing CaptureDraft 0.3.0:

```json
{"intake_version":"0.1.0","client_kind":"manual","draft":{"draft_version":"0.3.0"}}
```

The full shared schema is `schemas/workflow/current/intake_envelope.schema.json`; the committed
runtime validator is generated from it with `npm run contracts:generate` and checked for drift by
`npm run contracts:check`. `client_kind` is untrusted metadata and grants no permission.

R2 keys:

- `v1/intakes/sha256/<exact-body-sha256>.json`
- `v1/jobs/<job_id>.json`
- `v1/idempotency/<idempotency-key-sha256>.json`
- `v1/proposals/<proposal_id>.json` (read-only in this Worker)

The Worker uses a conditional dispatch lease and compare-and-swap job updates. Dispatch is
at-least-once: the future `medlearn-propose.yml` workflow must deduplicate executions by `job_id`.

```powershell
npm ci
npm run lint
npm run typecheck
npm test
npm run contracts:check
```

Deployment requires an ingest token of at least 32 characters and a non-empty GitHub token:

```powershell
npx wrangler secret put MEDLEARN_INGEST_TOKEN
npx wrangler secret put GITHUB_ACTIONS_DISPATCH_TOKEN
npm run deploy
```

The configured `medlearn-control` bucket must already exist. No public URL, account ID, secret, or
credential belongs in repository configuration.
