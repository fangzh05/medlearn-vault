import hashlib
import json
import os
import platform
import sys
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from pydantic import BaseModel, ValidationError

from medlearn_vault import __version__
from medlearn_vault.bundle import ContractBundle
from medlearn_vault.capture import (
    CaptureDraft,
    CaptureProposal,
    IntakeEnvelope,
    backfill_learning_capture,
    build_capture_proposal,
    capture_proposal_digest,
    contract_bundle_digest,
    exact_capture_proposal_json,
    extract_capture_draft,
    render_capture_proposal_markdown,
)
from medlearn_vault.catalog_update import (
    CatalogUpdateProposal,
    ReviewedMetadataEntry,
    build_catalog_update_proposal,
    bundle_path_identity,
    canonical_catalog_update_json,
    complete_catalog_update_metadata,
    prepare_catalog_patch,
    render_catalog_update_markdown,
    write_catalog_patch,
)
from medlearn_vault.composition import build_context, compose_preview, validate_composition
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
from medlearn_vault.handoff import LearningSegment, MedLearnHandoff
from medlearn_vault.identifiers import normalize_text
from medlearn_vault.normalization import NormalizationError, normalize_one
from medlearn_vault.presentation_publisher import (
    PresentationArtifact,
    PresentationCurrentPointer,
    PresentationGenerationReceipt,
)
from medlearn_vault.preview import (
    PreviewBuildError,
    PreviewRequest,
    build_preview_plan,
    render_markdown,
)
from medlearn_vault.publication import (
    VaultPublicationPlan,
    VaultPublicationReceipt,
    canonical_learning_capture_json,
    capture_identity,
    render_learning_capture_markdown,
)
from medlearn_vault.source_pdf import ExtractionResult, PdfExtractionError, extract_input
from medlearn_vault.sync_client import (
    configure as sync_configure_service,
)
from medlearn_vault.sync_client import (
    login as sync_login_service,
)
from medlearn_vault.sync_client import (
    logout as sync_logout_service,
)
from medlearn_vault.sync_client import (
    pull as sync_pull_service,
)
from medlearn_vault.sync_client import scheduled_pull as scheduled_pull_service
from medlearn_vault.sync_client import (
    status as sync_status_service,
)
from medlearn_vault.sync_models import Manifest, SyncError, SyncState
from medlearn_vault.windows_rollout import (
    install_schedule,
    install_windows,
    remove_schedule,
)
from medlearn_vault.windows_rollout import (
    schedule_status as schedule_status_service,
)
from medlearn_vault.workflow import (
    ApprovalAttestor,
    ApprovalOrchestrator,
    AutoPublicationOrchestrator,
    JobRecord,
    ProposalApprovalRecord,
    ProposalExecutionRecord,
    ProposalOrchestrator,
    ProposalOutputInspector,
    PublicationPlanOrchestrator,
    ReproposalOrchestrator,
    S3ObjectStore,
    S3ReadOnlyObjectStore,
    WorkflowError,
    WorkflowInputs,
    trace_source_job,
)

app = typer.Typer(no_args_is_help=True, help="MedLearn Vault contract tools")
schema_app = typer.Typer(help="Export JSON schemas")
concept_app = typer.Typer(help="Validate concept entities")
bundle_app = typer.Typer(help="Validate contract bundles")
preview_app = typer.Typer(help="Render deterministic previews")
capture_app = typer.Typer(help="Validate and review capture proposals")
composition_app = typer.Typer(help="Create local-only note-composition previews")
catalog_app = typer.Typer(help="Prepare manually reviewed catalog patches")
workflow_app = typer.Typer(help="Run cloud control-plane workflows")
sync_app = typer.Typer(help="Synchronize published artifacts to a local Obsidian Vault")
sync_schedule_app = typer.Typer(help="Manage the optional Windows Scheduled Task")
sources_app = typer.Typer(help="Extract private local source PDFs without OCR")
app.add_typer(schema_app, name="schema")
app.add_typer(concept_app, name="concept")
app.add_typer(bundle_app, name="bundle")
app.add_typer(preview_app, name="preview")
app.add_typer(capture_app, name="capture")
app.add_typer(composition_app, name="compose")
app.add_typer(catalog_app, name="catalog")
app.add_typer(workflow_app, name="workflow")
app.add_typer(sync_app, name="sync")
app.add_typer(sources_app, name="sources")
sync_app.add_typer(sync_schedule_app, name="schedule")

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
    "medlearn_handoff": MedLearnHandoff,
    "learning_segment": LearningSegment,
    "catalog_update_proposal": CatalogUpdateProposal,
}
CONTROL_SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "proposal_approval": ProposalApprovalRecord,
    "job_record": JobRecord,
    "proposal_execution": ProposalExecutionRecord,
    "vault_publication_plan": VaultPublicationPlan,
    "vault_publication_receipt": VaultPublicationReceipt,
    "presentation_artifact": PresentationArtifact,
    "presentation_generation_receipt": PresentationGenerationReceipt,
    "presentation_current_pointer": PresentationCurrentPointer,
    "manifest_0_2": Manifest,
    "sync_state_0_2": SyncState,
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


def _sync_output(value: dict[str, object], json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    else:
        typer.echo(
            " ".join(
                f"{key}={str(item).lower() if isinstance(item, bool) else item}"
                for key, item in value.items()
            )
        )


def _sync_error(exc: SyncError, json_output: bool) -> NoReturn:
    _sync_output({"status": "error", "error_code": exc.code}, json_output)
    raise typer.Exit(3 if exc.code == "SYNC_LOCAL_CONFLICT" else 1) from exc


@sources_app.command("normalize")
def normalize_command(
    input_root: Annotated[Path, typer.Option("--input-root")],
    output_root: Annotated[Path, typer.Option("--output-root")],
    exclusions: Annotated[Path | None, typer.Option("--exclusions")] = None,
    force: Annotated[bool, typer.Option("--force")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    try:
        ex = json.loads(exclusions.read_text(encoding="utf-8")) if exclusions else {"sources": []}
        mapping = {
            x["source_relative_path"]: x["excluded_pdf_pages"] for x in ex.get("sources", [])
        }
        dirs = sorted(
            {
                p.parent
                for p in input_root.rglob("pages.jsonl")
                if (p.parent / "report.json").exists()
            }
        )
        if not dirs:
            raise NormalizationError("NORMALIZATION_INPUT_INVALID")
        files = []
        failed = 0
        for src in dirs:
            try:
                files.append(
                    normalize_one(src, output_root / src.relative_to(input_root), mapping, force)
                )
            except NormalizationError as exc:
                failed += 1
                files.append(
                    {
                        "source_relative_path": src.relative_to(input_root).as_posix(),
                        "error_code": exc.code,
                    }
                )
        payload = {
            "discovered_count": len(dirs),
            "succeeded_count": len(dirs) - failed,
            "warning_count": sum(bool(x.get("warning_codes")) for x in files),
            "failed_count": failed,
            "sources": files,
        }
        typer.echo(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if json_output
            else " ".join(f"{k}={v}" for k, v in payload.items() if k != "sources")
        )
        if failed:
            raise typer.Exit(1)
    except NormalizationError as exc:
        typer.echo(
            json.dumps({"error_code": exc.code}, separators=(",", ":"))
            if json_output
            else f"error_code={exc.code}",
            err=not json_output,
        )
        raise typer.Exit(1) from exc


@sources_app.command("extract-pdf")
def extract_pdf_command(
    input_path: Annotated[Path, typer.Option("--input")],
    output_root: Annotated[Path, typer.Option("--output-root")],
    force: Annotated[bool, typer.Option("--force")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Extract local native PDF text to page JSONL, inspection TXT, and report JSON."""
    try:
        results = extract_input(input_path, output_root, force)
    except PdfExtractionError as exc:
        value = {"error_code": exc.code}
        typer.echo(
            json.dumps(value, separators=(",", ":")) if json_output else f"error_code={exc.code}",
            err=not json_output,
        )
        raise typer.Exit(1) from exc
    files: list[dict[str, object]] = []
    failed = warnings = 0
    keys = (
        "source_relative_path",
        "total_pages",
        "pages_with_text",
        "empty_pages",
        "low_text_pages",
        "total_characters",
        "extraction_status",
        "warning_codes",
    )
    for result in results:
        if isinstance(result, ExtractionResult):
            if result.report["extraction_status"] == "success_with_warnings":
                warnings += 1
            files.append({key: result.report[key] for key in keys})
        else:
            failed += 1
            files.append(dict(result))
    payload = {
        "discovered_count": len(results),
        "succeeded_count": len(results) - failed,
        "warning_count": warnings,
        "failed_count": failed,
        "files": files,
    }
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    else:
        typer.echo(" ".join(f"{key}={value}" for key, value in payload.items() if key != "files"))
        for item in files:
            typer.echo(" ".join(f"{key}={value}" for key, value in item.items()))
    if failed:
        raise typer.Exit(1)


@composition_app.command("preview")
def compose_preview_command(
    intake: Annotated[Path, typer.Option("--intake")],
    template: Annotated[Path, typer.Option("--template")],
    output: Annotated[Path, typer.Option("--output")],
    current_note: Annotated[Path | None, typer.Option("--current-note")] = None,
    source_job_id: Annotated[str | None, typer.Option("--source-job-id")] = None,
    expected_intake_digest: Annotated[str | None, typer.Option("--expected-intake-digest")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Write a deterministic local preview; this never writes Vault or R2."""
    try:
        context = build_context(
            intake.read_bytes(),
            template=template.read_text(encoding="utf-8"),
            current_note=(current_note.read_text(encoding="utf-8") if current_note else None),
            source_job_id=source_job_id,
            expected_intake_digest=expected_intake_digest,
        )
        result = compose_preview(context)
        output.write_text(result.markdown, encoding="utf-8", newline="\n")
    except (OSError, ValueError) as exc:
        code = "COMPOSITION_OUTPUT_WRITE_FAILED" if isinstance(exc, OSError) else str(exc)
        _sync_output({"status": "rejected", "error_code": code}, json_output)
        raise typer.Exit(1) from exc
    validation = validate_composition(context)
    payload: dict[str, object] = {
        "status": validation.status,
        "source_record_id": context.source_record_id,
        "target_path": result.target_path,
        "warning_count": len(result.warnings),
        "isolated_count": len(result.isolated_items),
    }
    if context.source_job_id is not None:
        payload["source_job_id"] = context.source_job_id
    if json_output:
        payload["warning_codes"] = [item.code for item in result.warnings]
        payload["blocker_codes"] = []
    _sync_output(payload, json_output)


@sync_app.command("configure")
def sync_configure(
    endpoint: Annotated[str, typer.Option("--endpoint")],
    vault: Annotated[Path, typer.Option("--vault")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    try:
        config = sync_configure_service(endpoint, vault)
    except SyncError as exc:
        _sync_error(exc, json_output)
    _sync_output(
        {"status": "configured", "endpoint": config.endpoint, "vault": config.vault_path},
        json_output,
    )


@sync_app.command("login")
def sync_login() -> None:
    token = os.environ.get("MEDLEARN_SYNC_TOKEN")
    if token is None:
        token = typer.prompt("Sync token", hide_input=True)
    try:
        sync_login_service(token)
    except SyncError as exc:
        _sync_error(exc, False)
    typer.echo("status=authenticated credential=windows_dpapi")


@sync_app.command("logout")
def sync_logout() -> None:
    try:
        sync_logout_service()
    except SyncError as exc:
        _sync_error(exc, False)
    typer.echo("status=logged_out")


@sync_app.command("status")
def sync_status(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    try:
        _sync_output(sync_status_service(), json_output)
    except SyncError as exc:
        _sync_error(exc, json_output)


@sync_app.command("pull")
def sync_pull(
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    confirm_first_pull: Annotated[bool, typer.Option("--confirm-first-pull")] = False,
    scheduled: Annotated[bool, typer.Option("--scheduled", hidden=True)] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    timeout: Annotated[float, typer.Option("--timeout")] = 30,
) -> None:
    try:
        result = (
            scheduled_pull_service(timeout=timeout)
            if scheduled
            else sync_pull_service(
                dry_run=dry_run, confirm_first_pull=confirm_first_pull, timeout=timeout
            )
        )
    except SyncError as exc:
        _sync_error(exc, json_output)
    _sync_output(result, json_output)
    if result["conflict_count"]:
        raise typer.Exit(3)


@sync_app.command("install-windows")
def sync_install_windows(
    wheel: Annotated[Path, typer.Option("--wheel")],
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    try:
        _sync_output(install_windows(wheel, dry_run=dry_run), json_output)
    except SyncError as exc:
        _sync_error(exc, json_output)


@sync_schedule_app.command("install")
def sync_schedule_install(
    interval_minutes: Annotated[int, typer.Option("--interval-minutes")] = 15,
    what_if: Annotated[bool, typer.Option("--what-if")] = False,
    elevated: Annotated[bool, typer.Option("--elevated")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    try:
        _sync_output(
            install_schedule(interval_minutes=interval_minutes, what_if=what_if, elevated=elevated),
            json_output,
        )
    except SyncError as exc:
        _sync_error(exc, json_output)


@sync_schedule_app.command("status")
def sync_schedule_status(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    try:
        _sync_output(schedule_status_service(), json_output)
    except SyncError as exc:
        _sync_error(exc, json_output)


@sync_schedule_app.command("remove")
def sync_schedule_remove(
    elevated: Annotated[bool, typer.Option("--elevated")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    try:
        _sync_output(remove_schedule(elevated=elevated), json_output)
    except SyncError as exc:
        _sync_error(exc, json_output)


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


@workflow_app.command("repropose")
def workflow_repropose(
    source_job_id: str,
    blocked_proposal_id: str,
    catalog_update_id: str,
    previous_base_bundle_digest: str,
    confirmation: str,
) -> None:
    """Create a new immutable Job/Proposal from the same Intake against the current catalog.

    This is the only path from a blocked bootstrap Proposal to a ready
    Proposal after catalog merge.  It never mutates the terminal blocked Job.
    """
    try:
        store = S3ObjectStore(
            os.environ.get("CONTROL_R2_ENDPOINT", ""),
            os.environ.get("CONTROL_R2_ACCESS_KEY_ID", ""),
            os.environ.get("CONTROL_R2_SECRET_ACCESS_KEY", ""),
        )
        result = ReproposalOrchestrator(store, Path.cwd()).run(
            source_job_id,
            blocked_proposal_id,
            catalog_update_id,
            previous_base_bundle_digest,
            confirmation,
            bundle_path=os.environ.get("MEDLEARN_PROPOSE_BUNDLE_PATH", ""),
        )
    except WorkflowError as exc:
        typer.echo(f"error_code={exc.code}", err=True)
        raise typer.Exit(1) from exc
    except ValidationError as exc:
        typer.echo("error_code=INVALID_REPROPOSAL_INPUT", err=True)
        raise typer.Exit(1) from exc
    typer.echo(
        f"source_job_id={result.source_job_id} proposal_id={result.proposal_id} "
        f"base_bundle_digest={result.base_bundle_digest} "
        f"reused={str(result.reused).lower()}"
    )


@workflow_app.command("approve")
def workflow_approve(
    proposal_id: str,
    proposal_object_digest: str,
    expected_base_bundle_digest: str,
    decision: Annotated[str, typer.Option("--decision")] = "approved",
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
            proposal_object_digest,
            expected_base_bundle_digest,
            decision=decision,  # type: ignore[arg-type]
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


@workflow_app.command("verify-approval")
def workflow_verify_approval(
    approval_id: str,
    source_job_id: str,
    proposal_id: str,
    expected_proposal_object_digest: str,
    expected_base_bundle_digest: str,
    expected_decision: Annotated[str, typer.Option("--expected-decision")] = "approved",
    expected_rejection_code: Annotated[
        str | None, typer.Option("--expected-rejection-code")
    ] = None,
    expected_approval_object_digest: Annotated[
        str | None, typer.Option("--expected-approval-object-digest")
    ] = None,
) -> None:
    try:
        store = S3ReadOnlyObjectStore(
            os.environ.get("CONTROL_R2_ENDPOINT", ""),
            os.environ.get("CONTROL_R2_ACCESS_KEY_ID", ""),
            os.environ.get("CONTROL_R2_SECRET_ACCESS_KEY", ""),
        )
        result = ApprovalAttestor(store).run(
            approval_id,
            source_job_id,
            proposal_id,
            expected_proposal_object_digest,
            expected_base_bundle_digest,
            expected_decision=expected_decision,
            expected_rejection_code=expected_rejection_code,
            expected_approval_object_digest=expected_approval_object_digest,
        )
    except WorkflowError as exc:
        typer.echo(f"error_code={exc.code}", err=True)
        raise typer.Exit(1) from exc
    except ValidationError as exc:
        typer.echo("error_code=INVALID_ATTESTATION_INPUT", err=True)
        raise typer.Exit(1) from exc
    typer.echo(
        f"status=verified approval_id={result.approval_id} "
        f"approval_object_digest={result.approval_object_digest} "
        f"proposal_id={result.proposal_id} "
        f"proposal_object_digest={result.proposal_object_digest} "
        f"review_digest={result.review_digest} decision={result.decision} "
        f"source_job_id={result.source_job_id} workflow_run_id={result.workflow_run_id}"
    )


@workflow_app.command("plan-publication")
def workflow_plan_publication(
    approval_id: str,
    approval_object_digest: str,
    source_job_id: str,
    proposal_id: str,
    proposal_object_digest: str,
    expected_base_bundle_digest: str,
) -> None:
    try:
        store = S3ObjectStore(
            os.environ.get("CONTROL_R2_ENDPOINT", ""),
            os.environ.get("CONTROL_R2_ACCESS_KEY_ID", ""),
            os.environ.get("CONTROL_R2_SECRET_ACCESS_KEY", ""),
        )
        result = PublicationPlanOrchestrator(store, Path.cwd()).run(
            approval_id,
            approval_object_digest,
            source_job_id,
            proposal_id,
            proposal_object_digest,
            expected_base_bundle_digest,
            bundle_path=os.environ.get("MEDLEARN_PROPOSE_BUNDLE_PATH", ""),
        )
    except WorkflowError as exc:
        typer.echo(f"error_code={exc.code}", err=True)
        raise typer.Exit(1) from exc
    except ValidationError as exc:
        typer.echo("error_code=INVALID_PUBLICATION_INPUT", err=True)
        raise typer.Exit(1) from exc
    typer.echo(
        f"status=planned publication_plan_id={result.publication_plan_id} "
        f"publication_plan_object_digest={result.publication_plan_object_digest} "
        f"capture_id={result.capture_id} capture_object_digest={result.capture_object_digest} "
        f"markdown_digest={result.markdown_digest} reused={str(result.reused).lower()}"
    )


@workflow_app.command("inspect-proposal")
def workflow_inspect_proposal(source_job_id: str) -> None:
    try:
        store = S3ReadOnlyObjectStore(
            os.environ.get("CONTROL_R2_ENDPOINT", ""),
            os.environ.get("CONTROL_R2_ACCESS_KEY_ID", ""),
            os.environ.get("CONTROL_R2_SECRET_ACCESS_KEY", ""),
        )
        result = ProposalOutputInspector(store).run(source_job_id)
    except WorkflowError as exc:
        typer.echo(f"error_code={exc.code}", err=True)
        raise typer.Exit(1) from exc
    except ValidationError as exc:
        typer.echo("error_code=INVALID_ATTESTATION_INPUT", err=True)
        raise typer.Exit(1) from exc
    typer.echo(
        f"status=verified source_job_id={result.source_job_id} "
        f"proposal_id={result.proposal_id} "
        f"proposal_object_digest={result.proposal_object_digest} "
        f"proposal_semantic_digest={result.proposal_semantic_digest} "
        f"expected_base_bundle_digest={result.expected_base_bundle_digest} "
        f"review_digest={result.review_digest} workflow_run_id={result.workflow_run_id}"
    )


@workflow_app.command("trace-job")
def workflow_trace_job(
    source_job_id: str,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Read the exact Job → Intake → Proposal → publication chain."""
    try:
        store = S3ReadOnlyObjectStore(
            os.environ.get("CONTROL_R2_ENDPOINT", ""),
            os.environ.get("CONTROL_R2_ACCESS_KEY_ID", ""),
            os.environ.get("CONTROL_R2_SECRET_ACCESS_KEY", ""),
        )
        result = trace_source_job(store, source_job_id)
    except WorkflowError as exc:
        if json_output:
            typer.echo(json.dumps({"error_code": exc.code}, separators=(",", ":")))
        else:
            typer.echo(f"error_code={exc.code}", err=True)
        raise typer.Exit(1) from exc
    payload = result.model_dump()
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    else:
        typer.echo(" ".join(f"{key}={value}" for key, value in payload.items()))


@workflow_app.command("export-proposal")
def workflow_export_proposal(
    source_job_id: str,
    expected_proposal_id: str,
    output: Annotated[Path, typer.Option("--output")],
) -> None:
    """Read-only export of one terminal Proposal and Review for catalog bootstrap."""
    try:
        store = S3ReadOnlyObjectStore(
            os.environ.get("CONTROL_R2_ENDPOINT", ""),
            os.environ.get("CONTROL_R2_ACCESS_KEY_ID", ""),
            os.environ.get("CONTROL_R2_SECRET_ACCESS_KEY", ""),
        )
        job_key = f"v1/jobs/{source_job_id}.json"
        job_stored = store.get(job_key)
        if job_stored is None:
            raise WorkflowError("JOB_NOT_FOUND")
        job = JobRecord.model_validate_json(job_stored.body)
        if (
            job.job_id != source_job_id
            or job.status not in {"succeeded", "blocked"}
            or job.proposal_id != expected_proposal_id
            or job.workflow_run_id is None
        ):
            raise WorkflowError("INVALID_JOB")

        execution_key = f"v1/executions/{source_job_id}.json"
        execution_stored = store.get(execution_key)
        if execution_stored is None:
            raise WorkflowError("EXECUTION_NOT_FOUND")
        execution = ProposalExecutionRecord.model_validate_json(execution_stored.body)
        if (
            execution.job_id != source_job_id
            or execution.status != job.status
            or execution.proposal_id != expected_proposal_id
            or execution.workflow_run_id != job.workflow_run_id
            or execution.proposal_digest is None
            or execution.review_digest is None
        ):
            raise WorkflowError("INVALID_EXECUTION")

        proposal_key = f"v1/proposals/{expected_proposal_id}.json"
        proposal_stored = store.get(proposal_key)
        if proposal_stored is None:
            raise WorkflowError("PROPOSAL_NOT_FOUND")
        proposal = CaptureProposal.model_validate_json(proposal_stored.body)
        proposal_object_digest = "sha256:" + hashlib.sha256(proposal_stored.body).hexdigest()
        if (
            proposal.proposal_id != expected_proposal_id
            or capture_proposal_digest(proposal) != proposal.proposal_digest
            or proposal_object_digest != execution.proposal_digest
        ):
            raise WorkflowError("INVALID_PROPOSAL")

        review_key = f"v1/reviews/{expected_proposal_id}.md"
        review_stored = store.get(review_key)
        if review_stored is None:
            raise WorkflowError("REVIEW_NOT_FOUND")
        review_digest = "sha256:" + hashlib.sha256(review_stored.body).hexdigest()
        if review_digest != execution.review_digest:
            raise WorkflowError("REVIEW_DIGEST_MISMATCH")

        output.mkdir(parents=True, exist_ok=True)
        proposal_path = output / "proposal.json"
        review_path = output / "review.md"
        details_path = output / "inspect.json"
        draft_diagnostics_path = output / "draft-diagnostics.json"
        proposal_path.write_bytes(proposal_stored.body)
        review_path.write_bytes(review_stored.body)
        non_resolved = [
            item.model_dump(mode="json")
            for item in proposal.concept_resolutions
            if item.status not in {"matched", "redirected"}
        ]
        details = {
            "source_job_id": source_job_id,
            "proposal_id": expected_proposal_id,
            "job_status": job.status,
            "proposal_status": proposal.status,
            "source_candidate_present": proposal.source_candidate is not None,
            "source_candidate": (
                proposal.source_candidate.model_dump(mode="json")
                if proposal.source_candidate is not None
                else None
            ),
            "new_concept_candidate_count": len(proposal.new_concept_candidates),
            "new_concept_candidates": [
                item.model_dump(mode="json") for item in proposal.new_concept_candidates
            ],
            "non_matched_or_redirected_concept_resolutions": non_resolved,
            "previous_base_bundle_digest": proposal.base_bundle_digest,
            "proposal_object_digest": proposal_object_digest,
            "proposal_semantic_digest": proposal.proposal_digest,
            "review_digest": review_digest,
            "issues": [item.model_dump(mode="json") for item in proposal.issues],
            "proposal_path": proposal_path.as_posix(),
            "review_path": review_path.as_posix(),
        }
        details_path.write_text(
            json.dumps(details, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        intake_stored = store.get(job.intake_object_key)
        if intake_stored is None:
            raise WorkflowError("INTAKE_NOT_FOUND")
        intake_digest = "sha256:" + hashlib.sha256(intake_stored.body).hexdigest()
        if intake_digest != job.intake_digest:
            raise WorkflowError("INTAKE_DIGEST_MISMATCH")
        draft_bytes, draft_digest = extract_capture_draft(intake_stored.body, intake_digest)
        draft = CaptureDraft.model_validate_json(draft_bytes)
        resolution_by_term = {
            normalize_text(item.surface_text): item for item in proposal.concept_resolutions
        }
        learner_evidence = []
        for index, item in enumerate(draft.learner_evidence_candidates):
            term_refs = []
            for term in item.concept_terms:
                resolution = resolution_by_term.get(normalize_text(term))
                term_refs.append(
                    {
                        "term": term,
                        "resolution_status": None if resolution is None else resolution.status,
                        "matched_concept_id": (
                            None if resolution is None else resolution.matched_concept_id
                        ),
                        "candidate_concept_ids": (
                            [] if resolution is None else list(resolution.candidate_concept_ids)
                        ),
                        "new_candidate_id": (
                            None if resolution is None else resolution.new_candidate_id
                        ),
                    }
                )
            unique_concept_ids = sorted(
                {
                    ref["matched_concept_id"]
                    for ref in term_refs
                    if ref["matched_concept_id"] is not None
                }
            )
            learner_evidence.append(
                {
                    "index": index,
                    "concept_terms": list(item.concept_terms),
                    "evidence_type": item.evidence_type,
                    "confidence": item.confidence,
                    "evidence_message_ids": list(item.evidence_message_ids),
                    "term_refs": term_refs,
                    "unique_matched_concept_ids": unique_concept_ids,
                    "valid_single_persistent_concept": (
                        len(unique_concept_ids) == 1
                        and len(term_refs) >= 1
                        and all(ref["matched_concept_id"] is not None for ref in term_refs)
                    ),
                }
            )
        draft_diagnostics = {
            "source_job_id": source_job_id,
            "proposal_id": expected_proposal_id,
            "intake_digest": intake_digest,
            "draft_digest": draft_digest,
            "learner_evidence_candidates": learner_evidence,
        }
        draft_diagnostics_path.write_text(
            json.dumps(draft_diagnostics, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except WorkflowError as exc:
        typer.echo(f"error_code={exc.code}", err=True)
        raise typer.Exit(1) from exc
    except ValidationError as exc:
        typer.echo("error_code=INVALID_PROPOSAL_EXPORT_INPUT", err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(details, ensure_ascii=False, separators=(",", ":")))


@workflow_app.command("auto-publish")
def workflow_auto_publish(
    source_job_id: str,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Approve and publish one eligible completed Job using only its Job ID."""
    from medlearn_vault.vault_writer import S3VaultObjectStore

    try:
        control_store = S3ObjectStore(
            os.environ.get("CONTROL_R2_ENDPOINT", ""),
            os.environ.get("CONTROL_R2_ACCESS_KEY_ID", ""),
            os.environ.get("CONTROL_R2_SECRET_ACCESS_KEY", ""),
        )
        vault_store = S3VaultObjectStore(
            os.environ.get("VAULT_R2_ENDPOINT", ""),
            os.environ.get("VAULT_R2_ACCESS_KEY_ID", ""),
            os.environ.get("VAULT_R2_SECRET_ACCESS_KEY", ""),
        )
        result = AutoPublicationOrchestrator(control_store, vault_store, Path.cwd()).run(
            source_job_id,
            bundle_path=os.environ.get("MEDLEARN_PROPOSE_BUNDLE_PATH", ""),
        )
    except WorkflowError as exc:
        if json_output:
            typer.echo(json.dumps({"error_code": exc.code}, separators=(",", ":")))
        else:
            typer.echo(f"error_code={exc.code}", err=True)
        raise typer.Exit(1) from exc
    except ValidationError as exc:
        if json_output:
            typer.echo('{"error_code":"INVALID_AUTO_PUBLICATION_INPUT"}')
        else:
            typer.echo("error_code=INVALID_AUTO_PUBLICATION_INPUT", err=True)
        raise typer.Exit(1) from exc
    payload = result.model_dump(exclude_none=True)
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    else:
        typer.echo(" ".join(f"{key}={value}" for key, value in payload.items()))


@workflow_app.command("publish-vault")
def workflow_publish_vault(
    publication_plan_id: str,
    publication_plan_object_digest: str,
    source_job_id: str,
) -> None:
    """Publish exact planned artifact bytes to medlearn-vault (create-only)."""
    from medlearn_vault.vault_writer import S3VaultObjectStore, VaultPublicationWriter

    try:
        control_store = S3ReadOnlyObjectStore(
            os.environ.get("CONTROL_R2_ENDPOINT", ""),
            os.environ.get("CONTROL_R2_ACCESS_KEY_ID", ""),
            os.environ.get("CONTROL_R2_SECRET_ACCESS_KEY", ""),
        )
        vault_store = S3VaultObjectStore(
            os.environ.get("VAULT_R2_ENDPOINT", ""),
            os.environ.get("VAULT_R2_ACCESS_KEY_ID", ""),
            os.environ.get("VAULT_R2_SECRET_ACCESS_KEY", ""),
        )
        result = VaultPublicationWriter(control_store, vault_store).run(
            publication_plan_id,
            publication_plan_object_digest,
            source_job_id,
        )
    except WorkflowError as exc:
        typer.echo(f"error_code={exc.code}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(
        f"status=published "
        f"publication_plan_id={result.publication_plan_id} "
        f"publication_plan_object_digest={result.publication_plan_object_digest} "
        f"capture_id={result.capture_id} "
        f"created_count={len(result.created_paths)} "
        f"reused_count={len(result.reused_paths)} "
        f"receipt_status={result.receipt_status}"
    )


@workflow_app.command("publish-presentation")
def workflow_publish_presentation(bundle_path: str) -> None:
    """Build one complete reader-facing snapshot and CAS-activate it."""
    from medlearn_vault.presentation_publisher import PresentationPublisher, S3PresentationStore

    try:
        result = PresentationPublisher(
            S3PresentationStore(
                os.environ.get("VAULT_R2_ENDPOINT", ""),
                os.environ.get("VAULT_R2_ACCESS_KEY_ID", ""),
                os.environ.get("VAULT_R2_SECRET_ACCESS_KEY", ""),
            ),
            Path.cwd(),
        ).run(bundle_path)
    except WorkflowError as exc:
        typer.echo(f"error_code={exc.code}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(
        f"status=published presentation_generation_id={result.presentation_generation_id} "
        f"artifact_count={len(result.artifacts)}"
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
        draft_bytes, draft_digest = extract_capture_draft(path.read_bytes(), expected_intake_digest)
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
    except (OSError, ValidationError, ValueError) as exc:
        _safe_error("INVALID_CAPTURE_INPUT", "input", type(exc).__name__)
        raise typer.Exit(1) from exc
    output.write_bytes(exact_capture_proposal_json(proposal))
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


@capture_app.command("catalog-update")
def catalog_update_capture(
    proposal_path: Path,
    json_output: Path,
    review_output: Path,
    bundle_path: Annotated[Path, typer.Option("--bundle")],
) -> None:
    """Create review-only repository-patch contents; this command never writes a bundle or R2."""
    try:
        exact_proposal_bytes = proposal_path.read_bytes()
        proposal = CaptureProposal.model_validate_json(exact_proposal_bytes)
        if capture_proposal_digest(proposal) != proposal.proposal_digest:
            _safe_error("PROPOSAL_DIGEST_MISMATCH", "proposal_digest", "proposal was modified")
            raise typer.Exit(1)
        bundle = ContractBundle.from_directory(bundle_path)
        if contract_bundle_digest(bundle) != proposal.base_bundle_digest:
            _safe_error("STALE_BASE_BUNDLE", "base_bundle_digest", "bundle changed after proposal")
            raise typer.Exit(1)
        update = build_catalog_update_proposal(
            proposal,
            capture_proposal_object_digest=(
                "sha256:" + hashlib.sha256(exact_proposal_bytes).hexdigest()
            ),
            target_bundle_path=bundle_path_identity(bundle_path),
        )
    except typer.Exit:
        raise
    except (OSError, ValidationError, ValueError) as exc:
        _safe_error("INVALID_CAPTURE_PROPOSAL", "proposal", type(exc).__name__)
        raise typer.Exit(1) from exc
    json_output.write_bytes(canonical_catalog_update_json(update))
    review_output.write_text(render_catalog_update_markdown(update), encoding="utf-8")
    typer.echo(f"catalog_update_id={update.catalog_update_id} status={update.status}")
    if update.status == "blocked":
        raise typer.Exit(1)


@capture_app.command("render-view")
def capture_render_view(
    capture_path: Path,
    output: Path,
    bundle_path: Annotated[Path, typer.Option("--bundle")],
) -> None:
    """Create a versioned local presentation without changing immutable Capture JSON."""
    try:
        capture = LearningCapture.model_validate_json(capture_path.read_bytes())
        bundle = ContractBundle.from_directory(bundle_path)
        capture_id = capture_path.stem
        if not capture_id.startswith("capture_"):
            raise ValueError("INVALID_CAPTURE_ID")
        markdown = render_learning_capture_markdown(
            bundle,
            capture,
            capture_id=capture_id,
            approval_id="derived_view",
            proposal_id="derived_view",
        )
        markdown = markdown.replace(
            'medlearn_type: "learning_capture"',
            'medlearn_type: "learning_capture_view"\nview_kind: "derived"',
        )
    except (OSError, ValidationError, ValueError) as exc:
        _safe_error("INVALID_CAPTURE_VIEW_INPUT", "capture", type(exc).__name__)
        raise typer.Exit(1) from exc
    if output.resolve() == capture_path.resolve():
        _safe_error("INVALID_CAPTURE_VIEW_OUTPUT", "output", "cannot overwrite canonical JSON")
        raise typer.Exit(1)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(markdown, encoding="utf-8", newline="\n")
    typer.echo(output.as_posix())


@capture_app.command("backfill-capture")
def capture_backfill(
    proposal_path: Path,
    output: Path,
    bundle_path: Annotated[Path, typer.Option("--bundle")],
) -> None:
    """Create a new derived Capture from immutable proposal statements; never overwrite input."""
    try:
        if output.resolve() == proposal_path.resolve():
            raise ValueError("INVALID_BACKFILL_OUTPUT")
        proposal = CaptureProposal.model_validate_json(proposal_path.read_bytes())
        bundle = ContractBundle.from_directory(bundle_path)
        capture = backfill_learning_capture(bundle, proposal)
    except (OSError, ValidationError, ValueError) as exc:
        _safe_error("INVALID_CAPTURE_BACKFILL_INPUT", "proposal", type(exc).__name__)
        raise typer.Exit(1) from exc
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(canonical_learning_capture_json(capture))
    typer.echo(f"capture_id={capture_identity(capture)} output={output.as_posix()}")


@catalog_app.command("prepare-patch")
def prepare_catalog_patch_command(
    catalog_update_path: Path,
    bundle_path: Annotated[Path, typer.Option("--bundle")],
    output: Annotated[Path, typer.Option("--output")],
) -> None:
    """Create a new review directory without modifying the catalog bundle in place."""
    try:
        update = CatalogUpdateProposal.model_validate_json(catalog_update_path.read_bytes())
        patch = prepare_catalog_patch(update, bundle_path)
        write_catalog_patch(patch, output)
    except (OSError, ValidationError, ValueError) as exc:
        _safe_error(str(exc), "catalog", type(exc).__name__)
        raise typer.Exit(1) from exc
    typer.echo(output.as_posix())


@catalog_app.command("complete-metadata")
def complete_catalog_metadata_command(
    catalog_update_path: Annotated[
        Path, typer.Argument(help="Path to the blocked catalog update JSON file")
    ],
    metadata_path: Annotated[
        Path, typer.Option("--metadata", help="Path to the reviewer-supplied metadata JSON file")
    ],
    bundle_path: Annotated[
        Path, typer.Option("--bundle", help="Path to the target catalog bundle directory")
    ],
    output: Annotated[
        Path, typer.Option("--output", help="Path to write the completed catalog update JSON")
    ],
) -> None:
    """Complete a blocked catalog update with reviewer-supplied concept metadata.

    The metadata JSON must provide, for every incomplete concept resolution:
    resolution_id, canonical_name, concept_type, scope_note, and optionally
    preferred_english and aliases.  No metadata may be inferred.
    """
    try:
        blocked_update = CatalogUpdateProposal.model_validate_json(catalog_update_path.read_bytes())
        raw_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if not isinstance(raw_metadata, list):
            raise ValueError("METADATA_MUST_BE_ARRAY")
        reviewed = tuple(ReviewedMetadataEntry.model_validate(item) for item in raw_metadata)
        completed = complete_catalog_update_metadata(blocked_update, reviewed, bundle_path)
        output.write_bytes(canonical_catalog_update_json(completed))
    except (OSError, ValidationError, ValueError) as exc:
        _safe_error(str(exc), "catalog", type(exc).__name__)
        raise typer.Exit(1) from exc
    typer.echo(
        f"catalog_update_id={completed.catalog_update_id} "
        f"parent_catalog_update_id={blocked_update.catalog_update_id} "
        f"status={completed.status}"
    )


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
