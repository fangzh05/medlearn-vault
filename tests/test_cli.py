from pathlib import Path

from typer.testing import CliRunner

from medlearn_vault.cli import app

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "0.4.0"


def test_schema_export(tmp_path: Path) -> None:
    result = runner.invoke(app, ["schema", "export", "-o", str(tmp_path)])
    assert result.exit_code == 0
    assert (tmp_path / "concept_entity.schema.json").exists()
    assert (tmp_path / "medical_claim.schema.json").exists()
    assert (tmp_path / "concept_relation.schema.json").exists()
    assert (tmp_path / "discipline_lens.schema.json").exists()
    check = runner.invoke(app, ["schema", "check", "--snapshot", str(tmp_path)])
    assert check.exit_code == 0


def test_schema_check_detects_drift(tmp_path: Path) -> None:
    result = runner.invoke(app, ["schema", "check", "--snapshot", str(tmp_path)])
    assert result.exit_code == 1
    assert "schema snapshots differ" in result.stderr
