# Fast composition preview

`medlearn compose preview` is a local-only, deterministic draft-note path. It reads an explicit persisted `IntakeEnvelope`, `MedLearnHandoff`, or `LearningSegment`, and writes only the explicit local `--output` file.

It is separate from the strict Intake → Proposal → Review/Approval → PublicationPlan → Vault publication → sync pipeline. That pipeline, its validation, and its authority remain unchanged. 结构或存储不安全才失败；知识不完整只告警。Composition warnings are not medical certification and cannot approve or publish anything.

```powershell
medlearn compose preview --intake intake.json --template template.md --output preview.md
```

An optional local SQLite index can add retrieved source context to that explicit preview file:

```powershell
medlearn compose preview --intake intake.json --template template.md --index medlearn.sqlite3 --retrieval-limit 6 --output preview.md
```

Retrieval is local-only and automatic: it uses up to the first three distinct normalized concept candidates, requests up to two lexical results per concept, preserves source-index ordering, deduplicates chunk IDs deterministically, and applies a final limit of 6 by default (allowed range: 1–12). A missing query produces `SOURCE_QUERY_UNAVAILABLE`; a valid index with no matches produces `SOURCE_NOT_FOUND`; an unreadable or incompatible explicitly supplied index rejects with `COMPOSITION_SOURCE_INDEX_FAILED`. Retrieved text is written only to the explicit local preview output and is never committed, synced, or published. It is source context—not learner evidence or medical certification—and cannot approve claims or change publication authority.

When no stable, approved concept target is available, the preview proposes `MedLearn/Inbox/<source_job_id>.md` only when `--source-job-id` was supplied. Otherwise it uses a deterministic `preview_<digest>` source-record identifier from the exact input bytes; it is not a Job ID. The current implementation uses only `StubNoteComposer`; it has no network, secret, R2, Vault, Git, clock, or random access.

Daily outcomes are `accepted`, `accepted_with_warnings`, or `rejected`. Fast composition creates a draft study note, isolates role-conflicted items, and never silently overwrites a note; the strict path alone certifies formal concepts, claims, and publication.
