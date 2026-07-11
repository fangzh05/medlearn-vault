import json
import os
import platform
import sys
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from pydantic import BaseModel, ValidationError

from medlearn_vault import __version__
from medlearn_vault.domain import ChapterDossier, ConceptEntity, LearningCapture, MedicalClaim

app = typer.Typer(no_args_is_help=True, help="MedLearn Vault contract tools")
schema_app = typer.Typer(help="Export JSON schemas")
concept_app = typer.Typer(help="Validate concept entities")
app.add_typer(schema_app, name="schema")
app.add_typer(concept_app, name="concept")

SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "concept_entity": ConceptEntity,
    "medical_claim": MedicalClaim,
    "chapter_dossier": ChapterDossier,
    "learning_capture": LearningCapture,
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
def doctor(
    vault: Annotated[Path | None, typer.Option("--vault", envvar="MEDLEARN_VAULT_PATH")] = None,
) -> None:
    python_ok = sys.version_info >= (3, 12)
    timezone_ok = datetime.now().astimezone().utcoffset() is not None
    target = vault or Path.cwd()
    vault_ok = target.is_dir() and os.access(target, os.R_OK | os.W_OK)
    typer.echo(f"medlearn: {__version__}")
    typer.echo(f"python: {platform.python_version()} ({'ok' if python_ok else 'requires >=3.12'})")
    typer.echo(f"timezone: {'ok' if timezone_ok else 'unavailable'}")
    typer.echo(f"vault: {target.resolve()} ({'read/write' if vault_ok else 'unavailable'})")
    typer.echo(f"configuration: {'MEDLEARN_VAULT_PATH' if vault else 'working-directory fallback'}")
    typer.echo("contracts: ok")
    if not all((python_ok, timezone_ok, vault_ok)):
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
    if mismatches:
        typer.echo("schema snapshots differ: " + ", ".join(mismatches), err=True)
        raise typer.Exit(1)
    typer.echo(f"schema snapshots: ok ({len(SCHEMA_MODELS)})")


@concept_app.command("validate")
def validate_concept(path: Path) -> None:
    try:
        concept = ConceptEntity.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError) as exc:
        typer.echo(f"invalid: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"valid: {concept.concept_id}")


if __name__ == "__main__":
    app()
