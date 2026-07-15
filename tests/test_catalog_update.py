import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from medlearn_vault.bundle import ContractBundle
from medlearn_vault.capture import (
    CaptureDraft,
    build_capture_proposal,
    exact_capture_proposal_json,
    extract_capture_draft,
    intake_envelope_digest,
    materialize_learning_capture,
)
from medlearn_vault.catalog_update import (
    CatalogUpdateProposal,
    ReviewedMetadataEntry,
    build_catalog_update_proposal,
    canonical_catalog_update_json,
    complete_catalog_update_metadata,
    prepare_catalog_patch,
    write_catalog_patch,
)
from medlearn_vault.cli import app
from medlearn_vault.domain import ConceptEntity
from medlearn_vault.handoff import MedLearnHandoff, handoff_submission, handoff_to_intake
from medlearn_vault.publication import build_vault_publication_plan
from medlearn_vault.workflow import (
    ProposalApprovalRecord,
    approval_identity,
    canonical_approval_json,
)

ROOT = Path(__file__).parents[1]


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _assert_deterministic_patch_bytes(value: bytes) -> None:
    assert b"\r" not in value
    assert not value.startswith(b"\xef\xbb\xbf")
    assert b"\x00" not in value
    assert value.endswith(b"\n")
    assert not value.endswith(b"\n\n")


def test_apl_bootstrap_requires_reviewed_catalog_then_publishes_persistent_ids_only(
    tmp_path: Path,
) -> None:
    """Production-shaped regression: no R2 operations occur in this pure contract test.

    The sanitized APL fixture has two incomplete concepts. The lifecycle is:

    1. First Capture Proposal is blocked.
    2. First CatalogUpdateProposal is blocked.
    3. prepare_catalog_patch(blocked_update) fails with CATALOG_UPDATE_NOT_READY.
    4. Reviewer supplies metadata for both incomplete concepts.
    5. Completed update becomes ready_for_manual_merge.
    6. Prepared concepts.json contains all promoted persistent concepts.
    7. Receipt matches the exact final files.
    8. Second Proposal is ready_for_review (after patch application).
    9. Approval runs unconditionally.
    10. VaultPublicationPlan is built unconditionally with exactly two artifacts.
    11. No candidate IDs enter LearningCapture.
    """
    handoff = MedLearnHandoff.model_validate_json(
        (ROOT / "examples/intake/apl-bootstrap-sanitized.json").read_text(encoding="utf-8")
    )
    transport, _ = handoff_submission(handoff)
    _, draft_digest = extract_capture_draft(transport, intake_envelope_digest(transport))
    assert draft_digest.startswith("sha256:")

    base = ContractBundle.from_directory(ROOT / "examples/copd")
    first = build_capture_proposal(base, handoff_to_intake(handoff).draft)

    # ── 1. First Capture Proposal is blocked ─────────────────────────────
    assert first.status == "blocked"
    assert first.source_candidate is not None
    assert first.source_candidate.source.source_type == "learning_chat"
    assert first.source_candidate.source.authority == 0
    assert {issue.code for issue in first.issues} >= {"CATALOG_UPDATE_REQUIRED"}
    assert not {"UNKNOWN_CONCEPT", "UNRESOLVED_CONCEPT_REFERENCE"} & {
        issue.code for issue in first.issues
    }
    assert first.learning_capture_candidate.capture.learner_evidence == ()

    first_proposal_bytes = exact_capture_proposal_json(first)

    # ── 2. First CatalogUpdateProposal is blocked ────────────────────────
    update = build_catalog_update_proposal(
        first,
        capture_proposal_object_digest=_digest(first_proposal_bytes),
        target_bundle_path="examples/copd",
    )
    assert update.capture_proposal_id == first.proposal_id
    assert update.capture_proposal_digest == first.proposal_digest
    assert update.capture_proposal_object_digest == _digest(first_proposal_bytes)
    assert update.base_bundle_digest == first.base_bundle_digest
    assert update.target_bundle_path == "examples/copd"
    assert update.source_candidate == first.source_candidate
    assert len(update.concept_promotions) == 4
    assert {item.surface_text for item in update.incomplete_concept_metadata} == {
        "白细胞淤滞",
        "凝血障碍分型",
    }
    assert update.status == "blocked"
    assert "manually merge" in update.next_action

    # ── 3. prepare_catalog_patch(blocked_update) fails ───────────────────
    with pytest.raises(ValueError, match="CATALOG_UPDATE_NOT_READY"):
        prepare_catalog_patch(update, Path("examples/copd"))

    # ── 4. Reviewer supplies metadata for both incomplete concepts ───────
    # Build metadata that matches the two incomplete resolutions
    incomplete_resolutions = list(update.incomplete_concept_metadata)
    assert len(incomplete_resolutions) == 2
    leukostasis = next(
        r for r in incomplete_resolutions if r.surface_text == "白细胞淤滞"
    )
    coagulation = next(
        r for r in incomplete_resolutions if r.surface_text == "凝血障碍分型"
    )

    reviewed_metadata = (
        ReviewedMetadataEntry(
            resolution_id=leukostasis.resolution_id,
            canonical_name="白细胞淤滞",
            preferred_english="Leukostasis",
            concept_type="complication",
            scope_note=(
                "The reviewed leukostasis concept for this sanitized regression fixture."
            ),
            aliases=(),
        ),
        ReviewedMetadataEntry(
            resolution_id=coagulation.resolution_id,
            canonical_name="凝血障碍分型",
            concept_type="other",
            scope_note=(
                "The reviewed coagulation-classification concept for this sanitized "
                "regression fixture."
            ),
            aliases=(),
        ),
    )

    # ── 5. Completed update becomes ready_for_manual_merge ───────────────
    completed = complete_catalog_update_metadata(
        update, reviewed_metadata, Path("examples/copd")
    )
    assert completed.status == "ready_for_manual_merge"
    assert completed.parent_catalog_update_id == update.catalog_update_id
    assert completed.catalog_update_id != update.catalog_update_id
    assert completed.incomplete_concept_metadata == ()
    # Original bindings are preserved
    assert completed.capture_proposal_id == update.capture_proposal_id
    assert completed.capture_proposal_digest == update.capture_proposal_digest
    assert completed.capture_proposal_object_digest == update.capture_proposal_object_digest
    assert completed.base_bundle_digest == update.base_bundle_digest
    assert completed.target_bundle_path == update.target_bundle_path
    # All 6 promotions: 4 original + 2 completed
    assert len(completed.concept_promotions) == 6

    # Test collision scenarios on the *completed* update
    duplicate_source = completed.model_copy(
        update={
            "source_candidate": completed.source_candidate.model_copy(
                update={"source": base.sources[0]}
            )
        }
    )
    with pytest.raises(ValueError, match="CATALOG_PATCH_ID_COLLISION"):
        prepare_catalog_patch(duplicate_source, Path("examples/copd"))

    alias_collision = completed.concept_promotions[0].model_copy(
        update={
            "concept": ConceptEntity(
                concept_id="concept_cccccccccccccccccccccccccccccccc",
                canonical_name="COPD",
                concept_type="other",
                scope_note="Deliberate alias-collision regression input.",
            )
        }
    )
    duplicate_alias = completed.model_copy(
        update={"concept_promotions": (alias_collision, *completed.concept_promotions[1:])}
    )
    with pytest.raises(ValueError, match="CATALOG_PATCH_ALIAS_COLLISION"):
        prepare_catalog_patch(duplicate_alias, Path("examples/copd"))

    # ── 6. Prepared concepts.json contains all promoted concepts ────────
    patch = prepare_catalog_patch(completed, Path("examples/copd"))
    output = tmp_path / "prepared"
    write_catalog_patch(patch, output)
    assert {path.name for path in output.iterdir()} == {
        "sources.json",
        "concepts.json",
        "manifest.json",
        "review.md",
    }
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["sources_old_digest"] != manifest["sources_new_digest"]
    assert manifest["concepts_old_digest"] != manifest["concepts_new_digest"]
    source_bytes = (output / "sources.json").read_bytes()
    concept_bytes = (output / "concepts.json").read_bytes()
    assert source_bytes == patch.sources_json.encode("utf-8")
    assert concept_bytes == patch.concepts_json.encode("utf-8")
    assert _digest(source_bytes) == manifest["sources_new_digest"]
    assert _digest(concept_bytes) == manifest["concepts_new_digest"]
    for name in ("sources.json", "concepts.json", "manifest.json", "review.md"):
        _assert_deterministic_patch_bytes((output / name).read_bytes())

    # ── 7. Receipt matches the exact final files ─────────────────────────
    receipt_path = (
        output.parent
        / "catalog_updates"
        / patch.receipt.catalog_update_id
        / "receipt.json"
    )
    assert receipt_path.exists()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["catalog_update_id"] == completed.catalog_update_id
    assert receipt["sources_new_digest"] == manifest["sources_new_digest"]
    assert receipt["concepts_new_digest"] == manifest["concepts_new_digest"]

    # Verify concepts.json contains the promoted concepts alongside existing ones
    concepts_data = json.loads(patch.concepts_json)
    # Existing bundle concepts plus the promotions generated by this fixture.
    assert len(concepts_data) == len(base.concepts) + len(completed.concept_promotions)
    concept_names = {c["canonical_name"] for c in concepts_data}
    # All promoted concepts are present
    assert "白细胞淤滞" in concept_names
    assert "凝血障碍分型" in concept_names
    for promo in update.concept_promotions:
        assert promo.concept.canonical_name in concept_names

    # ── CLI command produces identical output ────────────────────────────
    completed_path = tmp_path / "completed-update.json"
    completed_path.write_bytes(canonical_catalog_update_json(completed))
    command_output = tmp_path / "prepared-command"
    result = CliRunner().invoke(
        app,
        [
            "catalog",
            "prepare-patch",
            str(completed_path),
            "--bundle",
            "examples/copd",
            "--output",
            str(command_output),
        ],
    )
    assert result.exit_code == 0
    for name in ("sources.json", "concepts.json", "manifest.json", "review.md"):
        assert (command_output / name).read_bytes() == (output / name).read_bytes()
        _assert_deterministic_patch_bytes((command_output / name).read_bytes())

    # Also test the complete-metadata CLI command
    blocked_path = tmp_path / "blocked-update.json"
    blocked_path.write_bytes(canonical_catalog_update_json(update))
    metadata_path = tmp_path / "reviewed-metadata.json"
    metadata_path.write_text(
        json.dumps(
            [item.model_dump(mode="json") for item in reviewed_metadata],
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    cli_completed_path = tmp_path / "cli-completed-update.json"
    result = CliRunner().invoke(
        app,
        [
            "catalog",
            "complete-metadata",
            str(blocked_path),
            "--metadata",
            str(metadata_path),
            "--bundle",
            "examples/copd",
            "--output",
            str(cli_completed_path),
        ],
    )
    assert result.exit_code == 0
    assert f"catalog_update_id={completed.catalog_update_id}" in result.stdout
    assert f"parent_catalog_update_id={update.catalog_update_id}" in result.stdout
    assert "status=ready_for_manual_merge" in result.stdout
    cli_completed = CatalogUpdateProposal.model_validate_json(
        cli_completed_path.read_bytes()
    )
    assert cli_completed.catalog_update_id == completed.catalog_update_id
    assert cli_completed.concept_promotions == completed.concept_promotions

    # ── Apply patch and verify second Proposal is ready_for_review ───────
    copied_bundle = tmp_path / "bundle"
    shutil.copytree(ROOT / "examples/copd", copied_bundle)
    shutil.copyfile(output / "sources.json", copied_bundle / "sources.json")
    shutil.copyfile(output / "concepts.json", copied_bundle / "concepts.json")

    promoted = ContractBundle.from_directory(copied_bundle)
    assert not promoted.validate_integrity()
    second = build_capture_proposal(promoted, handoff_to_intake(handoff).draft)

    # ── 8. Second Proposal is exactly ready_for_review ───────────────────
    assert second.status == "ready_for_review"
    assert all(
        item.proposed_verification_status == "unverified_chat" for item in second.claim_proposals
    )
    capture = materialize_learning_capture(promoted, second)
    assert capture.learner_evidence
    assert capture.source_id in {item.source_id for item in promoted.sources}
    # ── 11. No candidate IDs enter LearningCapture ────────────────────────
    assert all("candidate_" not in value for value in json.dumps(capture.model_dump(mode="json")))

    proposal_bytes = exact_capture_proposal_json(second)
    proposal_object_digest = _digest(proposal_bytes)

    # ── 9. Approval runs unconditionally ─────────────────────────────────
    approval = ProposalApprovalRecord(
        approval_id=approval_identity(
            second.proposal_id, proposal_object_digest, second.base_bundle_digest
        ),
        proposal_id=second.proposal_id,
        proposal_object_digest=proposal_object_digest,
        expected_base_bundle_digest=second.base_bundle_digest,
        decision="approved",
        decided_at=datetime.fromisoformat("2026-07-14T20:00:00+08:00"),
    )

    # ── 10. VaultPublicationPlan built unconditionally with 2 artifacts ──
    plan = build_vault_publication_plan(
        promoted,
        proposal_bytes,
        canonical_approval_json(approval),
        "sha256:" + "0" * 64,
    )
    assert len(plan.artifacts) == 2
    planned_capture = json.loads(plan.artifacts[0].content_utf8)
    assert all(
        value.startswith("concept_")
        for mention in planned_capture["concept_mentions"]
        for value in mention["candidate_concept_ids"]
    )
    assert all(
        item["concept_id"].startswith("concept_")
        for item in planned_capture["learner_evidence"]
    )


# ── Rejection tests ────────────────────────────────────────────────────────


def _build_blocked_update_for_copd() -> tuple[CatalogUpdateProposal, bytes, ContractBundle]:
    """Shared fixture: build a blocked CatalogUpdateProposal for examples/copd."""
    handoff = MedLearnHandoff.model_validate_json(
        (ROOT / "examples/intake/apl-bootstrap-sanitized.json").read_text(encoding="utf-8")
    )
    base = ContractBundle.from_directory(ROOT / "examples/copd")
    first = build_capture_proposal(base, handoff_to_intake(handoff).draft)
    first_bytes = exact_capture_proposal_json(first)
    update = build_catalog_update_proposal(
        first,
        capture_proposal_object_digest=_digest(first_bytes),
        target_bundle_path="examples/copd",
    )
    assert update.status == "blocked"
    return update, first_bytes, base


def _reviewed_metadata_for(update: CatalogUpdateProposal) -> tuple[ReviewedMetadataEntry, ...]:
    """Produce valid metadata for every incomplete resolution in *update*."""
    incomplete = list(update.incomplete_concept_metadata)
    entries: list[ReviewedMetadataEntry] = []
    for item in incomplete:
        entries.append(
            ReviewedMetadataEntry(
                resolution_id=item.resolution_id,
                canonical_name=item.surface_text,
                concept_type="other",
                scope_note=f"Reviewed metadata for {item.surface_text}.",
                aliases=(),
            )
        )
    return tuple(entries)


def test_reject_prepare_blocked_update() -> None:
    """prepare_catalog_patch must reject any blocked CatalogUpdateProposal."""
    update, _, _ = _build_blocked_update_for_copd()
    with pytest.raises(ValueError, match="CATALOG_UPDATE_NOT_READY"):
        prepare_catalog_patch(update, Path("examples/copd"))


def test_reject_missing_reviewed_metadata() -> None:
    """Every incomplete resolution must have a matching metadata entry."""
    update, _, _ = _build_blocked_update_for_copd()
    incomplete = list(update.incomplete_concept_metadata)
    # Drop the second entry
    partial = (
        ReviewedMetadataEntry(
            resolution_id=incomplete[0].resolution_id,
            canonical_name=incomplete[0].surface_text,
            concept_type="other",
            scope_note="Reviewed.",
        ),
    )
    with pytest.raises(ValueError, match="MISSING_REVIEWED_METADATA"):
        complete_catalog_update_metadata(update, partial, Path("examples/copd"))


def test_reject_extra_reviewed_metadata() -> None:
    """No extra metadata entries beyond the incomplete set are allowed."""
    update, _, _ = _build_blocked_update_for_copd()
    metadata = list(_reviewed_metadata_for(update))
    metadata.append(
        ReviewedMetadataEntry(
            resolution_id="resolution_ffffffffffffffffffffffffffffffff",
            canonical_name="Extra Concept",
            concept_type="other",
            scope_note="This should not be here.",
        )
    )
    with pytest.raises(ValueError, match="EXTRA_REVIEWED_METADATA"):
        complete_catalog_update_metadata(update, tuple(metadata), Path("examples/copd"))


def test_reject_duplicate_resolution_id_in_metadata() -> None:
    """Each resolution_id must appear exactly once in reviewed metadata."""
    update, _, _ = _build_blocked_update_for_copd()
    incomplete = list(update.incomplete_concept_metadata)
    # Both entries use the same resolution_id (duplicate!)
    duped = (
        ReviewedMetadataEntry(
            resolution_id=incomplete[0].resolution_id,
            canonical_name="First",
            concept_type="other",
            scope_note="First entry.",
        ),
        ReviewedMetadataEntry(
            resolution_id=incomplete[0].resolution_id,  # duplicate!
            canonical_name="Second",
            concept_type="other",
            scope_note="Second entry.",
        ),
    )
    with pytest.raises(ValueError, match="DUPLICATE_RESOLUTION_ID_IN_METADATA"):
        complete_catalog_update_metadata(update, duped, Path("examples/copd"))


def test_duplicate_resolutions_can_share_identical_reviewed_concept() -> None:
    """Reviewer may map duplicate surface terms to one reviewed concept."""
    update, _, _ = _build_blocked_update_for_copd()
    incomplete = list(update.incomplete_concept_metadata)
    first, second = incomplete[0], incomplete[1]
    reviewed = (
        ReviewedMetadataEntry(
            resolution_id=first.resolution_id,
            canonical_name="共享审阅概念",
            preferred_english="shared reviewed concept",
            concept_type="other",
            scope_note="Reviewed concept shared by duplicate surface terms.",
        ),
        ReviewedMetadataEntry(
            resolution_id=second.resolution_id,
            canonical_name="共享审阅概念",
            preferred_english="shared reviewed concept",
            concept_type="other",
            scope_note="Reviewed concept shared by duplicate surface terms.",
        ),
    )
    missing = tuple(
        ReviewedMetadataEntry(
            resolution_id=item.resolution_id,
            canonical_name=item.surface_text,
            concept_type="other",
            scope_note=f"Reviewed metadata for {item.surface_text}.",
        )
        for item in incomplete[2:]
    )
    completed = complete_catalog_update_metadata(update, reviewed + missing, Path("examples/copd"))
    names = [item.concept.canonical_name for item in completed.concept_promotions]
    assert names.count("共享审阅概念") == 1


def test_reject_alias_collision_in_completed_metadata() -> None:
    """Aliases within reviewed metadata must not collide with the target bundle."""
    update, _, _ = _build_blocked_update_for_copd()
    incomplete = list(update.incomplete_concept_metadata)
    # One entry with a colliding alias, plus a valid second entry
    colliding = (
        ReviewedMetadataEntry(
            resolution_id=incomplete[0].resolution_id,
            canonical_name=incomplete[0].surface_text,
            concept_type="other",
            scope_note="Reviewed.",
            aliases=("COPD",),  # collides with existing concept in bundle
        ),
        ReviewedMetadataEntry(
            resolution_id=incomplete[1].resolution_id,
            canonical_name=incomplete[1].surface_text,
            concept_type="other",
            scope_note="Valid entry.",
        ),
    )
    with pytest.raises(ValueError, match="COMPLETED_CONCEPT_ALIAS_COLLISION"):
        complete_catalog_update_metadata(update, colliding, Path("examples/copd"))


def test_reject_modified_parent_catalog_update() -> None:
    """A tampered parent_catalog_update_id must not produce the same completed ID."""
    update, _, _ = _build_blocked_update_for_copd()
    # Tamper the update's catalog_update_id (as if the parent was modified)
    tampered_data = update.model_dump(mode="json")
    tampered_data["catalog_update_id"] = "catalog_update_" + "f" * 32
    with pytest.raises(ValueError) as exc_info:
        CatalogUpdateProposal.model_validate(tampered_data)
    # The tampered catalog_update_id doesn't match the hash-derived identity
    assert "catalog_update_id does not match" in str(exc_info.value)


def test_reject_completing_already_ready_update() -> None:
    """A ready_for_manual_merge update cannot be 'completed' again."""
    update, _, _ = _build_blocked_update_for_copd()
    metadata = _reviewed_metadata_for(update)
    completed = complete_catalog_update_metadata(update, metadata, Path("examples/copd"))
    assert completed.status == "ready_for_manual_merge"
    with pytest.raises(ValueError, match="CATALOG_UPDATE_ALREADY_READY"):
        complete_catalog_update_metadata(completed, metadata, Path("examples/copd"))


def test_completed_update_preserves_original_bindings() -> None:
    """The completed update must preserve Proposal digest, object digest, base, and target."""
    update, first_bytes, _ = _build_blocked_update_for_copd()
    metadata = _reviewed_metadata_for(update)
    completed = complete_catalog_update_metadata(update, metadata, Path("examples/copd"))

    assert completed.capture_proposal_id == update.capture_proposal_id
    assert completed.capture_proposal_digest == update.capture_proposal_digest
    assert completed.capture_proposal_object_digest == update.capture_proposal_object_digest
    assert completed.base_bundle_digest == update.base_bundle_digest
    assert completed.target_bundle_path == update.target_bundle_path
    assert completed.source_candidate == update.source_candidate
    assert completed.parent_catalog_update_id == update.catalog_update_id


def test_existing_copd_and_gerd_capture_flows_remain_ready() -> None:
    for name in ("copd", "gerd"):
        bundle = ContractBundle.from_directory(ROOT / "examples" / name)
        assert not bundle.validate_integrity()
    copd = CaptureDraft.model_validate_json(
        (ROOT / "examples/capture/copd-session/draft.json").read_text(encoding="utf-8")
    )
    assert (
        build_capture_proposal(ContractBundle.from_directory(ROOT / "examples/copd"), copd).status
        == "ready_for_review"
    )
