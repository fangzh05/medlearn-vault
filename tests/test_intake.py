import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from medlearn_vault.capture import (
    CaptureDraft,
    IntakeDigestMismatch,
    IntakeEnvelope,
    InvalidIntakeEnvelope,
    capture_draft_digest,
    extract_capture_draft,
    intake_envelope_digest,
)
from medlearn_vault.cli import app

FIXTURE = Path("examples/intake/manual-copd.json")
EXPECTED_DRAFT = Path("examples/intake/expected-draft.json")
HANDOFF_FIXTURE = Path("examples/intake/project-handoff-empty.json")
HANDOFF_DIGEST = Path("examples/intake/project-handoff-empty.digest.txt")


def test_shared_intake_fixture_extracts_deterministically() -> None:
    exact = FIXTURE.read_bytes()
    intake_digest = "sha256:" + hashlib.sha256(exact).hexdigest()
    draft_bytes, draft_digest = extract_capture_draft(exact, intake_digest)
    draft = CaptureDraft.model_validate_json(draft_bytes)
    assert IntakeEnvelope.model_validate_json(exact).draft == draft
    assert draft_bytes == EXPECTED_DRAFT.read_bytes()
    assert intake_envelope_digest(exact) == intake_digest
    assert capture_draft_digest(draft) == draft_digest
    assert intake_digest != draft_digest


def test_client_kind_is_only_untrusted_envelope_metadata() -> None:
    envelope = IntakeEnvelope.model_validate_json(FIXTURE.read_bytes())
    alternate = envelope.model_copy(update={"client_kind": "chatgpt_work"})
    assert alternate.draft == envelope.draft
    assert capture_draft_digest(alternate.draft) == capture_draft_digest(envelope.draft)
    assert "client_kind" not in alternate.draft.model_dump()


def test_intake_rejects_tampering_and_unsupported_versions() -> None:
    exact = FIXTURE.read_bytes()
    with pytest.raises(IntakeDigestMismatch, match="INTAKE_DIGEST_MISMATCH"):
        extract_capture_draft(exact, "sha256:" + "0" * 64)
    payload = IntakeEnvelope.model_validate_json(exact).model_dump(mode="json")
    payload["intake_version"] = "0.2.0"
    with pytest.raises(ValidationError):
        IntakeEnvelope.model_validate(payload)
    payload = IntakeEnvelope.model_validate_json(exact).model_dump(mode="json")
    payload["draft"]["draft_version"] = "0.2.0"
    with pytest.raises(ValidationError):
        IntakeEnvelope.model_validate(payload)


def test_exact_digest_and_invalid_envelope_have_distinct_failures() -> None:
    payload = IntakeEnvelope.model_validate_json(FIXTURE.read_bytes()).model_dump(mode="json")
    captured_at = payload["draft"]["context"]["captured_at"]
    payload["draft"]["evidence_messages"].extend(
        [
            {
                "message_id": "message_schema_failure_user",
                "role": "user",
                "observed_at": captured_at,
                "excerpt": "synthetic user evidence",
            },
            {
                "message_id": "message_schema_failure_assistant",
                "role": "assistant",
                "observed_at": captured_at,
                "excerpt": "synthetic assistant evidence",
            },
        ]
    )
    payload["draft"]["claim_candidates"][0]["evidence_message_ids"] = [
        "message_schema_failure_user",
        "message_schema_failure_assistant",
    ]
    exact = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    digest = intake_envelope_digest(exact)
    with pytest.raises(InvalidIntakeEnvelope, match="INVALID_INTAKE_ENVELOPE"):
        extract_capture_draft(exact, digest)


def test_cross_runtime_handoff_fixture_preserves_exact_bytes() -> None:
    exact = HANDOFF_FIXTURE.read_bytes()
    expected = HANDOFF_DIGEST.read_text(encoding="utf-8").strip()
    assert intake_envelope_digest(exact) == expected
    assert extract_capture_draft(exact, expected)
    tampered = exact[:-1] + bytes([exact[-1] ^ 1])
    with pytest.raises(IntakeDigestMismatch, match="INTAKE_DIGEST_MISMATCH"):
        extract_capture_draft(tampered, expected)


def test_extract_intake_cli_writes_no_output_on_digest_failure(tmp_path: Path) -> None:
    runner = CliRunner()
    output = tmp_path / "draft.json"
    failed = runner.invoke(
        app, ["capture", "extract-intake", str(FIXTURE), "sha256:" + "0" * 64, str(output)]
    )
    assert failed.exit_code == 1
    assert "INTAKE_DIGEST_MISMATCH" in failed.stderr
    assert not output.exists()
    digest = intake_envelope_digest(FIXTURE.read_bytes())
    succeeded = runner.invoke(
        app, ["capture", "extract-intake", str(FIXTURE), digest, str(output)]
    )
    assert succeeded.exit_code == 0
    assert "draft_digest=sha256:" in succeeded.stdout
    assert output.read_bytes() == EXPECTED_DRAFT.read_bytes()
