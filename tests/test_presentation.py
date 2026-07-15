from pathlib import Path

import pytest

from medlearn_vault.bundle import ContractBundle
from medlearn_vault.capture import CaptureProposal
from medlearn_vault.presentation import (
    NO_EXPLANATION,
    build_presentation,
    concept_paths,
    resolve_concept_explanation,
    sanitize_filename,
)

ROOT = Path(__file__).parent.parent


def bundle_and_capture() -> tuple[ContractBundle, object]:
    bundle = ContractBundle.from_directory(ROOT / "examples" / "copd")
    proposal = CaptureProposal.model_validate_json(
        (ROOT / "examples" / "capture" / "copd-session" / "expected_proposal.json").read_bytes()
    )
    return bundle, proposal.learning_capture_candidate.capture


def test_reader_projection_is_deterministic_and_only_exposes_markdown() -> None:
    bundle, capture = bundle_and_capture()
    capture_id = "capture_" + "a" * 32
    first = build_presentation(bundle, ((capture_id, capture, "sha256:" + "b" * 64),))
    second = build_presentation(bundle, ((capture_id, capture, "sha256:" + "b" * 64),))
    assert first == second
    assert first.artifacts
    assert all(
        item.path.startswith(("MedLearn/学习记录/", "MedLearn/概念/")) for item in first.artifacts
    )
    assert all(
        item.path.endswith(".md") and item.content_utf8.endswith("\n") for item in first.artifacts
    )
    capture_note = next(item.content_utf8 for item in first.artifacts if "学习记录" in item.path)
    assert "[[MedLearn/概念/" in capture_note
    assert "concept_" not in capture_note and "claim_" not in capture_note


def test_scope_note_or_explicit_unavailable_is_the_only_fallback() -> None:
    bundle, _ = bundle_and_capture()
    concept = bundle.concepts[0]
    explanation = resolve_concept_explanation(bundle, str(concept.concept_id))
    assert explanation.text == NO_EXPLANATION or explanation.source_type in {
        "scope_note",
        "source_backed",
        "verified_reference",
    }


def test_concept_paths_are_readable_and_filename_rejects_paths() -> None:
    bundle, _ = bundle_and_capture()
    paths = concept_paths(bundle.concepts)
    assert paths and all(path.startswith("MedLearn/概念/") for path in paths.values())
    assert all("concept_" not in Path(path).name for path in paths.values())
    with pytest.raises(ValueError):
        sanitize_filename("../escape")
    with pytest.raises(ValueError):
        sanitize_filename("CON")
