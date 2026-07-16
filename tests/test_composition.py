import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from medlearn_vault.cli import app
from medlearn_vault.composition import (
    CompositionContext,
    build_context,
    compose_preview,
    validate_target_path,
)

FIXTURE = Path("examples/intake/manual-copd.json")
HANDOFF = Path("examples/intake/project-handoff-synthetic.json")


def test_intake_composes_deterministically_and_uses_inbox() -> None:
    context = build_context(FIXTURE.read_bytes(), template="# Template", current_note="# Existing")
    first = compose_preview(context)
    second = compose_preview(context)
    assert first.markdown == second.markdown
    assert context.source_job_id is None
    assert first.target_path == f"MedLearn/Inbox/{context.source_record_id}.md"
    assert "# Template" in first.markdown and "# Existing" in first.markdown


def test_missing_context_and_concept_are_warnings() -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["draft"]["context"].update(course_id=None, chapter_id=None)
    context = build_context(json.dumps(payload).encode(), template="")
    assert {issue.code for issue in context.warnings} >= {
        "MISSING_COURSE_ID",
        "MISSING_CHAPTER_ID",
    }


def test_malformed_and_empty_intakes_are_blocked() -> None:
    with pytest.raises(ValueError, match="INVALID_COMPOSITION_INTAKE"):
        build_context(b"{}", template="")
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["draft"]["claim_candidates"] = []
    payload["draft"]["generated_explanations"] = []
    payload["draft"]["learner_evidence_candidates"] = []
    payload["draft"]["misconception_candidates"] = []
    with pytest.raises(ValueError, match="NO_USABLE_LEARNING_CONTENT"):
        build_context(json.dumps(payload).encode(), template="")


@pytest.mark.parametrize("field", ["learner_evidence", "misconceptions", "unresolved_questions"])
def test_non_claim_learning_material_composes(field: str) -> None:
    payload = json.loads(HANDOFF.read_text(encoding="utf-8"))
    for name in (
        "learning_goals",
        "claims",
        "learner_evidence",
        "misconceptions",
        "unresolved_questions",
    ):
        if name != field:
            payload[name] = []
    context = build_context(json.dumps(payload).encode(), template="")
    assert compose_preview(context).markdown


def test_explicit_source_job_id_is_preserved_and_used_for_inbox() -> None:
    context = build_context(FIXTURE.read_bytes(), template="", source_job_id="job_123")
    assert context.source_job_id == "job_123"
    assert context.proposed_target_path == "MedLearn/Inbox/job_123.md"
    with pytest.raises(ValueError, match="INVALID_SOURCE_JOB_ID"):
        build_context(FIXTURE.read_bytes(), template="", source_job_id="bad/path")


def test_empty_composer_does_not_write_output(tmp_path: Path) -> None:
    class Empty:
        def compose(self, context: CompositionContext) -> str:
            return ""

    context = build_context(FIXTURE.read_bytes(), template="")
    with pytest.raises(ValueError, match="EMPTY_COMPOSER_OUTPUT"):
        compose_preview(context, Empty())
    assert not (tmp_path / "out.md").exists()


@pytest.mark.parametrize(
    "path", ["../bad.md", "/MedLearn/bad.md", "MedLearn/../bad.md", "C:\\MedLearn\\bad.md"]
)
def test_unsafe_targets_are_rejected(path: str) -> None:
    with pytest.raises(ValueError, match="UNSAFE_COMPOSITION_TARGET_PATH"):
        validate_target_path(path)


def test_cli_writes_only_explicit_output(tmp_path: Path) -> None:
    template = tmp_path / "template.md"
    template.write_text("# Template\n", encoding="utf-8")
    output = tmp_path / "preview.md"
    result = CliRunner().invoke(
        app,
        [
            "compose",
            "preview",
            "--intake",
            str(FIXTURE),
            "--template",
            str(template),
            "--output",
            str(output),
            "--source-job-id",
            "job_123",
            "--json",
        ],
    )
    assert result.exit_code == 0
    assert '"status":"accepted"' in result.stdout
    assert "MedLearn/Inbox/job_123.md" in result.stdout
    assert output.exists()


def test_cli_output_write_error_is_sanitized(tmp_path: Path) -> None:
    template = tmp_path / "template.md"
    template.write_text("# Template\n", encoding="utf-8")
    result = CliRunner().invoke(
        app,
        [
            "compose",
            "preview",
            "--intake",
            str(FIXTURE),
            "--template",
            str(template),
            "--output",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 1
    assert "status=rejected error_code=COMPOSITION_OUTPUT_WRITE_FAILED" in result.stdout
    assert "Traceback" not in result.stdout


def test_cli_digest_mismatch_is_rejected(tmp_path: Path) -> None:
    template = tmp_path / "template.md"
    template.write_text("# Template\n", encoding="utf-8")
    result = CliRunner().invoke(
        app,
        [
            "compose",
            "preview",
            "--intake",
            str(FIXTURE),
            "--template",
            str(template),
            "--output",
            str(tmp_path / "out.md"),
            "--expected-intake-digest",
            "sha256:" + "0" * 64,
        ],
    )
    assert result.exit_code == 1
    assert "COMPOSITION_INPUT_DIGEST_MISMATCH" in result.stdout
