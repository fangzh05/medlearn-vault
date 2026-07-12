# Cloud proposal deployment

Configure these GitHub Actions secrets:

- `CONTROL_R2_ENDPOINT`
- `CONTROL_R2_ACCESS_KEY_ID`
- `CONTROL_R2_SECRET_ACCESS_KEY`

The credentials must be scoped only to the fixed `medlearn-control` bucket. Do not provide
credentials for `medlearn-vault`.

Set repository variable `MEDLEARN_PROPOSE_BUNDLE_PATH` to one validated, repository-relative bundle
directory. It has no default and cannot be supplied by workflow dispatch clients.

After merging, deploy the updated Worker so its fixed dispatch target can invoke
`medlearn-propose.yml`. Confirm the dispatch token can invoke Actions but has no content-write
permission. Submit a synthetic intake and verify the job, execution, proposal, and review keys in
`medlearn-control`.

No approval, LearningCapture commit, Vault bucket access, Obsidian sync, or mobile intake setup is
part of this deployment.
