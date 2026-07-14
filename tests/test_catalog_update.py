import hashlib
import json
from datetime import datetime
from pathlib import Path

from medlearn_vault.bundle import ContractBundle
from medlearn_vault.capture import (
    CaptureDraft,
    CaptureProposal,
    build_capture_proposal,
    extract_capture_draft,
    intake_envelope_digest,
    materialize_learning_capture,
)
from medlearn_vault.catalog_update import build_catalog_update_proposal
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


def _proposal_bytes(proposal: CaptureProposal) -> bytes:
    return (
        json.dumps(
            proposal.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def test_apl_bootstrap_requires_reviewed_catalog_then_publishes_persistent_ids_only() -> None:
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
    assert {issue.code for issue in first.issues} >= {"CATALOG_UPDATE_REQUIRED"}
    assert not {"UNKNOWN_CONCEPT", "UNRESOLVED_CONCEPT_REFERENCE"} & {
        issue.code for issue in first.issues
    }
    assert first.learning_capture_candidate.capture.learner_evidence == ()

    first_proposal_bytes = _proposal_bytes(first)
    update = build_catalog_update_proposal(
        first,
        capture_proposal_object_digest=_digest(first_proposal_bytes),
    )
    assert update.capture_proposal_id == first.proposal_id
    assert update.capture_proposal_digest == first.proposal_digest
    assert update.capture_proposal_object_digest == _digest(first_proposal_bytes)
    assert update.source_candidate == first.source_candidate
    assert len(update.concept_promotions) == 5
    assert {item.surface_text for item in update.incomplete_concept_metadata} == {
        "白细胞淤滞",
        "凝血障碍分型",
    }
    assert update.status == "blocked"
    assert "manually merge" in update.next_action

    # Synthetic stand-in for a reviewer-completed, manually merged catalog patch.
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
    promoted = ContractBundle(
        sources=(*base.sources, first.source_candidate.source),
        concepts=(
            *base.concepts,
            *(item.concept for item in update.concept_promotions),
            *reviewed_missing,
        ),
        claims=base.claims,
        relations=base.relations,
        discipline_lenses=base.discipline_lenses,
        chapters=base.chapters,
        learning_captures=base.learning_captures,
    )
    assert not promoted.validate_integrity()
    second = build_capture_proposal(promoted, handoff_to_intake(handoff).draft)
    assert second.status == "ready_for_review"
    capture = materialize_learning_capture(promoted, second)
    assert capture.learner_evidence
    assert all("candidate_" not in value for value in json.dumps(capture.model_dump(mode="json")))

    proposal_bytes = _proposal_bytes(second)
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
