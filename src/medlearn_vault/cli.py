import json
import platform
import sys
from pathlib import Path
from typing import Annotated

import typer
from pydantic import BaseModel, ValidationError

from medlearn_vault import __version__
from medlearn_vault.domain import ChapterDossier, ConceptEntity, LearningCapture

app = typer.Typer(no_args_is_help=True, help="MedLearn Vault contract tools")
schema_app = typer.Typer(help="Export JSON schemas")
concept_app = typer.Typer(help="Validate concept entities")
app.add_typer(schema_app, name="schema")
app.add_typer(concept_app, name="concept")


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
    ok = sys.version_info >= (3, 12)
    typer.echo(f"medlearn: {__version__}")
    typer.echo(f"python: {platform.python_version()} ({'ok' if ok else 'requires >=3.12'})")
    typer.echo("contracts: ok")
    if not ok:
        raise typer.Exit(1)


@schema_app.command("export")
def export_schema(
    output: Annotated[Path, typer.Option("--output", "-o")] = Path("schemas/generated"),
) -> None:
    output.mkdir(parents=True, exist_ok=True)
    models: dict[str, type[BaseModel]] = {
        "concept_entity": ConceptEntity,
        "chapter_dossier": ChapterDossier,
        "learning_capture": LearningCapture,
    }
    for name, model in models.items():
        path = output / f"{name}.schema.json"
        path.write_text(
            json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        typer.echo(path.as_posix())


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
