# Optional cloud-assisted capture architecture

The MedLearn core is local-first. Its required boundary is:

```text
CaptureDraft → deterministic local reconciliation → reviewable CaptureProposal
→ validated LearningCapture
```

One possible cloud-assisted deployment is:

```text
Work Skill → CaptureDraft → Cloudflare Worker → GitHub Actions
→ MedLearn CaptureProposal → user confirmation → R2 Vault writer
→ Remotely Save → Obsidian Mobile
```

ChatGPT Work's built-in model owns language understanding and structured extraction. Its
`CaptureDraft` is untrusted and intentionally carries only session context, referenced-message
metadata, short review evidence, and extracted candidates—not a complete transcript.

MedLearn calls no LLM API. It deterministically validates the draft, uses the exact alias
resolver, reconciles it with the accepted `ContractBundle`, and emits a versioned
`CaptureProposal`. `error` and `review` issues block; warnings remain visible but permit
`ready_for_review`. User assertions and errors become learner observations, never medical fact.

These services are replaceable adapters, not core dependencies. The current implementation contains
no Work Skill, Worker, Actions, R2, Remotely Save, approval, commit, or Vault writer.
