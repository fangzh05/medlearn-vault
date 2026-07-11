from pathlib import Path

from typer.testing import CliRunner

from medlearn_vault.bundle import ContractBundle
from medlearn_vault.cli import app
from medlearn_vault.domain import ConceptEntity, ConceptRelation, LearningCapture
from medlearn_vault.preview import build_preview_plan, render_markdown

FIXTURE = Path("examples/gerd")


def test_gerd_bundle_is_valid_and_matches_golden_preview() -> None:
    bundle = ContractBundle.from_directory(FIXTURE)
    assert bundle.validate_integrity() == ()
    assert render_markdown(build_preview_plan(bundle)) == (
        FIXTURE / "expected_preview.md"
    ).read_text(encoding="utf-8")


def test_bundle_reports_missing_cross_record_reference() -> None:
    bundle = ContractBundle.from_directory(FIXTURE)
    broken = ConceptRelation(
        relation_id="relation_" + "9" * 32,
        source_concept_id="concept_" + "9" * 32,
        relation_type="treated_by",
        target_concept_id=bundle.concepts[0].concept_id,
    )
    invalid = ContractBundle.model_validate(
        {**bundle.model_dump(), "relations": [*bundle.relations, broken]}
    )
    assert "MISSING_CONCEPT" in {item.code for item in invalid.validate_integrity()}


def test_bundle_checks_capture_source_type_and_merge_cycles() -> None:
    bundle = ContractBundle.from_directory(FIXTURE)
    bad_capture = LearningCapture.model_validate(
        {**bundle.learning_captures[0].model_dump(), "source_id": bundle.sources[0].source_id}
    )
    first = ConceptEntity(
        concept_id="concept_" + "7" * 32,
        canonical_name="cycle one",
        concept_type="other",
        scope_note="cycle test",
        status="merged",
        merged_into="concept_" + "8" * 32,
    )
    second = ConceptEntity(
        concept_id="concept_" + "8" * 32,
        canonical_name="cycle two",
        concept_type="other",
        scope_note="cycle test",
        status="merged",
        merged_into=first.concept_id,
    )
    invalid = ContractBundle.model_validate(
        {
            **bundle.model_dump(),
            "concepts": [*bundle.concepts, first, second],
            "learning_captures": [bad_capture],
        }
    )
    codes = {item.code for item in invalid.validate_integrity()}
    assert {"INVALID_CAPTURE_SOURCE_TYPE", "MERGE_REDIRECT_CYCLE"} <= codes


def test_bundle_cli_and_preview_cli(tmp_path: Path) -> None:
    runner = CliRunner()
    assert runner.invoke(app, ["bundle", "validate", str(FIXTURE)]).exit_code == 0
    output = tmp_path / "preview.md"
    result = runner.invoke(app, ["preview", "render", str(FIXTURE), str(output)])
    assert result.exit_code == 0
    assert output.read_text(encoding="utf-8") == (FIXTURE / "expected_preview.md").read_text(
        encoding="utf-8"
    )
