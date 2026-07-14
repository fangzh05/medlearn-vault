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
    build_catalog_update_proposal,
    canonical_catalog_update_json,
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
    """Production-shaped regression: no R2 operations occur in this pure contract test."""
    handoff = MedLearnHandoff.model_validate_json(
        (ROOT / "examples/intake/apl-bootstrap-sanitized.json").read_text(encoding="utf-8")
    )
    transport, _ = handoff_submission(handoff)
    _, draft_digest = extract_capture_draft(transport, intake_envelope_digest(transport))
    assert draft_digest.startswith("sha256:")

    base = ContractBundle.from_directory(ROOT / "examples/copd")
    first = build_capture_proposal(base, handoff_to_intake(handoff).draft)
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
    assert len(update.concept_promotions) == 5
    assert {item.surface_text for item in update.incomplete_concept_metadata} == {
        "白细胞淤滞",
        "凝血障碍分型",
    }
    assert update.status == "blocked"
    assert "manually merge" in update.next_action

    duplicate_source = update.model_copy(
        update={
            "source_candidate": update.source_candidate.model_copy(
                update={"source": base.sources[0]}
            )
        }
    )
    with pytest.raises(ValueError, match="CATALOG_PATCH_ID_COLLISION"):
        prepare_catalog_patch(duplicate_source, Path("examples/copd"))
    alias_collision = update.concept_promotions[0].model_copy(
        update={
            "concept": ConceptEntity(
                concept_id="concept_cccccccccccccccccccccccccccccccc",
                canonical_name="COPD",
                concept_type="other",
                scope_note="Deliberate alias-collision regression input.",
            )
        }
    )
    duplicate_alias = update.model_copy(
        update={"concept_promotions": (alias_collision, *update.concept_promotions[1:])}
    )
    with pytest.raises(ValueError, match="CATALOG_PATCH_ALIAS_COLLISION"):
        prepare_catalog_patch(duplicate_alias, Path("examples/copd"))

    patch = prepare_catalog_patch(update, Path("examples/copd"))
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
    assert "Incomplete concept metadata" in (output / "review.md").read_text(encoding="utf-8")
    update_path = tmp_path / "catalog-update.json"
    update_path.write_bytes(canonical_catalog_update_json(update))
    command_output = tmp_path / "prepared-command"
    result = CliRunner().invoke(
        app,
        [
            "catalog",
            "prepare-patch",
            str(update_path),
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

    # Synthetic stand-in for a reviewer manually applying the proposed files and
    # separately supplying the metadata that the blocked proposal cannot promote.
    copied_bundle = tmp_path / "bundle"
    shutil.copytree(ROOT / "examples/copd", copied_bundle)
    shutil.copyfile(output / "sources.json", copied_bundle / "sources.json")
    shutil.copyfile(output / "concepts.json", copied_bundle / "concepts.json")
    reviewed_missing = (
        ConceptEntity(
            concept_id="concept_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            canonical_name="白细胞淤滞",
            concept_type="complication",
            scope_note="The reviewed leukostasis concept for this sanitized regression fixture.",
        ),
        ConceptEntity(
            concept_id="concept_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            canonical_name="凝血障碍分型",
            concept_type="other",
            scope_note=(
                "The reviewed coagulation-classification concept for this sanitized regression "
                "fixture."
            ),
        ),
    )
    reviewed_concepts = json.loads((copied_bundle / "concepts.json").read_text(encoding="utf-8"))
    reviewed_concepts.extend(
        item.model_dump(mode="json", exclude_none=True) for item in reviewed_missing
    )
    (copied_bundle / "concepts.json").write_text(
        json.dumps(
            sorted(reviewed_concepts, key=lambda item: item["concept_id"]),
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    promoted = ContractBundle.from_directory(copied_bundle)
    assert not promoted.validate_integrity()
    second = build_capture_proposal(promoted, handoff_to_intake(handoff).draft)
    assert second.status == "ready_for_review"
    assert all(
        item.proposed_verification_status == "unverified_chat" for item in second.claim_proposals
    )
    capture = materialize_learning_capture(promoted, second)
    assert capture.learner_evidence
    assert capture.source_id in {item.source_id for item in promoted.sources}
    assert all("candidate_" not in value for value in json.dumps(capture.model_dump(mode="json")))

    proposal_bytes = exact_capture_proposal_json(second)
    proposal_object_digest = _digest(proposal_bytes)
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
