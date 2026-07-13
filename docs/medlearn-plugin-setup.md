# MedLearn Plugin setup

## Phase A: code complete

The repository contains the MCP lifecycle, OAuth Resource Server boundary, the
explicit-source Skill, and plugin manifest. No `.app.json` is generated and no
OAuth provider is configured, so this phase cannot submit to production.

## Phase B: real deployment

1. Configure an external OAuth/OIDC provider with Authorization Code, PKCE,
   OAuth metadata, ChatGPT-compatible client registration, resource support,
   JWKS, audience, and `medlearn:handoff:submit` scope.
2. Set `MEDLEARN_WORK_OAUTH_ISSUER`, `MEDLEARN_WORK_OAUTH_AUDIENCE`,
   `MEDLEARN_WORK_OAUTH_RESOURCE`, and `MEDLEARN_WORK_OAUTH_ALLOWED_SUBJECT`;
   deploy the Worker.
3. Verify OAuth in MCP Inspector, enable ChatGPT Developer Mode, then create a
   Plugin pointing to `https://medlearn-cloud.fzh050531.workers.dev/mcp`.
4. Complete OAuth, copy the resulting `plugin_asdk_app...` ID, and run:

   ```powershell
   python scripts/configure_medlearn_plugin_app.py plugin_asdk_app_...
   ```

5. Commit the generated `.app.json` and manifest wiring, install the local
   plugin, then in a new Work task explicitly select one Project Source and
   invoke `@MedLearn` / `submit-learning-handoff`.
6. Verify the returned `job_id`, then repeat the same Source to confirm
   idempotency.

`plugins/medlearn/.app.json.example` is documentation only; it is not an
installable app configuration.

## Phase C: OAuth staging

For OAuth staging validation, a second Cloudflare Worker environment
(`oauth-staging`) and a second Auth0 API are used. Production and staging
audiences must never overlap.

### Auth0 — production API

```
Name:       MedLearn Work API
Identifier: https://medlearn-cloud.fzh050531.workers.dev
Algorithm:  RS256
```

Permission: `medlearn:handoff:submit`

### Auth0 — staging API

```
Name:       MedLearn Work API Staging
Identifier: https://medlearn-cloud-oauth-staging.fzh050531.workers.dev
Algorithm:  RS256
```

Permission: `medlearn:handoff:submit`

Do not delete or modify the production API when adding the staging one.
The same Auth0 tenant is acceptable; the API Identifier (audience) is
what separates the two environments.

### Worker environment variables

| Variable | Production | Staging |
|---|---|---|
| `MEDLEARN_WORK_OAUTH_ISSUER` | `https://medlearn-fzh.jp.auth0.com/` | same |
| `MEDLEARN_WORK_OAUTH_AUDIENCE` | `https://medlearn-cloud.fzh050531.workers.dev` | `https://medlearn-cloud-oauth-staging.fzh050531.workers.dev` |
| `MEDLEARN_WORK_OAUTH_RESOURCE` | `https://medlearn-cloud.fzh050531.workers.dev` | `https://medlearn-cloud-oauth-staging.fzh050531.workers.dev` |
| `MEDLEARN_WORK_OAUTH_ALLOWED_SUBJECT` | (Cloudflare Secret) | (Cloudflare Secret) |

### Important notes

- The Client ID (`6wKslWjFCJTCN911uUPucA6EBEgmLv3g`) is NOT a Worker
  environment variable; it is used only in the Auth0 Application and the
  ChatGPT Developer App configuration.
- The Client Secret must never enter a Worker or environment variable.
- `MEDLEARN_WORK_OAUTH_ALLOWED_SUBJECT` must be set as a Cloudflare Secret
  (`npx wrangler secret put`), never in `wrangler.toml` or source code.
- The staging Worker has no R2 buckets or GitHub Actions token; `tools/call`
  returns `SERVICE_MISCONFIGURED` on staging, which is expected.
- After staging acceptance, the staging Worker and staging Auth0 API may be
  deleted.
