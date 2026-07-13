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

The existing Cloudflare Worker exposes authenticated Streamable MCP at `/mcp`.
It exposes exactly one tool, `submit_learning_handoff`, with input
`{"handoff": {…}}`. Configure the connector with the server-side
`MEDLEARN_WORK_TOKEN`; do not put a token in a Project Source, tool arguments,
logs, or user-facing errors. `MEDLEARN_WORK_TOKEN` is separate from the ingest,
sync, and GitHub credentials.

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
