# MedLearn Plugin

MedLearn Plugin directly invokes the single authenticated
submit_learning_handoff MCP tool.

There is no separate Skill layer. The Plugin instructions define invocation
behavior; the Worker remains the enforceable security and validation boundary.

- Only accepts exactly one explicitly selected or provided `MedLearnHandoff 0.1.0`.
- Does not scan other chats, Project Sources, or project memory.
- Does not supplement, rewrite, or infer evidence.
- `learning_goals` and `unfinished_topics` currently must be empty.
- Successful submission does not equal approval or publication.

For a long, explicitly authorized learning session, submit only the currently
visible 30–50-message `LearningSegment` at a time. Segments are hash-chained;
the client must mark missing coverage as `partial` and must never claim it can
recover chat history that was not submitted. `MedLearnHandoff 0.1.0` remains
unchanged for existing clients.

`.app.json.example` is documentation only and cannot be installed. Generate the real ignored `.app.json` after creating a ChatGPT Developer Mode App:

```powershell
python scripts/configure_medlearn_plugin_app.py plugin_asdk_app_...
```

The real `.app.json` is a locally generated file excluded by `.gitignore`. Never commit the real App binding, token, or secret.
