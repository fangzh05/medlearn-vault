import json
from pathlib import Path

from typer.testing import CliRunner

from medlearn_vault.bundle import ContractBundle
from medlearn_vault.capture import (
    CaptureDraft,
    CaptureProposal,
    build_capture_proposal,
    capture_draft_digest,
    capture_proposal_digest,
    render_capture_proposal_markdown,
)
from medlearn_vault.cli import app


def draft() -> CaptureDraft:
    return CaptureDraft.model_validate_json(
        Path("examples/capture/copd-session/draft.json").read_text(encoding="utf-8")
    )


def test_capture_golden_is_deterministic_and_review_only() -> None:
    bundle = ContractBundle.from_directory(Path("examples/copd"))
    value = draft()
    before_bundle, before_draft = bundle.model_dump(), value.model_dump()
    first = build_capture_proposal(bundle, value)
    second = build_capture_proposal(bundle, value)
    assert first == second
    assert first.status == "ready_for_review"
    assert first.claim_proposals[0].proposed_verification_status == "unverified_chat"
    assert first.claim_proposals[0].matching_existing_claim_ids
    assert first.learning_capture_candidate.observations[0].observation_type == "misconception"
    assert first.learning_capture_candidate.observations[0].correction_claim_ids
    assert bundle.model_dump() == before_bundle
    assert value.model_dump() == before_draft
    assert capture_draft_digest(value) == capture_draft_digest(value.model_copy())
    assert capture_proposal_digest(first) == first.proposal_digest
    assert "COPD（慢性阻塞性肺疾病）" in render_capture_proposal_markdown(first, bundle=bundle)
    expected = CaptureProposal.model_validate_json(
        Path("examples/capture/copd-session/expected_proposal.json").read_text(encoding="utf-8")
    )
    assert first == expected
    assert render_capture_proposal_markdown(first, bundle=bundle) == Path(
        "examples/capture/copd-session/expected_review.md"
    ).read_text(encoding="utf-8")


def test_capture_cli_rejects_tampering_and_stale_bundle(tmp_path: Path) -> None:
    runner = CliRunner()
    proposal_path = tmp_path / "proposal.json"
    review_path = tmp_path / "review.md"
    proposed = runner.invoke(
        app,
        [
            "capture",
            "propose",
            "examples/copd",
            "examples/capture/copd-session/draft.json",
            str(proposal_path),
        ],
    )
    assert proposed.exit_code == 0
    payload = json.loads(proposal_path.read_text(encoding="utf-8"))
    payload["claim_proposals"][0]["statement"] = "tampered"
    proposal_path.write_text(json.dumps(payload), encoding="utf-8")
    reviewed = runner.invoke(
        app, ["capture", "review", "examples/copd", str(proposal_path), str(review_path)]
    )
    assert reviewed.exit_code == 1
    assert "PROPOSAL_DIGEST_MISMATCH" in reviewed.stderr
    assert not review_path.exists()


def test_workflow_schema_is_independent() -> None:
    restored = CaptureDraft.model_validate_json(draft().model_dump_json())
    assert restored.draft_version == "0.1.0"
    assert "schema_version" not in CaptureProposal.model_fields


def test_ambiguous_ms_is_blocked_without_a_concept_reference() -> None:
    bundle = ContractBundle.from_directory(Path("examples/capture/ambiguous-ms/bundle"))
    value = CaptureDraft.model_validate_json(
        Path("examples/capture/ambiguous-ms/draft.json").read_text(encoding="utf-8")
    )
    proposal = build_capture_proposal(bundle, value)
    resolution = next(item for item in proposal.concept_resolutions if item.surface_text == "MS")
    assert proposal.status == "blocked"
    assert resolution.status == "ambiguous"
    assert len(resolution.candidate_concept_ids) == 2
    assert proposal.learning_capture_candidate.observations[0].concept_refs == ()


def test_complete_unknown_term_gets_only_a_stable_candidate_id() -> None:
    bundle = ContractBundle.from_directory(Path("examples/copd"))
    value = CaptureDraft.model_validate_json(
        Path("examples/capture/new-concept/draft.json").read_text(encoding="utf-8")
    )
    proposal = build_capture_proposal(bundle, value)
    candidate = proposal.new_concept_candidates[0]
    assert proposal.status == "ready_for_review"
    assert candidate.candidate_id.startswith("candidate_concept_")
    assert "concept_id" not in type(candidate).model_fields
    assert proposal.claim_proposals[0].concept_refs[0].candidate_id == candidate.candidate_id
    assert build_capture_proposal(bundle, value) == proposal
