# Fast composition preview

`medlearn compose preview` is a local-only, deterministic draft-note path. It reads an explicit persisted `IntakeEnvelope`, `MedLearnHandoff`, or `LearningSegment`, and writes only the explicit local `--output` file.

It is separate from the strict Intake → Proposal → Review/Approval → PublicationPlan → Vault publication → sync pipeline. That pipeline, its validation, and its authority remain unchanged. Composition warnings are not medical certification and cannot approve or publish anything.

```powershell
medlearn compose preview --intake intake.json --template template.md --output preview.md
```

When no stable, approved concept target is available, the preview proposes `MedLearn/Inbox/<source_job_id>.md` only when `--source-job-id` was supplied. Otherwise it uses a deterministic `preview_<digest>` source-record identifier from the exact input bytes; it is not a Job ID. The current implementation uses only `StubNoteComposer`; it has no network, secret, R2, Vault, Git, clock, or random access.

This PR provides local preview infrastructure only. Later work may add a private source pack, a real DeepSeek provider, and publication through the existing presentation manifest.
