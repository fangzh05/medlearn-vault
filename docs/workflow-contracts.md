# Workflow contracts

Persistent domain records remain at contract 1.2.0. Temporary cloud workflow records have an
independent 0.1.0 version and schemas under `schemas/workflow/current/`; they are not a ninth
persistent domain aggregate.

`CaptureDraft` is Work's untrusted structured extraction. `CaptureProposal` is MedLearn's
reviewable result for one exact draft and one exact bundle state. Existing concepts use
`ProposalConceptRef.concept_id`; proposed concepts use deterministic `candidate_id` values and
receive no permanent ID before approval.

Canonical JSON is UTF-8, key-sorted, compact JSON with set-like collections sorted by canonical
content. Digests are lowercase `sha256:<64 hex>`. `proposal_id` hashes workflow version, draft
digest, and bundle digest. `proposal_digest` covers the entire proposal except itself. Review
rejects digest tampering and, by default, `STALE_BASE_BUNDLE`.

Assistant statements are always proposed as `unverified_chat` / `unassessed`; only exact matches
to existing active supported claims can serve as authoritative corrections. No command is
interactive, and CLI diagnostics omit excerpts and medical body text.
