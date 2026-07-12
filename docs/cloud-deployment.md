# Cloud proposal deployment

Configure these GitHub Actions secrets:

- `CONTROL_R2_ENDPOINT`
- `CONTROL_R2_ACCESS_KEY_ID`
- `CONTROL_R2_SECRET_ACCESS_KEY`

The credentials must be scoped only to the fixed `medlearn-control` bucket. Do not provide
credentials for `medlearn-vault`.

Set repository variable `MEDLEARN_PROPOSE_BUNDLE_PATH` to one validated, repository-relative bundle
directory. It has no default and cannot be supplied by workflow dispatch clients.

Propose and Approve run only from `main` and check out `main` with credential persistence disabled.
Their control-plane R2 credentials are scoped only to the final business step; setup and dependency
installation receive no control credentials. Approval also requires an explicit decision and exact
proposal ID confirmation before its credential-bearing step runs.

`medlearn-plan-publication.yml` likewise runs only from `main`. It requires exact `approval_id`
confirmation before exposing the same control-only credentials, then creates or verifies only the
immutable plan under `medlearn-control`. It has no Vault credential, bucket, or write.

`medlearn-publish-vault.yml` runs only from `main` and requires separate Vault-scoped credentials
configured as additional Actions secrets:

- `VAULT_R2_ENDPOINT`
- `VAULT_R2_ACCESS_KEY_ID`
- `VAULT_R2_SECRET_ACCESS_KEY`

These credentials must be scoped only to the fixed `medlearn-vault` bucket and must not be shared
with any other workflow or step. The workflow validates an exact `publication_plan_id` confirmation
before exposing `CONTROL_R2_*` (read-only) and `VAULT_R2_*` (create-only) in the final step only.
No single job-level environment variable holds both credential sets.

For the permanent synthetic intake workflow, configure Actions secret `MEDLEARN_INGEST_TOKEN` to
the same value held by the Worker and set repository variable `MEDLEARN_INGEST_URL` to the fixed
HTTPS Worker endpoint ending in `/v1/captures`. Neither value is a workflow-dispatch input.

After merging, deploy the updated Worker so its fixed dispatch target can invoke
`medlearn-propose.yml`. Confirm the dispatch token can invoke Actions but has no content-write
permission. Submit a synthetic intake and verify the job, execution, proposal, and review keys in
`medlearn-control`.

No approval, LearningCapture commit, Vault bucket access, Obsidian sync, or mobile intake setup is
part of this deployment.
