# Work Skill capability probe

## Decision

This probe stops before creating a Skill, Worker, or secret.  The available
ChatGPT documentation describes Skills as reusable instructions and supporting
resources that ChatGPT builds and enables, but does not publish an executable
Work Skill package format or runtime contract.  Creating a `skill/` directory,
manifest, script, or temporary bearer-token flow from assumptions would not
test ChatGPT Work; it would only test a locally invented interface.

PR #23 remains unchanged.  No production Worker, R2 bucket, GitHub Actions
token, ingest token, sync token, Work token, or deployment is involved.

## Evidence checked

1. The installed local Skills and related project examples use Codex's
   `SKILL.md` convention.  They are not evidence of a ChatGPT Work execution
   runtime or Project Source API.
2. The local `claude` executable is unrelated to ChatGPT Work and has no local
   documentation for a ChatGPT Work Skill package.
3. The official Skills and Plugins guide says ChatGPT Skills package
   instructions and supporting resources, are created through a ChatGPT
   conversation, and are enabled in ChatGPT.  It does not specify a repository
   manifest, script interpreter, working directory, source-selection handle,
   outbound-network permission, or secret/environment-variable injection API.

Official reference: <https://learn.chatgpt.com/docs/skills-and-plugins>

## Unverified capability matrix

| Check | Status | Reason |
| --- | --- | --- |
| A_SOURCE_READ | NOT_TESTED | No documented interface identifies one explicitly selected Project Source or exposes its bytes to a Skill. |
| B_OUTBOUND_GET | NOT_TESTED | No documented Work Skill outbound HTTP capability or allowlist model. |
| C_OUTBOUND_POST | NOT_TESTED | No documented Work Skill outbound HTTP capability or response contract. |
| D_SECRET_INJECTION | NOT_SUPPORTED | No documented secure Skill secret/config injection mechanism was found.  A chat prompt, Project Source, Skill text, tool parameter, URL, or environment convention inferred from Codex/Claude examples is not an acceptable substitute. |
| E_NO_CONTENT_LEAK | NOT_TESTED | It depends on A--D and on documented Work logging/telemetry behavior. |

## Required Work UI confirmation

Before implementing this probe, confirm these exact platform capabilities in
the ChatGPT Work UI or official product documentation:

1. Can an installed Skill require the user to select exactly one Project Source
   and read that source's complete original bytes, without scanning other
   Sources or chats?  What API or variable carries the selection?
2. Can that Skill execute a script?  If yes, what is the supported package
   root, required manifest/frontmatter, supported languages, current working
   directory, dependency model, and installation/enablement procedure?
3. Can the Skill make allowlisted HTTPS `GET` and `POST` requests?  What are
   the network policy, request-body limits, redirects, and logging guarantees?
4. Is there a secret/config store scoped to one Skill invocation that can pass
   a named secret to the Skill without putting it in sources, prompts, logs,
   tool parameters, command lines, or URLs?  If yes, provide the documented
   reference syntax and audit/redaction behavior.
5. Can Work prove that Source text, request headers, and secret values are
   excluded from execution logs, generated output, and retained telemetry?

## Resumption rule

Proceed with Stage A only after questions 1--3 have official, testable
answers.  Proceed with Stage B only after question 4 is confirmed.  If that
secret mechanism is unavailable, do not build an unauthenticated production
write endpoint; retain the MCP/App direction for PR #23.
