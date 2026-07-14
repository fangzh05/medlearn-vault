---
name: submit-learning-handoff
description: Use when the user explicitly selects or provides a MedLearnHandoff 0.1.0 Project Source and asks to submit it to MedLearn. Do not use for ordinary conversation summaries, implicit project memory, or chats without an explicit Handoff.
---

# Submit MedLearn Handoff

Only process exactly one MedLearnHandoff explicitly selected or explicitly provided by the user in the current context. Do not search Project Sources, enumerate chats, inspect other chats, or use project memory.

Do not alter the Handoff or add claims, learner evidence, misconceptions, corrections, unresolved questions, or any other evidence. Do not make medical inferences. Check `handoff_version == "0.1.0"`, `learning_goals == []`, and `unfinished_topics == []`. If no single Handoff is explicitly selected, or more than one is a candidate, stop and ask the user to select one Source.

Pass the Handoff object unchanged (not JSON-encoded as a string) to `submit_learning_handoff`. Do not save tokens, use HTTP directly, or call any other submission tool. On success return only status, job_id, intake_digest, and the returned counts. A repeated identical Source is an idempotent retry. On tool failure show its stable error code without fallback.
