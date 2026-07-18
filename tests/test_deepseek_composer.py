import json
from pathlib import Path

import pytest

from medlearn_vault.composition import build_context
from medlearn_vault.deepseek_composer import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    DeepSeekComposerError,
    DeepSeekNoteComposer,
    build_deepseek_user_payload,
    normalize_generated_markdown,
    validate_base_url,
)


class FakeTransport:
    def __init__(self, response: bytes) -> None:
        self.response = response
        self.body = b""

    def post(self, url: str, body: bytes, api_key: str, timeout: float) -> bytes:
        self.body = body
        assert api_key == "secret"
        return self.response


def _context():
    return build_context(Path("examples/intake/manual-copd.json").read_bytes(), template="# T")


def test_payload_order_and_outbound_boundary() -> None:
    payload = build_deepseek_user_payload(_context())
    assert list(json.loads(payload)) == [
        "task",
        "template",
        "concept_metadata",
        "current_note",
        "learning_record",
        "retrieved_sources",
        "golden_example",
        "output_constraints",
    ]
    assert (
        "source_file" not in payload
        and "source_relative_path" not in payload
        and "query" not in payload
    )
    assert "learning_context_unverified" in payload


def test_request_is_deterministic_and_secret_free() -> None:
    response = json.dumps(
        {"choices": [{"finish_reason": "stop", "message": {"content": "\ufeff# draft\r\n"}}]}
    ).encode()
    transport = FakeTransport(response)
    composer = DeepSeekNoteComposer("system", api_key="secret", transport=transport)
    assert composer.compose(_context()) == "# draft\n"
    assert composer.model == DEFAULT_MODEL and DEFAULT_BASE_URL.endswith("/chat/completions")
    assert (
        b'"stream":false' in transport.body and b'"thinking":{"type":"disabled"}' in transport.body
    )
    assert b"secret" not in transport.body and composer.request_digest


@pytest.mark.parametrize(
    "url", ["http://api.deepseek.com/chat/completions", "https://api.deepseek.com/x"]
)
def test_invalid_url_and_models_reject_before_network(url: str) -> None:
    with pytest.raises(DeepSeekComposerError, match="DEEPSEEK_BASE_URL_INVALID"):
        validate_base_url(url)
    with pytest.raises(DeepSeekComposerError, match="UNSUPPORTED_DEEPSEEK_MODEL"):
        DeepSeekNoteComposer("x", api_key="secret", model="deepseek-chat")


def test_missing_key_and_normalization() -> None:
    with pytest.raises(DeepSeekComposerError, match="DEEPSEEK_API_KEY_MISSING"):
        DeepSeekNoteComposer("x", api_key=None)
    assert normalize_generated_markdown("a\r\n\r\n") == "a\n"
