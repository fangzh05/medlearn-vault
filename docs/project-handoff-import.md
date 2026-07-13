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

The Work Skill must receive one explicitly selected Project Source. It must not
scan Sources or chats, use project memory, add evidence, make medical
inferences, save a token, or submit directly over HTTP. OAuth validation and
schema validation are the MCP App boundary; deterministic conversion and the
existing intake business function stay in-process in the Worker.

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
