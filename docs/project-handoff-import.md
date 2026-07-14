# Chat Project Source to Work import

MedLearnHandoff 0.1.0 is the explicit handoff from an ordinary Chat learning
conversation to MedLearn Work intake. It is not a memory feature and it is not
a chat scanner.

## User flow

1. Learn in a normal Chat conversation.
2. Ask: `结束本次学习并生成 MedLearnHandoff 0.1.0`.
3. Save that JSON reply as a Project Source in the same ChatGPT Project.
4. Open a Work conversation in that Project and explicitly select that Source.
5. Ask: `读取这个 Source，并调用 MedLearn 提交工具`.
6. Work calls `submit_learning_handoff` and returns the `job_id`.
7. Review, approval, publication, and the Windows Obsidian sync continue through
   the existing MedLearn workflow.

Project memory is only a convenience. It is never formal input; Work must not
invent learner evidence from a vague memory, and the tool processes only the
structured handoff explicitly passed by Work.

## Remote tool boundary

The Worker exposes Streamable MCP at `/mcp`. Discovery (`initialize`,
`notifications/initialized`, `ping`, and `tools/list`) is unauthenticated;
only `tools/call` requires an OAuth access token with
`medlearn:handoff:submit`. It exposes exactly one tool,
`submit_learning_handoff`, with input `{"handoff": {…}}`.

The MedLearn Plugin/App instructions permit submission of exactly one
explicitly selected or provided MedLearnHandoff. The remote Worker independently
enforces OAuth, schema, evidence-reference, conversion, and idempotency boundaries.

### Architectural separation

- **Plugin instructions**: Define invocation behavior and user interaction rules. They are not a security boundary.
- **MCP Tool**: The public capability interface, declaring the input/output schema and required OAuth scope.
- **Worker**: The enforceable security and validation boundary. It performs OAuth validation, Handoff Schema validation, evidence-reference checks, enforces conversion rejection rules, ensures idempotency, and protects the R2 / GitHub workflow business boundary.

The tool validates the strict UTF-8 schema, creates canonical JSON, derives
source/session/message IDs and its idempotency key from the handoff SHA-256,
then calls the existing intake business functions in-process. It does not use
an unlisted Project Source API, a public HTTP self-call, an LLM, or medical
inference. A repeated identical Source returns the same job. It never approves
or publishes anything.

`unresolved_questions` are converted only into explicitly supplied question
candidates. Until CaptureDraft gains an explicit compatible field, non-empty
`learning_goals` or `unfinished_topics` fail with `HANDOFF_CONVERSION_FAILURE`;
the tool never silently turns them into claims, learner evidence, or corrections.

The Worker is an OAuth Resource Server, not an Authorization Server. It uses
an external OIDC/OAuth provider configured with issuer, audience, and one
allowed subject. Until that provider is configured, discovery remains usable
but `tools/call` returns the OAuth challenge and cannot submit.

## Persisted-byte integrity (0.14.1)

Following production acceptance failure `INTAKE_DIGEST_MISMATCH` (run
`29328840172`), the Worker now re-reads the persisted R2 intake as raw bytes
before it creates a JobRecord or dispatches GitHub Actions. A byte-identical
content-addressed object is safely reused. A different object at the same key,
including BOM, whitespace, newline, or JSON key-order differences, returns
`INTAKE_STORAGE_CONFLICT`; it is never overwritten or deleted. Python retains
its independent exact-byte validation when the workflow reads the intake.

Read-only diagnosis of the subsequent production intake confirmed that its
exact SHA-256 matched its content-addressed key. The sanitized Python failure
was at `draft`: `value_error` / `assertion evidence must have exactly one
derived speaker role`. Exact-byte mismatches now remain
`INTAKE_DIGEST_MISMATCH`, while a matching-but-invalid Envelope is recorded as
`INVALID_INTAKE_ENVELOPE`. Before persistence, the Worker also rejects a
Handoff assertion whose evidence references mix user and assistant roles, so
this cross-field domain invariant cannot bypass JSON Schema validation again.
