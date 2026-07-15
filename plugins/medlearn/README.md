# MedLearn Plugin

MedLearn Plugin directly invokes the single authenticated
`submit_learning_handoff` MCP tool. The normal user command is:

```text
@MedLearn 存档当前学习对话
```

There is no separate Skill layer. The Plugin instructions define invocation
behavior; the Worker remains the enforceable security and validation boundary.

- Builds one `LearningSegment 0.2.0` and nested `MedLearnHandoff 0.1.0` internally; the learner never supplies JSON or contract fields.
- Does not scan other chats, Project Sources, project memory, or hidden history.
- Locally validates the segment, evidence references, marker ordering, timestamps, field placement, and payload byte size before one submission attempt.
- `learning_goals` and `unfinished_topics` currently must be empty.
- Successful submission does not equal approval or publication.

For a long, explicitly authorized learning session, submit the currently
visible requested range. Coverage is
`complete` only when both requested start and end markers are visible and the
full range is represented; otherwise mark it `partial` with an explanation.
Segments remain hash-chained, and the client must never claim it can recover
chat history that was not submitted. If transport bytes require it, the
submission adapter performs byte-aware internal segmentation without asking
the user to split by message count. `MedLearnHandoff 0.1.0` remains unchanged
for existing clients.

`.app.json.example` is documentation only and cannot be installed. Generate the real ignored `.app.json` after creating a ChatGPT Developer Mode App:

```powershell
python scripts/configure_medlearn_plugin_app.py plugin_asdk_app_...
```

The real `.app.json` is a locally generated file excluded by `.gitignore`. Never commit the real App binding, token, or secret.
