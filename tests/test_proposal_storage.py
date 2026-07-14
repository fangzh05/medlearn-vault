import hashlib
import json
from pathlib import Path

from test_workflow import MemoryStore, seed_proposal
from typer.testing import CliRunner

from medlearn_vault.capture import CaptureProposal, exact_capture_proposal_json
from medlearn_vault.cli import app


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def test_workflow_cli_and_catalog_update_share_exact_proposal_bytes(tmp_path: Path) -> None:
    store = MemoryStore()
    proposal_id, _, _, stored_bytes = seed_proposal(store)
    stored = CaptureProposal.model_validate_json(stored_bytes)
    assert exact_capture_proposal_json(stored) == stored_bytes

    runner = CliRunner()
    cli_proposal = tmp_path / "proposal.json"
    proposed = runner.invoke(
        app,
        [
            "capture",
            "propose",
            "examples/copd",
            "examples/capture/copd-session/draft.json",
            str(cli_proposal),
        ],
    )
    assert proposed.exit_code == 0
    assert cli_proposal.read_bytes() == stored_bytes

    catalog_update = tmp_path / "catalog-update.json"
    review = tmp_path / "review.md"
    updated = runner.invoke(
        app,
        [
            "capture",
            "catalog-update",
            str(cli_proposal),
            str(catalog_update),
            str(review),
            "--bundle",
            "examples/copd",
        ],
    )
    assert updated.exit_code == 0
    update = json.loads(catalog_update.read_text(encoding="utf-8"))
    assert update["capture_proposal_id"] == proposal_id
    assert update["capture_proposal_object_digest"] == _digest(stored_bytes)
