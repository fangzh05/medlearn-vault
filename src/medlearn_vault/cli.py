import json
import os
import platform
import sys
from pathlib import Path
from typing import Annotated

import typer
from pydantic import BaseModel, ValidationError

from medlearn_vault import __version__
from medlearn_vault.bundle import ContractBundle
from medlearn_vault.capture import (
    CaptureDraft,
    CaptureProposal,
    IntakeEnvelope,
    build_capture_proposal,
    capture_proposal_digest,
    contract_bundle_digest,
    extract_capture_draft,
    render_capture_proposal_markdown,
)
from medlearn_vault.domain import (
    ChapterDossier,
    ConceptEntity,
    ConceptRelation,
    DisciplineLens,
    LearnerState,
    LearningCapture,
    MedicalClaim,
    SourceDocument,
)
from medlearn_vault.preview import (
    PreviewBuildError,
    PreviewRequest,
    build_preview_plan,
    render_markdown,
)
from medlearn_vault.workflow import (
    ApprovalOrchestrator,
    JobRecord,
    ProposalApprovalRecord,
    ProposalExecutionRecord,
    ProposalOrchestrator,
    S3ObjectStore,
    WorkflowError,
    WorkflowInputs,
)

app = typer.Typer(no_args_is_help=True, help="MedLearn Vault contract tools")
schema_app = typer.Typer(help="Export JSON schemas")
concept_app = typer.Typer(help="Validate concept entities")
bundle_app = typer.Typer(help="Validate contract bundles")
preview_app = typer.Typer(help="Render deterministic previews")
capture_app = typer.Typer(help="Validate and review capture proposals")
workflow_app = typer.Typer(help="Run cloud control-plane workflows")
app.add_typer(schema_app, name="schema")
app.add_typer(concept_app, name="concept")
app.add_typer(bundle_app, name="bundle")
app.add_typer(preview_app, name="preview")
app.add_typer(capture_app, name="capture")
app.add_typer(workflow_app, name="workflow")

SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "concept_entity": ConceptEntity,
    "concept_relation": ConceptRelation,
    "discipline_lens": DisciplineLens,
    "medical_claim": MedicalClaim,
    "source_document": SourceDocument,
    "chapter_dossier": ChapterDossier,
    "learning_capture": LearningCapture,
    "learner_state": LearnerState,
}
WORKFLOW_SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "capture_draft": CaptureDraft,
    "capture_proposal": CaptureProposal,
    "intake_envelope": IntakeEnvelope,
}
CONTROL_SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "proposal_approval": ProposalApprovalRecord,
    "job_record": JobRecord,
    "proposal_execution": ProposalExecutionRecord,
}


def version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool | None, typer.Option("--version", callback=version_callback, is_eager=True)
    ] = None,
) -> None:
    """Local-first medical knowledge contracts."""


@app.command()
def doctor() -> None:
    python_ok = sys.version_info >= (3, 12)
    typer.echo(f"medlearn: {__version__}")
    typer.echo(f"python: {platform.python_version()} ({'ok' if python_ok else 'requires >=3.12'})")
    typer.echo("contracts: ok")
    if not python_ok:
        raise typer.Exit(1)


@schema_app.command("export")
def export_schema(
    output: Annotated[Path, typer.Option("--output", "-o")] = Path("schemas/generated"),
) -> None:
    output.mkdir(parents=True, exist_ok=True)
    for name, model in SCHEMA_MODELS.items():
        path = output / f"{name}.schema.json"
        path.write_text(
            json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        typer.echo(path.as_posix())
    workflow = output.parent / "workflow" / output.name
    workflow.mkdir(parents=True, exist_ok=True)
    for name, model in WORKFLOW_SCHEMA_MODELS.items():
        path = workflow / f"{name}.schema.json"
        path.write_text(
            json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        typer.echo(path.as_posix())
    control = output.parent / "control" / output.name
    control.mkdir(parents=True, exist_ok=True)
    for name, model in CONTROL_SCHEMA_MODELS.items():
        path = control / f"{name}.schema.json"
        path.write_text(
            json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        typer.echo(path.as_posix())


@schema_app.command("check")
def check_schema(
    snapshot: Annotated[Path, typer.Option("--snapshot")] = Path("schemas/current"),
) -> None:
    mismatches: list[str] = []
    for name, model in SCHEMA_MODELS.items():
        expected = json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2) + "\n"
        path = snapshot / f"{name}.schema.json"
        if not path.exists() or path.read_text(encoding="utf-8") != expected:
            mismatches.append(path.as_posix())
    workflow = snapshot.parent / "workflow" / snapshot.name
    for name, model in WORKFLOW_SCHEMA_MODELS.items():
        expected = json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2) + "\n"
        path = workflow / f"{name}.schema.json"
        if not path.exists() or path.read_text(encoding="utf-8") != expected:
            mismatches.append(path.as_posix())
    control = snapshot.parent / "control" / snapshot.name
    for name, model in CONTROL_SCHEMA_MODELS.items():
        expected = json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2) + "\n"
        path = control / f"{name}.schema.json"
        if not path.exists() or path.read_text(encoding="utf-8") != expected:
            mismatches.append(path.as_posix())
    if mismatches:
        typer.echo("schema snapshots differ: " + ", ".join(mismatches), err=True)
        raise typer.Exit(1)
    typer.echo(
        "schema snapshots: ok "
        f"({len(SCHEMA_MODELS) + len(WORKFLOW_SCHEMA_MODELS) + len(CONTROL_SCHEMA_MODELS)})"
    )


@workflow_app.command("propose")
def workflow_propose(job_id: str, intake_object_key: str, intake_digest: str) -> None:
    try:
        inputs = WorkflowInputs(
            job_id=job_id,
            intake_object_key=intake_object_key,
            intake_digest=intake_digest,
        )
        store = S3ObjectStore(
            os.environ.get("CONTROL_R2_ENDPOINT", ""),
            os.environ.get("CONTROL_R2_ACCESS_KEY_ID", ""),
            os.environ.get("CONTROL_R2_SECRET_ACCESS_KEY", ""),
        )
        result = ProposalOrchestrator(store, Path.cwd()).run(
            inputs,
            bundle_path=os.environ.get("MEDLEARN_PROPOSE_BUNDLE_PATH", ""),
            workflow_run_id=os.environ.get("GITHUB_RUN_ID", ""),
        )
    except WorkflowError as exc:
        typer.echo(f"error_code={exc.code}", err=True)
        raise typer.Exit(1) from exc
    except ValidationError as exc:
        typer.echo("error_code=INVALID_WORKFLOW_INPUT", err=True)
        raise typer.Exit(1) from exc
    typer.echo(
        f"status={result.status} proposal_id={result.proposal_id or 'none'} "
        f"reused={str(result.reused).lower()}"
    )


@workflow_app.command("approve")
def workflow_approve(
    proposal_id: str,
    proposal_digest: str,
    expected_base_bundle_digest: str,
    decision: Annotated[str, typer.Option("--decision")] = "approved",
    source_job_id: Annotated[str | None, typer.Option("--source-job-id")] = None,
    rejection_code: Annotated[str | None, typer.Option("--rejection-code")] = None,
) -> None:
    try:
        if decision not in {"approved", "rejected"}:
            raise WorkflowError("INVALID_APPROVAL_INPUT")
        store = S3ObjectStore(
            os.environ.get("CONTROL_R2_ENDPOINT", ""),
            os.environ.get("CONTROL_R2_ACCESS_KEY_ID", ""),
            os.environ.get("CONTROL_R2_SECRET_ACCESS_KEY", ""),
        )
        result = ApprovalOrchestrator(store).run(
            proposal_id,
            proposal_digest,
            expected_base_bundle_digest,
            decision=decision,  # type: ignore[arg-type]
            source_job_id=source_job_id,
            rejection_code=rejection_code,
        )
    except WorkflowError as exc:
        typer.echo(f"error_code={exc.code}", err=True)
        raise typer.Exit(1) from exc
    except ValidationError as exc:
        typer.echo("error_code=INVALID_APPROVAL_INPUT", err=True)
        raise typer.Exit(1) from exc
    typer.echo(
        f"decision={result.decision} approval_id={result.approval_id} "
        f"reused={str(result.reused).lower()}"
    )


def _safe_error(code: str, field: str, message: str) -> None:
    typer.echo(f"{code}: {field}: {message}", err=True)


@capture_app.command("validate-draft")
def validate_capture_draft(path: Path) -> None:
    try:
        draft = CaptureDraft.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError) as exc:
        _safe_error("INVALID_CAPTURE_DRAFT", "draft", type(exc).__name__)
        raise typer.Exit(1) from exc
    typer.echo(f"draft: valid ({draft.context.session_id})")


@capture_app.command("extract-intake")
def extract_intake(path: Path, expected_intake_digest: str, output: Path) -> None:
    try:
        draft_bytes, draft_digest = extract_capture_draft(
            path.read_bytes(), expected_intake_digest
        )
        output.write_bytes(draft_bytes)
    except (OSError, ValidationError, ValueError) as exc:
        code = str(exc) if str(exc) == "INTAKE_DIGEST_MISMATCH" else "INVALID_INTAKE_ENVELOPE"
        _safe_error(code, "intake", type(exc).__name__)
        raise typer.Exit(1) from exc
    typer.echo(f"draft_digest={draft_digest}")


@capture_app.command("propose")
def propose_capture(bundle_path: Path, draft_path: Path, output: Path) -> None:
    try:
        bundle = ContractBundle.from_directory(bundle_path)
        draft = CaptureDraft.model_validate_json(draft_path.read_text(encoding="utf-8"))
        proposal = build_capture_proposal(bundle, draft)
        payload = json.dumps(proposal.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
    except (OSError, ValidationError, ValueError) as exc:
        _safe_error("INVALID_CAPTURE_INPUT", "input", type(exc).__name__)
        raise typer.Exit(1) from exc
    output.write_text(payload, encoding="utf-8")
    typer.echo(f"proposal_id={proposal.proposal_id} status={proposal.status}")
    if proposal.status == "blocked":
        raise typer.Exit(1)


@capture_app.command("review")
def review_capture(bundle_path: Path, proposal_path: Path, output: Path) -> None:
    try:
        bundle = ContractBundle.from_directory(bundle_path)
        proposal = CaptureProposal.model_validate_json(proposal_path.read_text(encoding="utf-8"))
        if capture_proposal_digest(proposal) != proposal.proposal_digest:
            _safe_error("PROPOSAL_DIGEST_MISMATCH", "proposal_digest", "proposal was modified")
            raise typer.Exit(1)
        if contract_bundle_digest(bundle) != proposal.base_bundle_digest:
            _safe_error("STALE_BASE_BUNDLE", "base_bundle_digest", "bundle changed after proposal")
            raise typer.Exit(1)
        markdown = render_capture_proposal_markdown(proposal, bundle=bundle)
    except typer.Exit:
        raise
    except (OSError, ValidationError, ValueError) as exc:
        _safe_error("INVALID_CAPTURE_PROPOSAL", "proposal", type(exc).__name__)
        raise typer.Exit(1) from exc
    output.write_text(markdown, encoding="utf-8")
    typer.echo(output.as_posix())


@concept_app.command("validate")
def validate_concept(path: Path) -> None:
    try:
        concept = ConceptEntity.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError) as exc:
        typer.echo(f"invalid: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"valid: {concept.concept_id}")


@bundle_app.command("validate")
def validate_bundle(path: Path) -> None:
    try:
        bundle = ContractBundle.from_directory(path)
    except (OSError, ValueError, ValidationError) as exc:
        typer.echo(f"invalid: {exc}", err=True)
        raise typer.Exit(1) from exc
    issues = bundle.validate_integrity()
    for issue in issues:
        typer.echo(issue.model_dump_json(), err=True)
    if any(issue.severity == "error" for issue in issues):
        raise typer.Exit(1)
    typer.echo(f"bundle: valid ({sum(issue.severity == 'warning' for issue in issues)} warning(s))")


@preview_app.command("render")
def render_preview(path: Path, output: Path, topic: str = typer.Option(..., "--topic")) -> None:
    try:
        markdown = render_markdown(
            build_preview_plan(ContractBundle.from_directory(path), PreviewRequest(topic=topic))
        )
    except (OSError, ValueError, ValidationError, PreviewBuildError) as exc:
        code = getattr(exc, "code", "INVALID_PREVIEW")
        typer.echo(f"{code}: {exc}", err=True)
        raise typer.Exit(1) from exc
    output.write_text(markdown, encoding="utf-8")
    typer.echo(output.as_posix())


if __name__ == "__main__":
    app()
