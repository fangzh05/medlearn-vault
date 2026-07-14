## Summary
- replace the Worker/Python learning-chat source identity drift with `medlearn.learning_chat_source.v1`: a length-delimited UTF-8 CaptureContext payload that excludes `source_id`
- add shared sanitized APL identity and Worker-envelope goldens; actual Worker conversion bytes are validated by Python before proposal generation
- add `medlearn catalog prepare-patch` to emit a deterministic, non-mutating review directory with proposed `sources.json`, `concepts.json`, `manifest.json`, and `review.md`
- bind catalog updates to exact Proposal object bytes/digest, semantic digest, base bundle digest, and target bundle path
- write every generated catalog patch artifact as explicit UTF-8 bytes with an LF terminator; its source and concept SHA-256 values are checked against the emitted manifest

## Explicit metadata completion (this update)
- `prepare_catalog_patch()` now rejects any `CatalogUpdateProposal` whose status is not `ready_for_manual_merge` with `CATALOG_UPDATE_NOT_READY`
- add `ReviewedMetadataEntry` model and `complete_catalog_update_metadata()` — a deterministic reviewer-supplied metadata completion that:
  - verifies every incomplete concept resolution is covered exactly once
  - checks for duplicate resolution IDs, canonical names, and aliases within the reviewed metadata
  - validates no ID or alias collision with the target base bundle or existing promotions
  - preserves all original Proposal bindings (digest, object digest, base, target path, source)
  - sets `parent_catalog_update_id` to the original blocked update, creating an immutable lineage
  - derives a new deterministic `catalog_update_id` from the completed metadata and all original bindings
  - produces a `ready_for_manual_merge` update
- add `medlearn catalog complete-metadata` CLI command
- `CatalogMergeReceipt` now binds to the completed update's `catalog_update_id`

## Lifecycle fix
- blocked updates can no longer produce partial patches or receipts — the reviewer must explicitly complete metadata
- reproposal lifecycle test now asserts `new_job.status == "succeeded"` and `new_proposal.status == "ready_for_review"` unconditionally
- approval and VaultPublicationPlan run without conditional guards
- added rejection tests for: missing metadata, extra metadata, duplicate resolution_id, alias collision, modified parent catalog update, completing already-ready updates, and blocked patch preparation

## Windows CI root cause and repair
- Run `29339225119`, job `87106264629` failed only in `tests/test_handoff.py::test_apl_worker_python_source_identity_golden_bootstraps_a_candidate` and `tests/test_intake.py::test_cross_runtime_handoff_fixture_preserves_exact_bytes`
- Root cause: the two SHA-256d JSON transport fixtures lacked Git LF-only attributes, so a Windows checkout converted the final LF to CRLF and changed their exact-byte digests
- Mark those fixtures `-text` and cover direct and CLI patch output byte-for-byte: no CR, BOM, or NUL; exactly one LF terminator; and manifest digest equality for `sources.json` and `concepts.json`

## Safety
- Draft PR only; no merge, deploy, production workflow dispatch, R2 access, or secret changes
- incomplete metadata remains non-promotable; candidate IDs never enter LearningCapture
- publication remains exactly two immutable artifacts; no automatic concept merge or claim verification
- every CatalogUpdateProposal identity is cryptographically derived from its contents
- blocked updates cannot produce receipts or catalog patches

## Validation
- Python: Ruff, mypy, schema snapshots (18 ok), pytest — 438 passed, 3 skipped
- Worker: contracts check, TypeScript typecheck, ESLint, 138 unit tests, 8 runtime tests
- Wrangler was invoked only with --dry-run
