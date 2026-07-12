# Workflow contracts

Persistent domain records remain at contract 1.2.0. Temporary capture workflow records have an
independent 0.3.0 version under `schemas/workflow/current/`; they are not a ninth persistent domain
aggregate.

`CaptureDraft` is untrusted structured extraction. Every assertion references one or more existing
`EvidenceMessage` records. Ownership is derived exclusively from their roles: missing or mixed-role
evidence is invalid, and learner evidence must be user-owned. A draft cannot declare or override a
speaker role. Correctness is an explicit observed outcome using the existing `LearnerEvidence`
taxonomy; matching user and assistant text does not establish correctness.

Client source metadata is not authenticated identity and no longer appears in `CaptureContext`.
Cloud intake wraps the draft in versioned `IntakeEnvelope` 0.1.0 whose `client_kind` is one of
`chatgpt_work`, `ios_shortcut`, or `manual`; it never grants permission. Misconceptions separate
`observed_error_message_ids` from `correction_message_ids`. Observed error evidence must be
user-owned, while correction evidence may reference assistant messages. `observed_at` derives
only from observed error evidence.

The committed `intake_envelope.schema.json` is generated from the Python model and is the shared
Worker/Python contract. The intake digest hashes exact HTTP bytes. After verification,
`medlearn capture extract-intake` validates the envelope and nested draft, writes canonical
CaptureDraft JSON, and reports the canonical draft digest. `CaptureProposal.draft_digest` continues
to mean only that canonical CaptureDraft digest.

`ProposalExecutionRecord` 0.1.0 is a control-plane record, not a persistent medical aggregate. It
leases proposal production by `job_id`, records only output identities and digests, and ends as
`succeeded`, `blocked`, or sanitized `failed`. Terminal reruns recompute deterministic bytes and
verify the existing proposal and review; mismatched or missing bytes are `PROPOSAL_COLLISION`.
Only after that verification is a terminal Execution the authoritative record of the completed
operation. A verified terminal rerun rereads its Job and repairs `dispatched`, `running`, or
`failed` to the Execution's status with bounded compare-and-swap retries. It preserves intake
identity, dispatch attempt, and creation time; copies the verified proposal and workflow run IDs;
and clears errors and leases. A stale CAS rereads and accepts only an identical winner. Expired,
identity-mismatched, or conflicting terminal Jobs fail with `CONTROL_STATE_CONFLICT`. Reconciliation
never creates or rewrites Proposal or Review objects.

JobRecord remains 0.2.0. `succeeded` and `blocked` require both `proposal_id` and
`workflow_run_id`; `failed` requires `error_code`; terminal records cannot retain dispatch leases.

`ProposalApprovalRecord` 0.1.0 is the immutable boundary between proposal production and any future
commit workflow. Its deterministic `approval_id` binds exactly the proposal ID, exact stored
Proposal byte digest, expected base bundle digest, and `approved` or `rejected` decision. Records
use canonical key-sorted compact UTF-8 JSON and are stored only at
`v1/approvals/<approval_id>.json` with create-only semantics. An identical request reuses the
canonical winner; malformed, noncanonical, or differently attributed bytes at that identity are an
`APPROVAL_CONFLICT`.

Approval loads only `v1/proposals/<proposal_id>.json` from the fixed `medlearn-control` bucket. It
checks the exact stored byte digest, Proposal contract and internal digest, Proposal ID, ready
status, and base bundle digest before writing. It never reads or mutates a ContractBundle, Proposal,
Review, `medlearn-vault`, Obsidian note, or persistent `LearningCapture`. Inputs cannot select a
bucket, endpoint, repository ref, workflow, or object key.

```bash
medlearn workflow approve \
  proposal_0123456789abcdef0123456789abcdef \
  sha256:<exact-proposal-object-digest> \
  sha256:<expected-base-bundle-digest>
```

## Production workflow operations

The secret-bearing workflow installs only `requirements/workflow.txt`, then installs this package
without dependency resolution or build isolation. Regenerate the lock reproducibly with Python
3.12 and pip-tools 7.5.3:

```bash
python -m pip install pip-tools==7.5.3
python -m piptools compile --generate-hashes --resolver=backtracking --output-file requirements/workflow.txt requirements/workflow.in
```

Configure GitHub Secrets `CONTROL_R2_ENDPOINT`, `CONTROL_R2_ACCESS_KEY_ID`, and
`CONTROL_R2_SECRET_ACCESS_KEY`, plus repository variable `MEDLEARN_PROPOSE_BUNDLE_PATH`. The R2
credentials must be scoped only to the `medlearn-control` control bucket; they require no
`medlearn-vault` access.

After deployment, dispatch a synthetic intake and Job using a non-medical fixture, run
`MedLearn Propose`, and verify that the Execution, deterministic Proposal, Review, and terminal Job
agree on status, proposal ID, digests, workflow run ID, and intake identity. Then fault-inject or
manually retain the synthetic Job as `dispatched`, `running`, and stale `failed`, rerun the workflow,
and verify repair without Proposal/Review writes. Finally verify that expired and conflicting
terminal Jobs are rejected and delete the synthetic control objects.

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
