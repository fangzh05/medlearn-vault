"""Local browser UI for the explicit note-composition preview flow."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from medlearn_vault.composition import (
    attach_retrieval,
    build_context,
    compose_preview,
    validate_composition,
    validate_generated_note,
)
from medlearn_vault.deepseek_composer import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    DeepSeekComposerError,
    DeepSeekNoteComposer,
)

WEB_DIR = Path(__file__).with_name("webui")


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _decode_file(value: dict[str, Any] | None, *, binary: bool = False) -> bytes | str | None:
    if not value:
        return None
    encoded = value.get("content_base64")
    if not isinstance(encoded, str):
        raise ValueError("WEB_FILE_INVALID")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise ValueError("WEB_FILE_INVALID") from exc
    return raw if binary else raw.decode("utf-8")


def compose_from_web(payload: dict[str, Any]) -> dict[str, Any]:
    intake = _decode_file(payload.get("intake"))
    template = _decode_file(payload.get("template"))
    prompt = _decode_file(payload.get("prompt"))
    current_note = _decode_file(payload.get("current_note"))
    golden_example = _decode_file(payload.get("golden_example"))
    if template is None:
        template = Path("templates/medical_note_v1.md").read_text(encoding="utf-8")
    if prompt is None:
        prompt = Path("prompts/deepseek_note_composer_v1.md").read_text(encoding="utf-8")
    if not isinstance(intake, str) or not isinstance(template, str):
        raise ValueError("WEB_INTAKE_AND_TEMPLATE_REQUIRED")
    composer_name = payload.get("composer", "stub")
    if composer_name not in {"stub", "deepseek"}:
        raise ValueError("UNSUPPORTED_COMPOSER")
    try:
        context = build_context(
            intake.encode("utf-8"),
            template=template,
            current_note=current_note if isinstance(current_note, str) else None,
            source_job_id=payload.get("source_job_id") or None,
        )
        index_bytes = _decode_file(payload.get("index"), binary=True)
        with tempfile.TemporaryDirectory(prefix="medlearn-ui-") as temp_dir:
            index_path: Path | None = None
            if isinstance(index_bytes, bytes):
                index_path = Path(temp_dir) / "source.sqlite3"
                index_path.write_bytes(index_bytes)
            if index_path is not None:
                context = attach_retrieval(
                    context, index_path, int(payload.get("retrieval_limit", 6))
                )
            if composer_name == "stub":
                result = compose_preview(context)
                validation = validate_composition(context)
                model = None
                request_digest = None
            else:
                if not isinstance(prompt, str) or not prompt.strip():
                    raise ValueError("DEEPSEEK_PROMPT_REQUIRED")
                api_key = payload.get("api_key") or os.environ.get("DEEPSEEK_API_KEY")
                composer = DeepSeekNoteComposer(
                    prompt,
                    model=payload.get("model", DEFAULT_MODEL),
                    base_url=payload.get("base_url", DEFAULT_BASE_URL),
                    api_key=api_key,
                    timeout=float(payload.get("timeout_seconds", 300)),
                    max_tokens=int(payload.get("max_tokens", 16384)),
                    golden_example=golden_example if isinstance(golden_example, str) else None,
                )
                result = compose_preview(context, composer)
                validation = validate_generated_note(context, result.markdown)
                model = composer.model
                request_digest = composer.request_digest
            output_sha256 = "sha256:" + hashlib.sha256(result.markdown.encode("utf-8")).hexdigest()
            warnings = list(validation.warnings)
            blockers = list(validation.blockers)
            return {
                "status": "rejected" if blockers else validation.status,
                "composer": composer_name,
                "model": model,
                "markdown": None if blockers else result.markdown,
                "target_path": result.target_path,
                "warning_count": len(warnings),
                "warning_codes": [item.code for item in warnings],
                "blocker_count": len(blockers),
                "blocker_codes": [item.code for item in blockers],
                "isolated_count": len(result.isolated_items),
                "retrieval_count": len(context.retrieved_sources),
                "retrieval_digest": context.retrieval_digest,
                "request_digest": request_digest,
                "output_sha256": output_sha256,
            }
    except (OSError, ValueError, DeepSeekComposerError) as exc:
        return {"status": "rejected", "error_code": str(exc), "markdown": None}


class _Handler(BaseHTTPRequestHandler):
    server_version = "MedLearnUI/1.0"

    def _send(self, status: int, value: Any, content_type: str = "application/json") -> None:
        body = value if isinstance(value, bytes) else _json_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return
        if self.path == "/api/health":
            self._send(HTTPStatus.OK, {"status": "ok", "default_composer": "stub"})
            return
        if self.path in {"/", "/index.html"}:
            self._send(HTTPStatus.OK, (WEB_DIR / "index.html").read_bytes(), "text/html")
            return
        if self.path == "/app.css":
            self._send(HTTPStatus.OK, (WEB_DIR / "app.css").read_bytes(), "text/css")
            return
        if self.path == "/app.js":
            self._send(HTTPStatus.OK, (WEB_DIR / "app.js").read_bytes(), "text/javascript")
            return
        self._send(HTTPStatus.NOT_FOUND, {"error_code": "WEB_NOT_FOUND"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/compose":
            self._send(HTTPStatus.NOT_FOUND, {"error_code": "WEB_NOT_FOUND"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 20_000_000:
                raise ValueError("WEB_REQUEST_TOO_LARGE")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("WEB_REQUEST_INVALID")
            result = compose_from_web(payload)
            self._send(HTTPStatus.OK, result)
        except (ValueError, json.JSONDecodeError) as exc:
            self._send(HTTPStatus.BAD_REQUEST, {"status": "rejected", "error_code": str(exc)})
        except Exception:
            self._send(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"status": "rejected", "error_code": "WEB_INTERNAL_ERROR"},
            )

    def log_message(self, _format: str, *_args: object) -> None:
        return


def serve(host: str = "127.0.0.1", port: int = 8765, *, open_browser: bool = True) -> None:
    server = ThreadingHTTPServer((host, port), _Handler)
    url = f"http://{host}:{port}/"
    if open_browser:
        threading.Timer(0.25, lambda: webbrowser.open(url)).start()
    print(f"MedLearn UI: {url}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
