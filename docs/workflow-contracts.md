# Workflow contracts

Persistent domain records remain at contract 1.2.0. Temporary capture workflow records have an
independent 0.2.0 version under `schemas/workflow/current/`; they are not a ninth persistent domain
aggregate.

`CaptureDraft` is untrusted structured extraction. Every assertion references one or more existing
`EvidenceMessage` records. Ownership is derived exclusively from their roles: missing or mixed-role
evidence is invalid, and learner evidence must be user-owned. A draft cannot declare or override a
speaker role. Correctness is an explicit observed outcome using the existing `LearnerEvidence`
taxonomy; matching user and assistant text does not establish correctness.

Concept terms for an observed misconception and `correction_terms` are resolved independently.
Authoritative correction matching uses the correction terms and requires an exact statement and
concept-set match to an active supported source-backed or verified-reference claim.

`CaptureProposal` is bound to one exact draft and bundle state. Existing concepts use permanent
`ConceptId` values; proposed concepts retain deterministic candidate IDs and receive no permanent
ID. A proposal carries a complete `LearningCaptureCandidate`, including interval and event times,
context, concept mentions, learner evidence, misconceptions, and open questions.

`materialize_learning_capture` is pure and deterministic. It rejects blocked, stale, tampered,
ambiguous, unresolved, or invalid proposals before returning a persistent `LearningCapture` 1.2.0.
It performs no writes, network access, clock reads, environment access, or ID allocation.

Canonical JSON is UTF-8, key-sorted, compact JSON. Array order is preserved by default, including
message and chronological event order. Only explicitly set-like fields are sorted: concept IDs and
terms, concept references, correction claim IDs and terms, discipline IDs, matching claim IDs,
candidate concept IDs, and issue target IDs. Digests are lowercase `sha256:<64 hex>`.

Assistant statements remain `unverified_chat` / `unassessed`. User assertions and errors never
become medical facts. Review output continues to use the shared bilingual concept formatter.
