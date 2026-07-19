from __future__ import annotations

import base64
from pathlib import Path

import medlearn_vault.web as web
from medlearn_vault.web import compose_from_web


def _file(text: str) -> dict[str, str]:
    return {"content_base64": base64.b64encode(text.encode()).decode()}


def test_web_stub_uses_existing_composer_without_network(tmp_path) -> None:
    intake = Path("examples/intake/manual-copd.json").read_text(encoding="utf-8")
    template = Path("templates/medical_note_v1.md").read_text(encoding="utf-8")
    result = compose_from_web(
        {
            "intake": _file(intake),
            "template": _file(template),
            "composer": "stub",
        }
    )
    assert result["composer"] == "stub"
    assert result["status"] in {"accepted", "accepted_with_warnings"}
    assert result["markdown"]
    assert result["request_digest"] is None


def test_web_rejects_missing_intake() -> None:
    try:
        compose_from_web({"composer": "stub"})
    except ValueError as exc:
        assert str(exc) == "WEB_INTAKE_AND_TEMPLATE_REQUIRED"
    else:
        raise AssertionError("missing intake should be rejected")


def test_web_does_not_use_environment_api_key(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-be-used")
    intake = Path("examples/intake/manual-copd.json").read_text(encoding="utf-8")
    result = compose_from_web(
        {
            "intake": _file(intake),
            "template": _file(Path("templates/medical_note_v1.md").read_text(encoding="utf-8")),
            "prompt": _file("prompt"),
            "composer": "deepseek",
        }
    )
    assert result["error_code"] == "DEEPSEEK_API_KEY_MISSING"


def test_web_server_is_always_bound_to_loopback(monkeypatch) -> None:
    bound: list[tuple[str, int]] = []

    class FakeServer:
        def __init__(self, address: tuple[str, int], _handler: object) -> None:
            bound.append(address)

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def server_close(self) -> None:
            pass

    monkeypatch.setattr(web, "ThreadingHTTPServer", FakeServer)
    web.serve(9123, open_browser=False)
    assert bound == [("127.0.0.1", 9123)]
