import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from medlearn_vault.cli import app
from medlearn_vault.composition import (
    CompositionContext,
    attach_retrieval,
    build_context,
    compose_preview,
    validate_target_path,
)
from medlearn_vault.handoff import LearningSegment, MedLearnHandoff
from medlearn_vault.source_index import build_index

FIXTURE = Path("examples/intake/manual-copd.json")
HANDOFF = Path("examples/intake/project-handoff-synthetic.json")


def _source(root: Path, name: str, text: str) -> None:
    directory = root / name
    directory.mkdir(parents=True)
    identity = {
        "source_relative_path": f"synthetic/{name}",
        "source_file": name,
        "source_sha256": "sha256:" + "a" * 64,
    }
    chunk = {
        **identity,
        "chunking_version": "1",
        "structure_version": "1",
        "normalization_version": "1",
        "chunk_id": f"chunk_{name}",
        "chunk_index": 0,
        "section_id": "section_1",
        "section_path": ["section_1"],
        "section_titles": ["Synthetic section"],
        "start_pdf_page_number": 2,
        "end_pdf_page_number": 3,
        "text": text,
        "text_sha256": "sha256:" + hashlib.sha256(text.encode()).hexdigest(),
        "char_count": len(text),
    }
    (directory / "sections.jsonl").write_text(
        json.dumps({"section_id": "section_1", "title": "Synthetic section"}) + "\n",
        encoding="utf-8",
    )
    chunks = json.dumps(chunk) + "\n"
    (directory / "chunks.jsonl").write_text(chunks, encoding="utf-8")
    digest = "sha256:" + hashlib.sha256((directory / "chunks.jsonl").read_bytes()).hexdigest()
    (directory / "chunking-report.json").write_text(
        json.dumps(
            {
                **identity,
                "chunking_version": "1",
                "structure_version": "1",
                "supported_normalization_version": "1",
                "chunk_count": 1,
                "output_digests": {"chunks.jsonl": digest},
            }
        ),
        encoding="utf-8",
    )


def _context_with_concepts(*concepts: str) -> CompositionContext:
    return replace(
        build_context(FIXTURE.read_bytes(), template="# Template"), concept_candidates=concepts
    )


def _index(tmp_path: Path, *texts: str) -> Path:
    for number, text in enumerate(texts):
        _source(tmp_path / "input", f"source_{number}.pdf", text)
    index = tmp_path / "index.sqlite3"
    build_index(tmp_path / "input", index, "0.22.0")
    return index


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


def test_learning_segment_uses_nested_strict_handoff() -> None:
    handoff = MedLearnHandoff.model_validate_json(HANDOFF.read_bytes())
    segment = LearningSegment(
        learning_session_id="composition_segment_1",
        segment_index=0,
        first_evidence_marker=handoff.evidence_messages[0].local_id,
        last_evidence_marker=handoff.evidence_messages[-1].local_id,
        segment_message_count=len(handoff.evidence_messages),
        coverage_status="complete",
        handoff=handoff,
    )
    context = build_context(segment.model_dump_json().encode(), template="")
    assert compose_preview(context).markdown


def test_retrieval_attaches_sources_digest_and_markdown(tmp_path: Path) -> None:
    index = _index(tmp_path, "alpha exact synthetic text", "alpha another synthetic text")
    context = attach_retrieval(_context_with_concepts(" alpha "), index)
    assert len(context.retrieved_sources) == 2
    assert context.retrieval_digest and context.retrieval_digest.startswith("sha256:")
    assert "SOURCE_MISSING" not in {item.code for item in context.warnings}
    markdown = compose_preview(context).markdown
    assert "## Retrieved source context" in markdown and "alpha exact synthetic text" in markdown
    assert "- Pages: `2-3`" in markdown and "- Retrieval query: `alpha`" in markdown


def test_retrieval_queries_first_three_in_order_and_deduplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context_with_concepts(" A  B ", "a b", "Second", "Third", "Fourth")
    calls: list[tuple[str, int]] = []

    def fake_search(index: Path, query: str, limit: int = 10) -> dict[str, object]:
        calls.append((query, limit))
        row = {
            "rank": 1,
            "score": 1,
            "chunk_id": "shared" if query != "Second" else "second",
            "source_relative_path": "synthetic/a.pdf",
            "source_file": "a.pdf",
            "section_id": "s",
            "section_titles": ["S"],
            "start_pdf_page_number": 1,
            "end_pdf_page_number": 1,
            "text": "synthetic",
            "text_sha256": "sha256:" + "b" * 64,
        }
        return {"results": [row, row]}

    monkeypatch.setattr("medlearn_vault.composition.search_index", fake_search)
    attached = attach_retrieval(context, Path("unused.sqlite3"))
    assert calls == [("A B", 2), ("Second", 2), ("Third", 2)]
    assert [item.chunk_id for item in attached.retrieved_sources] == ["shared", "second"]


def test_retrieval_limit_digest_and_warning_outcomes(tmp_path: Path) -> None:
    index = _index(tmp_path, "alpha first", "alpha second", "alpha third")
    context = _context_with_concepts("alpha")
    first = attach_retrieval(context, index, retrieval_limit=1)
    second = attach_retrieval(context, index, retrieval_limit=1)
    assert len(first.retrieved_sources) == 1 and first.retrieval_digest == second.retrieval_digest
    no_concepts = attach_retrieval(_context_with_concepts(), index)
    assert {item.code for item in no_concepts.warnings} >= {"SOURCE_QUERY_UNAVAILABLE"}
    no_matches = attach_retrieval(_context_with_concepts("missing"), index)
    assert {item.code for item in no_matches.warnings} >= {"SOURCE_NOT_FOUND"}


def test_retrieval_index_failure_and_cli_payload_are_safe(tmp_path: Path) -> None:
    template = tmp_path / "template.md"
    template.write_text("# Template", encoding="utf-8")
    output = tmp_path / "preview.md"
    missing = tmp_path / "missing.sqlite3"
    result = CliRunner().invoke(
        app,
        [
            "compose",
            "preview",
            "--intake",
            str(FIXTURE),
            "--template",
            str(template),
            "--index",
            str(missing),
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 1 and "COMPOSITION_SOURCE_INDEX_FAILED" in result.stdout
    assert "Traceback" not in result.stdout
    index = _index(tmp_path / "good", "alpha private synthetic text")
    intake = tmp_path / "intake.json"
    intake.write_text(json.dumps(json.loads(FIXTURE.read_text(encoding="utf-8"))), encoding="utf-8")
    payload = json.loads(intake.read_text(encoding="utf-8"))
    payload["draft"]["concept_mentions"] = [
        {"surface_text": "alpha", "evidence_message_ids": ["message:user-001"]}
    ]
    intake.write_text(json.dumps(payload), encoding="utf-8")
    result = CliRunner().invoke(
        app,
        [
            "compose",
            "preview",
            "--intake",
            str(intake),
            "--template",
            str(template),
            "--index",
            str(index),
            "--output",
            str(output),
            "--json",
        ],
    )
    assert result.exit_code == 0
    summary = json.loads(result.stdout)
    assert summary["retrieval_count"] == 1 and summary["retrieval_digest"]
    assert "private synthetic text" not in result.stdout and str(index) not in result.stdout


def test_retrieval_uses_at_most_two_results_per_concept(tmp_path: Path) -> None:
    index = _index(tmp_path, "alpha one", "alpha two", "alpha three")
    attached = attach_retrieval(_context_with_concepts("alpha"), index)
    assert len(attached.retrieved_sources) == 2


def test_retrieval_limit_is_a_global_cap(tmp_path: Path) -> None:
    index = _index(tmp_path, "alpha beta one", "alpha beta two", "alpha beta three")
    attached = attach_retrieval(_context_with_concepts("alpha", "beta"), index, 1)
    assert len(attached.retrieved_sources) == 1


def test_retrieval_digest_is_deterministic(tmp_path: Path) -> None:
    index = _index(tmp_path, "alpha stable")
    context = _context_with_concepts("alpha")
    assert (
        attach_retrieval(context, index).retrieval_digest
        == attach_retrieval(context, index).retrieval_digest
    )


def test_retrieval_no_concepts_warns_without_search(tmp_path: Path) -> None:
    attached = attach_retrieval(_context_with_concepts(), tmp_path / "not-opened.sqlite3")
    assert {issue.code for issue in attached.warnings} >= {"SOURCE_QUERY_UNAVAILABLE"}


def test_retrieval_zero_matches_warns(tmp_path: Path) -> None:
    index = _index(tmp_path, "alpha only")
    attached = attach_retrieval(_context_with_concepts("unmatched"), index)
    assert {issue.code for issue in attached.warnings} >= {"SOURCE_NOT_FOUND"}


def test_compose_without_index_keeps_preview_without_source_context(tmp_path: Path) -> None:
    template = tmp_path / "template.md"
    template.write_text("# Template", encoding="utf-8")
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
            "--json",
        ],
    )
    assert result.exit_code == 0
    assert "## Retrieved source context" not in output.read_text(encoding="utf-8")
    assert json.loads(result.stdout)["retrieval_count"] == 0


def test_cli_retrieval_limit_validation_has_no_traceback(tmp_path: Path) -> None:
    template = tmp_path / "template.md"
    template.write_text("# Template", encoding="utf-8")
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
            str(tmp_path / "preview.md"),
            "--retrieval-limit",
            "13",
        ],
    )
    assert result.exit_code == 2 and "Traceback" not in result.stdout
