from pathlib import Path

from typer.testing import CliRunner

from medlearn_vault.cli import app

runner = CliRunner()


def test_sync_help() -> None:
    for command in ([], ["configure"], ["login"], ["logout"], ["status"], ["pull"]):
        result = runner.invoke(app, ["sync", *command, "--help"])
        assert result.exit_code == 0


def test_sync_configure_json(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    result = runner.invoke(
        app,
        [
            "sync",
            "configure",
            "--endpoint",
            "https://example.test",
            "--vault",
            str(vault),
            "--json",
        ],
        env={"MEDLEARN_HOME": str(tmp_path / "home")},
    )
    assert result.exit_code == 0
    assert '"status": "configured"' in result.stdout
