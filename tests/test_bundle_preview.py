import json
import shutil
from pathlib import Path

from typer.testing import CliRunner

from medlearn_vault.bundle import ContractBundle
from medlearn_vault.cli import app
from medlearn_vault.domain import ConceptAlias, ConceptEntity, ConceptRelation, LearningCapture
from medlearn_vault.preview import (
    PreviewBuildError,
    PreviewRequest,
    build_preview_plan,
    render_markdown,
)

FIXTURE = Path("examples/gerd")
COPD_FIXTURE = Path("examples/copd")


def test_gerd_bundle_is_valid_and_matches_golden_preview() -> None:
    bundle = ContractBundle.from_directory(FIXTURE)
    assert bundle.validate_integrity() == ()
    assert render_markdown(build_preview_plan(bundle, PreviewRequest(topic="GERD"))) == (
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
    result = runner.invoke(app, ["preview", "render", str(FIXTURE), str(output), "--topic", "GERD"])
    assert result.exit_code == 0
    assert output.read_text(encoding="utf-8") == (FIXTURE / "expected_preview.md").read_text(
        encoding="utf-8"
    )


def test_copd_uses_same_preview_pipeline() -> None:
    bundle = ContractBundle.from_directory(COPD_FIXTURE)
    assert bundle.validate_integrity() == ()
    markdown = render_markdown(build_preview_plan(bundle, PreviewRequest(topic="copd")))
    assert markdown == (COPD_FIXTURE / "expected_preview.md").read_text(encoding="utf-8")
    assert markdown.startswith("# COPD（慢性阻塞性肺疾病）跨学科学习预览")


def test_preview_topic_errors_are_structured() -> None:
    bundle = ContractBundle.from_directory(FIXTURE)
    try:
        build_preview_plan(bundle, PreviewRequest(topic="unknown"))
    except PreviewBuildError as exc:
        assert exc.code == "TOPIC_NOT_FOUND"
    else:
        raise AssertionError("unknown topic should fail")


def test_ambiguous_abbreviation_warns_and_is_not_selected() -> None:
    bundle = ContractBundle.from_directory(FIXTURE)
    concepts = tuple(
        ConceptEntity(
            concept_id="concept_" + digit * 32,
            canonical_name=name,
            concept_type="disease",
            scope_note="test ambiguity",
            aliases=[ConceptAlias(text="MS", language="en", alias_type="abbreviation")],
        )
        for digit, name in (("7", "二尖瓣狭窄"), ("8", "多发性硬化"))
    )
    ambiguous = ContractBundle.model_validate(
        {**bundle.model_dump(), "concepts": [*bundle.concepts, *concepts]}
    )
    warnings = ambiguous.validate_integrity()
    assert any(item.code == "AMBIGUOUS_ABBREVIATION" for item in warnings)
    try:
        build_preview_plan(ambiguous, PreviewRequest(topic="MS"))
    except PreviewBuildError as exc:
        assert exc.code == "AMBIGUOUS_TOPIC"
    else:
        raise AssertionError("ambiguous abbreviation should fail")


def test_cli_requires_valid_topic(tmp_path: Path) -> None:
    runner = CliRunner()
    output = tmp_path / "preview.md"
    ok = runner.invoke(
        app, ["preview", "render", str(COPD_FIXTURE), str(output), "--topic", "COPD"]
    )
    assert ok.exit_code == 0
    by_id = runner.invoke(
        app,
        [
            "preview",
            "render",
            str(COPD_FIXTURE),
            str(output),
            "--topic",
            "concept_" + "1" * 32,
        ],
    )
    assert by_id.exit_code == 0
    failed = runner.invoke(
        app, ["preview", "render", str(COPD_FIXTURE), str(output), "--topic", "unknown"]
    )
    assert failed.exit_code == 1
    assert "TOPIC_NOT_FOUND" in failed.stderr

    ambiguous_dir = tmp_path / "ambiguous"
    shutil.copytree(COPD_FIXTURE, ambiguous_dir)
    concept_file = ambiguous_dir / "concepts.json"
    concepts = json.loads(concept_file.read_text(encoding="utf-8"))
    concepts.extend(
        {
            "schema_version": "1.2.0",
            "concept_id": "concept_" + digit * 32,
            "canonical_name": name,
            "concept_type": "disease",
            "scope_note": "test ambiguity",
            "aliases": [{"text": "MS", "language": "en", "alias_type": "abbreviation"}],
        }
        for digit, name in (("7", "二尖瓣狭窄"), ("8", "多发性硬化"))
    )
    concept_file.write_text(json.dumps(concepts, ensure_ascii=False), encoding="utf-8")
    warning_result = runner.invoke(app, ["bundle", "validate", str(ambiguous_dir)])
    assert warning_result.exit_code == 0
    assert "AMBIGUOUS_ABBREVIATION" in warning_result.stderr
    ambiguous = runner.invoke(
        app,
        ["preview", "render", str(ambiguous_dir), str(output), "--topic", "MS"],
    )
    assert ambiguous.exit_code == 1
    assert "AMBIGUOUS_TOPIC" in ambiguous.stderr
