from pathlib import Path

from typer.testing import CliRunner

from medlearn_vault.cli import app

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "0.1.0"


def test_schema_export(tmp_path: Path) -> None:
    result = runner.invoke(app, ["schema", "export", "-o", str(tmp_path)])
    assert result.exit_code == 0
    assert (tmp_path / "concept_entity.schema.json").exists()
