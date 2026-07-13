# MedLearn Plugin setup

## Phase A: code complete

The repository contains the MCP lifecycle, OAuth Resource Server boundary, the
explicit-source Skill, and plugin manifest. No `.app.json` is generated and no
OAuth provider is configured, so this phase cannot submit to production.

## Phase B: real deployment

1. Configure an external OAuth/OIDC provider with Authorization Code, PKCE,
   OAuth metadata, ChatGPT-compatible client registration, resource support,
   JWKS, audience, and `medlearn:handoff:submit` scope.
2. Set `MEDLEARN_WORK_OAUTH_ISSUER`, `MEDLEARN_WORK_OAUTH_AUDIENCE`, and
   `MEDLEARN_WORK_OAUTH_ALLOWED_SUBJECT`; deploy the Worker.
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
