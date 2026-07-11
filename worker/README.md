# medlearn-cloud Worker

Single-user transport adapter for CaptureDraft 0.3.0. The request body is:

```json
{"client_kind":"manual","draft":{"draft_version":"0.3.0"}}
```

`client_kind` is untrusted metadata. The full draft fields remain governed by the committed workflow
schema and are validated by the Python core after intake.

R2 keys:

- `v1/drafts/sha256/<body-sha256>.json`
- `v1/jobs/<job_id>.json`
- `v1/idempotency/<idempotency-key-sha256>.json`
- `v1/proposals/<proposal_id>.json` (read-only in this Worker)

Deployment requires `wrangler secret put MEDLEARN_INGEST_TOKEN` and
`wrangler secret put GITHUB_ACTIONS_DISPATCH_TOKEN`, followed by `npx wrangler deploy`. The configured
`medlearn-control` bucket must already exist. No public URL, account ID, secret, or credential belongs
in repository configuration.
