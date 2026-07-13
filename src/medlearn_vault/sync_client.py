"""Read-only, integrity-checked pull of published artifacts into MedLearn/."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from email.message import Message
from pathlib import Path, PurePosixPath
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic import ValidationError

from medlearn_vault import __version__
from medlearn_vault.sync_models import (
    ManagedArtifact,
    Manifest,
    ManifestArtifact,
    SyncConfig,
    SyncError,
    SyncState,
)
from medlearn_vault.windows_secrets import delete_token, load_token, store_token

MAX_MANIFEST = 4 * 1024 * 1024
MAX_ARTIFACT = 16 * 1024 * 1024
MAX_TOTAL = 128 * 1024 * 1024


@dataclass(frozen=True)
class SyncPaths:
    home: Path

    @property
    def config(self) -> Path:
        return self.home / "config.json"

    @property
    def state(self) -> Path:
        return self.home / "state.json"

    @property
    def credential(self) -> Path:
        return self.home / "credential.bin"

    @property
    def lock(self) -> Path:
        return self.home / "sync.lock"


class SyncResponse(Protocol):
    headers: Message

    def read(self, amount: int | None = None) -> bytes: ...

    def close(self) -> None: ...

    def getcode(self) -> int: ...


def paths() -> SyncPaths:
    value = os.environ.get("MEDLEARN_HOME")
    home = Path(value) if value else Path(os.environ.get("LOCALAPPDATA", "")) / "MedLearn" / "sync"
    return SyncPaths(home)


def _atomic_json(path: Path, model: SyncConfig | SyncState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (
        json.dumps(
            model.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        + "\n"
    )
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _read_model(path: Path, cls: type[SyncConfig] | type[SyncState]) -> SyncConfig | SyncState:
    try:
        return cls.model_validate_json(path.read_bytes())
    except (OSError, ValidationError, ValueError) as exc:
        raise SyncError("SYNC_STATE_FAILURE") from exc


def load_config(p: SyncPaths | None = None) -> SyncConfig:
    item = p or paths()
    if not item.config.exists():
        raise SyncError("SYNC_NOT_CONFIGURED")
    model = _read_model(item.config, SyncConfig)
    assert isinstance(model, SyncConfig)
    if (
        validate_endpoint(model.endpoint) != model.endpoint
        or not Path(model.vault_path).is_absolute()
    ):
        raise SyncError("SYNC_STATE_FAILURE")
    return model


def load_state(
    config: SyncConfig, p: SyncPaths | None = None, *, required: bool = False
) -> SyncState | None:
    item = p or paths()
    if not item.state.exists():
        if required:
            raise SyncError("SYNC_STATE_FAILURE")
        return None
    model = _read_model(item.state, SyncState)
    assert isinstance(model, SyncState)
    if model.endpoint != config.endpoint or model.vault_path != config.vault_path:
        raise SyncError("SYNC_STATE_FAILURE")
    return model


def _is_reparse(path: Path) -> bool:
    try:
        attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
        return path.is_symlink() or bool(attributes & 0x400)
    except (AttributeError, OSError):
        return path.is_symlink()


def validate_endpoint(endpoint: str) -> str:
    from urllib.parse import urlsplit

    parsed = urlsplit(endpoint)
    test_http = os.environ.get("MEDLEARN_SYNC_TESTING") == "1"
    allowed_loopback = parsed.hostname in {"127.0.0.1", "localhost"}
    if (
        parsed.scheme not in {"https", "http"}
        or (parsed.scheme != "https" and not (test_http and allowed_loopback))
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise SyncError("SYNC_INVALID_ENDPOINT")
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def configure(endpoint: str, vault: Path, p: SyncPaths | None = None) -> SyncConfig:
    normalized = validate_endpoint(endpoint)
    try:
        root = vault.resolve(strict=True)
    except OSError as exc:
        raise SyncError("SYNC_INVALID_VAULT") from exc
    if not root.is_dir() or not (root / ".obsidian").is_dir() or _is_reparse(vault):
        raise SyncError("SYNC_INVALID_VAULT")
    config = SyncConfig(endpoint=normalized, vault_path=str(root))
    item = p or paths()
    previous = load_config(item) if item.config.exists() else None
    _atomic_json(item.config, config)
    try:
        item.lock.touch(exist_ok=True)
    except OSError as exc:
        raise SyncError("SYNC_STATE_FAILURE") from exc
    if previous != config:
        item.state.unlink(missing_ok=True)
    return config


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: object, code: int, msg: str, headers: object, newurl: str
    ) -> Request | None:
        return None


def _quoted_digest(value: str) -> str:
    return f'"{value}"'


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _read_limited(response: SyncResponse, limit: int) -> bytes:
    data = response.read(limit + 1)
    if len(data) > limit:
        raise SyncError("SYNC_MANIFEST_PROTOCOL_ERROR")
    return data


def _open(request: Request, timeout: float) -> SyncResponse:
    try:
        return cast(SyncResponse, build_opener(_NoRedirect()).open(request, timeout=timeout))
    except HTTPError as exc:
        if exc.code in {301, 302, 303, 307, 308}:
            raise SyncError("SYNC_NETWORK_FAILURE") from exc
        if exc.code in {401, 403}:
            raise SyncError("SYNC_AUTH_FAILED") from exc
        raise SyncError("SYNC_NETWORK_FAILURE") from exc
    except (URLError, OSError) as exc:
        raise SyncError("SYNC_NETWORK_FAILURE") from exc


def _manifest(
    config: SyncConfig, token: str, state: SyncState | None, timeout: float
) -> tuple[Manifest, str, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "User-Agent": f"medlearn-vault/{__version__} windows-sync",
    }
    if state:
        headers["If-None-Match"] = state.manifest_etag
    request = Request(config.endpoint + "/v1/vault/manifest", headers=headers)
    try:
        response = cast(SyncResponse, build_opener(_NoRedirect()).open(request, timeout=timeout))
    except HTTPError as exc:
        if exc.code == 304:
            if state is None or exc.read() or exc.headers.get("ETag") != state.manifest_etag:
                raise SyncError("SYNC_MANIFEST_PROTOCOL_ERROR") from exc
            return (
                Manifest(manifest_version="0.1.0", artifacts=state.manifest_artifacts),
                state.manifest_etag,
                "not_modified",
            )
        if exc.code in {301, 302, 303, 307, 308}:
            raise SyncError("SYNC_NETWORK_FAILURE") from exc
        if exc.code in {401, 403}:
            raise SyncError("SYNC_AUTH_FAILED") from exc
        raise SyncError("SYNC_NETWORK_FAILURE") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise SyncError("SYNC_NETWORK_FAILURE") from exc
    try:
        if response.getcode() != 200:
            raise SyncError("SYNC_MANIFEST_PROTOCOL_ERROR")
        body = _read_limited(response, MAX_MANIFEST)
        etag = response.headers.get("ETag")
        if response.headers.get(
            "Content-Type"
        ) != "application/json; charset=utf-8" or etag != _quoted_digest(_digest(body)):
            raise SyncError("SYNC_MANIFEST_INTEGRITY_FAILURE")
        if (
            body.startswith(b"\xef\xbb\xbf")
            or b"\r" in body
            or not body.endswith(b"\n")
            or body.endswith(b"\n\n")
        ):
            raise SyncError("SYNC_MANIFEST_PROTOCOL_ERROR")
        manifest = Manifest.model_validate_json(body)
        canonical = (
            json.dumps(
                manifest.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        if canonical != body:
            raise SyncError("SYNC_MANIFEST_PROTOCOL_ERROR")
        return manifest, etag, "downloaded"
    except ValidationError as exc:
        raise SyncError("SYNC_MANIFEST_PROTOCOL_ERROR") from exc
    finally:
        response.close()


def _check_rollback(previous: SyncState | None, manifest: Manifest) -> None:
    if previous is None:
        return
    now = {a.path: a for a in manifest.artifacts}
    for old in previous.manifest_artifacts:
        new = now.get(old.path)
        if new is None or new.model_dump() != old.model_dump():
            raise SyncError("SYNC_MANIFEST_ROLLBACK")


def _target(root: Path, artifact: ManifestArtifact) -> Path:
    target = root.joinpath(*PurePosixPath(artifact.path).parts)
    managed = root / "MedLearn"
    try:
        target.relative_to(managed)
    except ValueError as exc:
        raise SyncError("SYNC_LOCAL_PATH_UNSAFE") from exc
    _validate_target_parent(root, target)
    return target


def _path_exists(path: Path) -> bool:
    return path.exists() or _is_reparse(path)


def _validate_target_parent(root: Path, target: Path) -> None:
    if _is_reparse(root) or not root.is_dir():
        raise SyncError("SYNC_LOCAL_PATH_UNSAFE")
    current = root
    for part in target.relative_to(root).parts[:-1]:
        current = current / part
        if _path_exists(current) and (_is_reparse(current) or not current.is_dir()):
            raise SyncError("SYNC_LOCAL_PATH_UNSAFE")


def _file_matches(path: Path, artifact: ManifestArtifact) -> bool:
    if _is_reparse(path):
        raise SyncError("SYNC_LOCAL_PATH_UNSAFE")
    if not path.is_file():
        return False
    digest = hashlib.sha256()
    length = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            length += len(block)
    return (
        length == artifact.byte_length and "sha256:" + digest.hexdigest() == artifact.content_digest
    )


def _download(
    config: SyncConfig, token: str, artifact: ManifestArtifact, timeout: float, total: int
) -> bytes:
    if total + artifact.byte_length > MAX_TOTAL or artifact.byte_length > MAX_ARTIFACT:
        raise SyncError("SYNC_ARTIFACT_INTEGRITY_FAILURE")
    url = config.endpoint + "/v1/vault/files?path=" + quote(artifact.path, safe="")
    request = Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept-Encoding": "identity",
            "User-Agent": f"medlearn-vault/{__version__} windows-sync",
        },
    )
    try:
        response = _open(request, timeout)
        headers = response.headers
        if response.getcode() != 200:
            raise SyncError("SYNC_ARTIFACT_INTEGRITY_FAILURE")
        length = headers.get("Content-Length")
        if (
            headers.get("Content-Type") != artifact.media_type
            or headers.get("ETag") != _quoted_digest(artifact.content_digest)
            or headers.get("Content-Encoding") not in {None, "identity"}
            or (length is not None and length != str(artifact.byte_length))
        ):
            raise SyncError("SYNC_ARTIFACT_INTEGRITY_FAILURE")
        body = response.read(artifact.byte_length + 1)
        if len(body) > artifact.byte_length:
            raise SyncError("SYNC_ARTIFACT_INTEGRITY_FAILURE")
        if len(body) != artifact.byte_length or _digest(body) != artifact.content_digest:
            raise SyncError("SYNC_ARTIFACT_INTEGRITY_FAILURE")
        return body
    finally:
        if "response" in locals():
            response.close()


def _atomic_create(root: Path, target: Path, body: bytes, artifact: ManifestArtifact) -> str:
    _validate_target_parent(root, target)
    target.parent.mkdir(parents=True, exist_ok=True)
    _validate_target_parent(root, target)
    fd, name = tempfile.mkstemp(
        prefix=f".{target.name}.medlearn-", suffix=".tmp", dir=target.parent
    )
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        if not _file_matches(temporary, artifact):
            raise SyncError("SYNC_LOCAL_WRITE_FAILURE")
        try:
            _validate_target_parent(root, target)
            os.link(temporary, target)
            return "downloaded"
        except FileExistsError:
            return "unchanged" if _file_matches(target, artifact) else "conflict"
        except OSError as exc:
            raise SyncError("SYNC_LOCAL_WRITE_FAILURE") from exc
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _lock(path: Path) -> Iterator[None]:
    try:
        handle = path.open("r+b")
    except OSError as exc:
        raise SyncError("SYNC_STATE_FAILURE") from exc
    acquired = False
    try:
        handle.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise SyncError("SYNC_ALREADY_RUNNING") from exc
        acquired = True
        yield
    finally:
        try:
            if acquired:
                handle.seek(0)
                if sys.platform == "win32":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def login(token: str, p: SyncPaths | None = None) -> None:
    load_config(p)
    if len(token) < 32:
        raise SyncError("SYNC_CREDENTIAL_FAILURE")
    store_token((p or paths()).credential, token)


def logout(p: SyncPaths | None = None) -> None:
    delete_token((p or paths()).credential)


def status(p: SyncPaths | None = None) -> dict[str, object]:
    item = p or paths()
    config = load_config(item)
    state = load_state(config, item)
    try:
        load_token(item.credential)
        authenticated = True
    except SyncError:
        authenticated = False
    return {
        "configured": True,
        "authenticated": authenticated,
        "endpoint": config.endpoint,
        "vault": config.vault_path,
        "vault_exists": Path(config.vault_path).is_dir(),
        "obsidian_vault": (Path(config.vault_path) / ".obsidian").is_dir(),
        "state_available": state is not None,
        "manifest_artifact_count": len(state.manifest_artifacts) if state else 0,
    }


def pull(
    *, dry_run: bool = False, timeout: float = 30, p: SyncPaths | None = None
) -> dict[str, object]:
    if timeout <= 0:
        raise SyncError("SYNC_NETWORK_FAILURE")
    item = p or paths()
    config = load_config(item)
    state = load_state(config, item)
    token = load_token(item.credential)
    root = Path(config.vault_path)
    if not root.is_dir() or not (root / ".obsidian").is_dir() or _is_reparse(root):
        raise SyncError("SYNC_INVALID_VAULT")
    with _lock(item.lock):
        manifest, etag, manifest_status = _manifest(config, token, state, timeout)
        _check_rollback(state, manifest)
        downloaded = unchanged = conflicts = would_download = total = 0
        conflict_paths: list[str] = []
        managed: dict[str, ManagedArtifact] = {}
        for artifact in manifest.artifacts:
            target = _target(root, artifact)
            if _path_exists(target):
                if _is_reparse(target):
                    raise SyncError("SYNC_LOCAL_PATH_UNSAFE")
                if target.is_dir():
                    conflicts += 1
                    conflict_paths.append(artifact.path)
                    continue
                if target.is_file() and _file_matches(target, artifact):
                    unchanged += 1
                    managed[artifact.path] = ManagedArtifact(
                        content_digest=artifact.content_digest,
                        media_type=artifact.media_type,
                        byte_length=artifact.byte_length,
                    )
                    continue
                if target.is_file():
                    conflicts += 1
                    conflict_paths.append(artifact.path)
                    continue
                raise SyncError("SYNC_LOCAL_PATH_UNSAFE")
            would_download += 1
            if dry_run:
                continue
            result = _atomic_create(
                root, target, _download(config, token, artifact, timeout, total), artifact
            )
            total += artifact.byte_length
            if result == "downloaded":
                downloaded += 1
                managed[artifact.path] = ManagedArtifact(
                    content_digest=artifact.content_digest,
                    media_type=artifact.media_type,
                    byte_length=artifact.byte_length,
                )
            elif result == "unchanged":
                unchanged += 1
                managed[artifact.path] = ManagedArtifact(
                    content_digest=artifact.content_digest,
                    media_type=artifact.media_type,
                    byte_length=artifact.byte_length,
                )
            else:
                conflicts += 1
                conflict_paths.append(artifact.path)
        if not dry_run:
            _atomic_json(
                item.state,
                SyncState(
                    endpoint=config.endpoint,
                    vault_path=config.vault_path,
                    manifest_etag=etag,
                    manifest_artifacts=manifest.artifacts,
                    managed_artifacts=managed,
                ),
            )
        return {
            "status": "synced",
            "manifest_status": manifest_status,
            "remote_count": len(manifest.artifacts),
            "downloaded_count": downloaded,
            "unchanged_count": unchanged,
            "conflict_count": conflicts,
            "conflict_paths": sorted(conflict_paths),
            "would_download_count": would_download if dry_run else 0,
        }
