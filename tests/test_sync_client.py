import hashlib
import io
import json
import subprocess
import sys
import time
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from medlearn_vault import sync_client
from medlearn_vault.sync_models import (
    ManagedArtifact,
    Manifest,
    ManifestArtifact,
    RolloutState,
    SyncError,
    SyncState,
)

CAPTURE = "capture_" + "a" * 32
PLAN = "publication_plan_" + "b" * 32
ETAG = '"sha256:' + "c" * 64 + '"'


def artifact(path: str, media_type: str, body: bytes) -> ManifestArtifact:
    return ManifestArtifact(
        path=path,
        media_type=media_type,
        content_digest="sha256:" + hashlib.sha256(body).hexdigest(),
        byte_length=len(body),
        capture_id=CAPTURE,
        publication_plan_id=PLAN,
    )


def presentation_artifact(path: str, body: bytes, generation: str) -> ManifestArtifact:
    return ManifestArtifact(
        path=path,
        media_type="text/markdown; charset=utf-8",
        content_digest="sha256:" + hashlib.sha256(body).hexdigest(),
        byte_length=len(body),
        presentation_generation_id=generation,
    )


def presentation_manifest(
    generation: str, items: list[ManifestArtifact], previous: str | None = None
) -> Manifest:
    return Manifest(
        manifest_version="0.2.0",
        presentation_generation_id=generation,
        presentation_receipt_digest="sha256:" + generation.removeprefix("presentation_") * 2,
        previous_generation_id=previous,
        artifacts=sorted(items, key=lambda item: item.path),
    )


def managed(item: ManifestArtifact) -> ManagedArtifact:
    return ManagedArtifact(
        content_digest=item.content_digest, media_type=item.media_type, byte_length=item.byte_length
    )


def seed_ready_state(
    config: sync_client.SyncConfig, home: sync_client.SyncPaths, manifest: Manifest
) -> None:
    sync_client._atomic_json(
        home.state,
        SyncState(
            endpoint=config.endpoint,
            vault_path=config.vault_path,
            manifest_etag=ETAG,
            manifest_version=manifest.manifest_version,
            presentation_generation_id=manifest.presentation_generation_id,
            presentation_receipt_digest=manifest.presentation_receipt_digest,
            previous_generation_id=manifest.previous_generation_id,
            manifest_artifacts=manifest.artifacts,
            managed_artifacts={item.path: managed(item) for item in manifest.artifacts},
        ),
    )
    sync_client._atomic_json(
        home.rollout,
        RolloutState(
            endpoint=config.endpoint,
            vault_path=config.vault_path,
            dry_run_succeeded=True,
            first_pull_completed=True,
        ),
    )


def vault(tmp_path: Path) -> Path:
    result = tmp_path / "知识库"
    (result / ".obsidian").mkdir(parents=True, exist_ok=True)
    return result


class Response:
    def __init__(self, body: bytes, code: int = 200, **headers: str) -> None:
        self.body = body
        self.code = code
        self.headers = Message()
        for name, value in headers.items():
            self.headers[name.replace("_", "-")] = value

    def read(self, amount: int | None = None) -> bytes:
        return self.body if amount is None else self.body[:amount]

    def close(self) -> None:
        pass

    def getcode(self) -> int:
        return self.code


def opener(monkeypatch: pytest.MonkeyPatch, result: Response | BaseException) -> None:
    class Opener:
        def open(self, *_: object, **__: object) -> Response:
            if isinstance(result, BaseException):
                raise result
            return result

    monkeypatch.setattr(sync_client, "build_opener", lambda *_: Opener())


def manifest_body(items: list[ManifestArtifact]) -> bytes:
    return (
        json.dumps(
            Manifest(manifest_version="0.1.0", artifacts=items).model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )


def test_configure_requires_https_and_obsidian(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEDLEARN_HOME", str(tmp_path / "home"))
    with pytest.raises(SyncError, match="SYNC_INVALID_ENDPOINT"):
        sync_client.configure("http://example.test", vault(tmp_path))
    with pytest.raises(SyncError, match="SYNC_INVALID_VAULT"):
        sync_client.configure("https://example.test", tmp_path / "not-a-vault")
    config = sync_client.configure("https://example.test/", vault(tmp_path))
    assert config.endpoint == "https://example.test"
    assert "token" not in sync_client.paths().config.read_text(encoding="utf-8")


def test_pull_is_idempotent_and_only_writes_medlearn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEDLEARN_HOME", str(tmp_path / "home"))
    root = vault(tmp_path)
    sync_client.configure("https://example.test", root)
    json_body, markdown_body = b'{"a":1}\n', b"# capture\n"
    artifacts = [
        artifact(
            f"MedLearn/Captures/2026/07/{CAPTURE}.md", "text/markdown; charset=utf-8", markdown_body
        ),
        artifact(
            f"MedLearn/Data/Captures/{CAPTURE}.json", "application/json; charset=utf-8", json_body
        ),
    ]
    manifest = Manifest(manifest_version="0.1.0", artifacts=artifacts)
    content = {
        item.path: body for item, body in zip(artifacts, (markdown_body, json_body), strict=True)
    }
    monkeypatch.setattr(sync_client, "load_token", lambda _: "x" * 32)
    monkeypatch.setattr(sync_client, "_manifest", lambda *_: (manifest, ETAG, "downloaded"))
    monkeypatch.setattr(sync_client, "_download", lambda _, __, item, ___, ____: content[item.path])
    dry_run = sync_client.pull(dry_run=True, p=sync_client.paths())
    assert dry_run["would_download_count"] == 2
    first = sync_client.pull(confirm_first_pull=True, p=sync_client.paths())
    assert first["downloaded_count"] == 2
    assert (root / artifacts[1].path).read_bytes() == json_body
    assert not (root / ".obsidian" / "anything").exists()
    monkeypatch.setattr(sync_client, "_manifest", lambda *_: (manifest, ETAG, "not_modified"))
    second = sync_client.pull(p=sync_client.paths())
    assert second["downloaded_count"] == 0
    assert second["unchanged_count"] == 2


def test_conflicting_local_file_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEDLEARN_HOME", str(tmp_path / "home"))
    root = vault(tmp_path)
    sync_client.configure("https://example.test", root)
    body = b"expected\n"
    item = artifact(
        f"MedLearn/Data/Captures/{CAPTURE}.json", "application/json; charset=utf-8", body
    )
    target = root / item.path
    target.parent.mkdir(parents=True)
    target.write_bytes(b"user edited\n")
    monkeypatch.setattr(sync_client, "load_token", lambda _: "x" * 32)
    monkeypatch.setattr(
        sync_client,
        "_manifest",
        lambda *_: (
            Manifest(manifest_version="0.1.0", artifacts=[item]),
            ETAG,
            "downloaded",
        ),
    )
    sync_client.pull(dry_run=True, p=sync_client.paths())
    result = sync_client.pull(confirm_first_pull=True, p=sync_client.paths())
    assert result["conflict_count"] == 1
    assert target.read_bytes() == b"user edited\n"


def test_dry_run_does_not_create_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEDLEARN_HOME", str(tmp_path / "home"))
    root = vault(tmp_path)
    sync_client.configure("https://example.test", root)
    item = artifact(
        f"MedLearn/Data/Captures/{CAPTURE}.json", "application/json; charset=utf-8", b"{}\n"
    )
    monkeypatch.setattr(sync_client, "load_token", lambda _: "x" * 32)
    monkeypatch.setattr(
        sync_client,
        "_manifest",
        lambda *_: (
            Manifest(manifest_version="0.1.0", artifacts=[item]),
            ETAG,
            "downloaded",
        ),
    )
    assert sync_client.pull(dry_run=True, p=sync_client.paths())["would_download_count"] == 1
    assert not (root / "MedLearn").exists()
    assert not sync_client.paths().state.exists()
    assert sync_client.paths().lock.exists()


@pytest.mark.parametrize("edited", [False, True])
def test_reader_projection_migrates_only_untouched_legacy_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, edited: bool
) -> None:
    monkeypatch.setenv("MEDLEARN_HOME", str(tmp_path / "home"))
    root = vault(tmp_path)
    config = sync_client.configure("https://example.test", root)
    legacy_body = b"# canonical\n"
    legacy = artifact(
        f"MedLearn/Captures/2026/07/{CAPTURE}.md", "text/markdown; charset=utf-8", legacy_body
    )
    legacy_target = root / legacy.path
    legacy_target.parent.mkdir(parents=True)
    legacy_target.write_bytes(b"local edit\n" if edited else legacy_body)
    home = sync_client.paths()
    sync_client._atomic_json(
        home.state,
        SyncState(
            endpoint=config.endpoint,
            vault_path=config.vault_path,
            manifest_etag=ETAG,
            manifest_version="0.1.0",
            manifest_artifacts=[legacy],
            managed_artifacts={
                legacy.path: ManagedArtifact(
                    content_digest=legacy.content_digest,
                    media_type=legacy.media_type,
                    byte_length=legacy.byte_length,
                )
            },
        ),
    )
    sync_client._atomic_json(
        home.rollout,
        RolloutState(
            endpoint=config.endpoint,
            vault_path=config.vault_path,
            dry_run_succeeded=True,
            first_pull_completed=True,
        ),
    )
    reader_body = "# 房室结\n".encode()
    reader = ManifestArtifact(
        path="MedLearn/概念/房室结.md",
        media_type="text/markdown; charset=utf-8",
        content_digest="sha256:" + hashlib.sha256(reader_body).hexdigest(),
        byte_length=len(reader_body),
        presentation_generation_id="presentation_" + "c" * 32,
    )
    manifest = Manifest(
        manifest_version="0.2.0",
        presentation_generation_id=reader.presentation_generation_id,
        presentation_receipt_digest="sha256:" + "d" * 64,
        artifacts=[reader],
    )
    monkeypatch.setattr(sync_client, "load_token", lambda _: "x" * 32)
    monkeypatch.setattr(sync_client, "_manifest", lambda *_: (manifest, ETAG, "downloaded"))
    monkeypatch.setattr(sync_client, "_download", lambda *_: reader_body)
    result = sync_client.pull(p=home)
    assert (root / reader.path).read_bytes() == reader_body
    if edited:
        assert legacy_target.read_bytes() == b"local edit\n"
        assert result["conflict_paths"] == [legacy.path]
    else:
        assert not legacy_target.exists()


def test_forward_transition_stages_before_writing_and_resumes_after_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed B download cannot change A; a partial install is reusable on retry."""
    monkeypatch.setenv("MEDLEARN_HOME", str(tmp_path / "home"))
    root = vault(tmp_path)
    config = sync_client.configure("https://example.test", root)
    home = sync_client.paths()
    generation_a = "presentation_" + "a" * 32
    generation_b = "presentation_" + "b" * 32
    old_body, replace_body, create_body = b"# A\n", b"# B\n", b"# new\n"
    old = presentation_artifact("MedLearn/概念/旧.md", old_body, generation_a)
    replace = presentation_artifact("MedLearn/概念/旧.md", replace_body, generation_b)
    create = presentation_artifact("MedLearn/概念/新.md", create_body, generation_b)
    manifest_a = presentation_manifest(generation_a, [old])
    manifest_b = presentation_manifest(generation_b, [replace, create], generation_a)
    target = root / old.path
    target.parent.mkdir(parents=True)
    target.write_bytes(old_body)
    seed_ready_state(config, home, manifest_a)
    monkeypatch.setattr(sync_client, "load_token", lambda _: "x" * 32)
    monkeypatch.setattr(sync_client, "_manifest", lambda *_: (manifest_b, ETAG, "downloaded"))
    calls = 0

    def fail_second_download(*_: object) -> bytes:
        nonlocal calls
        calls += 1
        if calls == 1:
            return replace_body
        raise SyncError("SYNC_NETWORK_FAILURE")

    monkeypatch.setattr(sync_client, "_download", fail_second_download)
    with pytest.raises(SyncError, match="SYNC_NETWORK_FAILURE"):
        sync_client.pull(p=home)
    assert target.read_bytes() == old_body
    assert not (root / create.path).exists()
    state_after_download_failure = sync_client.load_state(config, home, required=True)
    assert state_after_download_failure.presentation_generation_id == generation_a

    installed: list[str] = []

    def download(*args: object) -> bytes:
        item = args[2]
        assert isinstance(item, ManifestArtifact)
        installed.append(item.path)
        return {replace.path: replace_body, create.path: create_body}[item.path]

    monkeypatch.setattr(sync_client, "_download", download)
    original_replace = sync_client._atomic_replace
    interrupted = False

    def replace_once(*args: object) -> None:
        nonlocal interrupted
        original_replace(*args)  # type: ignore[arg-type]
        if not interrupted:
            interrupted = True
            raise SyncError("SYNC_LOCAL_WRITE_FAILURE")

    monkeypatch.setattr(sync_client, "_atomic_replace", replace_once)
    with pytest.raises(SyncError, match="SYNC_LOCAL_WRITE_FAILURE"):
        sync_client.pull(p=home)
    assert target.read_bytes() == replace_body
    state_after_install_failure = sync_client.load_state(config, home, required=True)
    assert state_after_install_failure.presentation_generation_id == generation_a
    monkeypatch.setattr(sync_client, "_atomic_replace", original_replace)
    installed.clear()
    result = sync_client.pull(p=home)
    assert result["unchanged_count"] == 2
    assert installed == []
    state_after_retry = sync_client.load_state(config, home, required=True)
    assert state_after_retry.presentation_generation_id == generation_b


def test_forward_state_commit_precedes_cleanup_and_preserves_all_local_conflicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEDLEARN_HOME", str(tmp_path / "home"))
    root = vault(tmp_path)
    config = sync_client.configure("https://example.test", root)
    home = sync_client.paths()
    generation_a = "presentation_" + "a" * 32
    generation_b = "presentation_" + "b" * 32
    same_body, old_body, new_body = b"# same\n", b"# old\n", b"# new\n"
    edited_old, stale, stale_edited = b"# edited old\n", b"# stale\n", b"# stale edited\n"
    same_a = presentation_artifact("MedLearn/概念/相同.md", same_body, generation_a)
    old_a = presentation_artifact("MedLearn/概念/替换.md", old_body, generation_a)
    edited_a = presentation_artifact("MedLearn/概念/保留.md", old_body, generation_a)
    stale_a = presentation_artifact("MedLearn/概念/过期.md", stale, generation_a)
    stale_edit_a = presentation_artifact("MedLearn/概念/过期已编辑.md", stale, generation_a)
    manifest_a = presentation_manifest(
        generation_a, [same_a, old_a, edited_a, stale_a, stale_edit_a]
    )
    same_b = presentation_artifact("MedLearn/概念/相同.md", same_body, generation_b)
    replace_b = presentation_artifact("MedLearn/概念/替换.md", new_body, generation_b)
    edited_b = presentation_artifact("MedLearn/概念/保留.md", new_body, generation_b)
    create_b = presentation_artifact("MedLearn/概念/新增.md", new_body, generation_b)
    collision_b = presentation_artifact("MedLearn/概念/碰撞.md", new_body, generation_b)
    manifest_b = presentation_manifest(
        generation_b, [collision_b, create_b, edited_b, replace_b, same_b], generation_a
    )
    for item, body in (
        (same_a, same_body),
        (old_a, old_body),
        (edited_a, edited_old),
        (stale_a, stale),
        (stale_edit_a, stale_edited),
    ):
        target = root / item.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    collision_target = root / collision_b.path
    collision_target.parent.mkdir(parents=True, exist_ok=True)
    collision_target.write_bytes(b"# user collision\n")
    seed_ready_state(config, home, manifest_a)
    monkeypatch.setattr(sync_client, "load_token", lambda _: "x" * 32)
    monkeypatch.setattr(sync_client, "_manifest", lambda *_: (manifest_b, ETAG, "downloaded"))
    monkeypatch.setattr(
        sync_client,
        "_download",
        lambda _, __, item, ___, ____: {
            replace_b.path: new_body,
            create_b.path: new_body,
        }[item.path],
    )
    result = sync_client.pull(p=home)
    assert (root / replace_b.path).read_bytes() == new_body
    assert (root / same_b.path).read_bytes() == same_body
    assert (root / create_b.path).read_bytes() == new_body
    assert (root / edited_b.path).read_bytes() == edited_old
    assert collision_target.read_bytes() == b"# user collision\n"
    assert not (root / stale_a.path).exists()
    assert (root / stale_edit_a.path).read_bytes() == stale_edited
    assert result["conflict_paths"] == sorted([collision_b.path, edited_b.path, stale_edit_a.path])
    state_b = sync_client.load_state(config, home, required=True)
    assert state_b.presentation_generation_id == generation_b
    assert set(state_b.managed_artifacts) == {same_b.path, replace_b.path, create_b.path}
    assert state_b.unresolved_conflict_paths == result["conflict_paths"]
    assert set(state_b.pending_cleanup_artifacts) == {stale_edit_a.path}

    monkeypatch.setattr(sync_client, "_manifest", lambda *_: (manifest_b, ETAG, "not_modified"))
    repeat = sync_client.pull(p=home)
    assert repeat["downloaded_count"] == 0
    assert (root / edited_b.path).read_bytes() == edited_old
    assert collision_target.read_bytes() == b"# user collision\n"
    with pytest.raises(SyncError, match="SYNC_PRESENTATION_ROLLBACK"):
        sync_client._check_rollback(state_b, manifest_a)


def test_state_write_and_cleanup_failures_leave_a_retryable_b_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEDLEARN_HOME", str(tmp_path / "home"))
    root = vault(tmp_path)
    config = sync_client.configure("https://example.test", root)
    home = sync_client.paths()
    generation_a = "presentation_" + "a" * 32
    generation_b = "presentation_" + "b" * 32
    old_body, new_body = b"# A\n", b"# B\n"
    old = presentation_artifact("MedLearn/概念/条目.md", old_body, generation_a)
    stale = presentation_artifact("MedLearn/概念/旧条目.md", old_body, generation_a)
    manifest_a = presentation_manifest(generation_a, [old, stale])
    replacement = presentation_artifact("MedLearn/概念/条目.md", new_body, generation_b)
    manifest_b = presentation_manifest(generation_b, [replacement], generation_a)
    for item in (old, stale):
        target = root / item.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(old_body)
    seed_ready_state(config, home, manifest_a)
    monkeypatch.setattr(sync_client, "load_token", lambda _: "x" * 32)
    monkeypatch.setattr(sync_client, "_manifest", lambda *_: (manifest_b, ETAG, "downloaded"))
    monkeypatch.setattr(sync_client, "_download", lambda *_: new_body)
    original_atomic_json = sync_client._atomic_json

    def fail_b_state(path: Path, model: object) -> None:
        if (
            path == home.state
            and isinstance(model, SyncState)
            and model.presentation_generation_id == generation_b
        ):
            raise SyncError("SYNC_STATE_FAILURE")
        original_atomic_json(path, model)  # type: ignore[arg-type]

    monkeypatch.setattr(sync_client, "_atomic_json", fail_b_state)
    with pytest.raises(SyncError, match="SYNC_STATE_FAILURE"):
        sync_client.pull(p=home)
    assert (root / replacement.path).read_bytes() == new_body
    assert (root / stale.path).read_bytes() == old_body
    state_after_state_failure = sync_client.load_state(config, home, required=True)
    assert state_after_state_failure.presentation_generation_id == generation_a
    monkeypatch.setattr(sync_client, "_atomic_json", original_atomic_json)

    original_cleanup = sync_client._cleanup_obsolete
    cleanup_failed = False

    def fail_cleanup(*args: object) -> tuple[dict[str, ManagedArtifact], list[str]]:
        nonlocal cleanup_failed
        if not cleanup_failed:
            cleanup_failed = True
            raise SyncError("SYNC_LOCAL_WRITE_FAILURE")
        return original_cleanup(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(sync_client, "_cleanup_obsolete", fail_cleanup)
    with pytest.raises(SyncError, match="SYNC_LOCAL_WRITE_FAILURE"):
        sync_client.pull(p=home)
    state_after_cleanup_failure = sync_client.load_state(config, home, required=True)
    assert state_after_cleanup_failure.presentation_generation_id == generation_b
    assert (root / stale.path).read_bytes() == old_body
    monkeypatch.setattr(sync_client, "_cleanup_obsolete", original_cleanup)
    result = sync_client.pull(p=home)
    assert result["downloaded_count"] == 0
    assert not (root / stale.path).exists()


def test_manifest_accepts_canonical_document_and_maps_network_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = artifact(
        f"MedLearn/Data/Captures/{CAPTURE}.json", "application/json; charset=utf-8", b"{}\n"
    )
    body = manifest_body([item])
    config = sync_client.SyncConfig(
        endpoint="https://example.test", vault_path=str(vault(tmp_path))
    )
    opener(
        monkeypatch,
        Response(
            body,
            Content_Type="application/json; charset=utf-8",
            ETag='"' + sync_client._digest(body) + '"',
        ),
    )
    assert sync_client._manifest(config, "x" * 32, None, 1)[0].artifacts == [item]
    for failure in (URLError("dns"), TimeoutError(), OSError("tls")):
        opener(monkeypatch, failure)
        with pytest.raises(SyncError, match="SYNC_NETWORK_FAILURE"):
            sync_client._manifest(config, "x" * 32, None, 1)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (401, "SYNC_AUTH_FAILED"),
        (403, "SYNC_AUTH_FAILED"),
        (301, "SYNC_NETWORK_FAILURE"),
        (500, "SYNC_NETWORK_FAILURE"),
    ],
)
def test_manifest_http_errors_are_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int, expected: str
) -> None:
    opener(monkeypatch, HTTPError("https://example.test", code, "failed", Message(), io.BytesIO()))
    config = sync_client.SyncConfig(
        endpoint="https://example.test", vault_path=str(vault(tmp_path))
    )
    with pytest.raises(SyncError, match=expected):
        sync_client._manifest(config, "x" * 32, None, 1)


@pytest.mark.parametrize(
    "body",
    [
        b"\xef\xbb\xbf{}\n",
        b"{}\r\n",
        b"{}",
        b"{}\n\n",
        b'{ "artifacts":[],"manifest_version":"0.1.0"}\n',
    ],
)
def test_manifest_rejects_noncanonical_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    opener(
        monkeypatch,
        Response(
            body,
            Content_Type="application/json; charset=utf-8",
            ETag='"' + sync_client._digest(body) + '"',
        ),
    )
    config = sync_client.SyncConfig(
        endpoint="https://example.test", vault_path=str(vault(tmp_path))
    )
    with pytest.raises(SyncError):
        sync_client._manifest(config, "x" * 32, None, 1)


def test_manifest_handler_rejects_invalid_models_and_integrity_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = artifact(
        f"MedLearn/Captures/2026/07/{CAPTURE}.md", "text/markdown; charset=utf-8", b"# capture\n"
    )
    second = artifact(
        f"MedLearn/Data/Captures/{CAPTURE}.json", "application/json; charset=utf-8", b"{}\n"
    )
    base = Manifest(manifest_version="0.1.0", artifacts=[first, second]).model_dump(mode="json")
    variants: list[dict[str, object]] = []
    wrong_version = dict(base)
    wrong_version["manifest_version"] = "0.2.0"
    variants.append(wrong_version)
    extra_top = dict(base)
    extra_top["extra"] = True
    variants.append(extra_top)
    extra_artifact = json.loads(json.dumps(base))
    extra_artifact["artifacts"][0]["extra"] = True
    variants.append(extra_artifact)
    duplicate = dict(base)
    duplicate["artifacts"] = [base["artifacts"][0], base["artifacts"][0]]
    variants.append(duplicate)
    unsorted = dict(base)
    unsorted["artifacts"] = list(reversed(base["artifacts"]))
    variants.append(unsorted)
    for path, value in (("content_digest", "sha256:bad"), ("byte_length", 1.5), ("path", "../bad")):
        invalid = json.loads(json.dumps(base))
        invalid["artifacts"][0][path] = value
        variants.append(invalid)
    invalid_month = json.loads(json.dumps(base))
    invalid_month["artifacts"][0]["path"] = f"MedLearn/Captures/2026/13/{CAPTURE}.md"
    variants.append(invalid_month)
    config = sync_client.SyncConfig(
        endpoint="https://example.test", vault_path=str(vault(tmp_path))
    )
    for document in variants:
        body = json.dumps(document, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        opener(
            monkeypatch,
            Response(
                body,
                Content_Type="application/json; charset=utf-8",
                ETag='"' + sync_client._digest(body) + '"',
            ),
        )
        with pytest.raises(SyncError, match="SYNC_MANIFEST_PROTOCOL_ERROR"):
            sync_client._manifest(config, "x" * 32, None, 1)
    valid = manifest_body([first, second])
    for headers in (
        {"Content_Type": "application/json", "ETag": '"' + sync_client._digest(valid) + '"'},
        {"Content_Type": "application/json; charset=utf-8", "ETag": '"sha256:bad"'},
    ):
        opener(monkeypatch, Response(valid, **headers))
        with pytest.raises(SyncError, match="SYNC_MANIFEST_INTEGRITY_FAILURE"):
            sync_client._manifest(config, "x" * 32, None, 1)
    huge = b"x" * (sync_client.MAX_MANIFEST + 1)
    opener(
        monkeypatch,
        Response(
            huge,
            Content_Type="application/json; charset=utf-8",
            ETag='"' + sync_client._digest(huge) + '"',
        ),
    )
    with pytest.raises(SyncError, match="SYNC_MANIFEST_PROTOCOL_ERROR"):
        sync_client._manifest(config, "x" * 32, None, 1)


def test_manifest_304_requires_matching_existing_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = artifact(
        f"MedLearn/Data/Captures/{CAPTURE}.json", "application/json; charset=utf-8", b"{}\n"
    )
    state = SyncState(
        endpoint="https://example.test",
        vault_path=str(vault(tmp_path)),
        manifest_etag='"sha256:' + "c" * 64 + '"',
        manifest_artifacts=[item],
        managed_artifacts={},
    )
    config = sync_client.SyncConfig(endpoint=state.endpoint, vault_path=state.vault_path)
    error = HTTPError(config.endpoint, 304, "not modified", Message(), io.BytesIO())
    error.headers["ETag"] = state.manifest_etag
    opener(monkeypatch, error)
    assert sync_client._manifest(config, "x" * 32, state, 1)[2] == "not_modified"
    for missing_state, body in ((None, b""), (state, b"unexpected")):
        error = HTTPError(config.endpoint, 304, "not modified", Message(), io.BytesIO(body))
        error.headers["ETag"] = state.manifest_etag
        opener(monkeypatch, error)
        with pytest.raises(SyncError, match="SYNC_MANIFEST_PROTOCOL_ERROR"):
            sync_client._manifest(config, "x" * 32, missing_state, 1)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (401, "SYNC_AUTH_FAILED"),
        (403, "SYNC_AUTH_FAILED"),
        (302, "SYNC_NETWORK_FAILURE"),
        (500, "SYNC_NETWORK_FAILURE"),
    ],
)
def test_artifact_http_errors_are_stable(
    monkeypatch: pytest.MonkeyPatch, code: int, expected: str
) -> None:
    item = artifact(
        f"MedLearn/Data/Captures/{CAPTURE}.json", "application/json; charset=utf-8", b"{}\n"
    )
    opener(monkeypatch, HTTPError("https://example.test", code, "failed", Message(), io.BytesIO()))
    config = sync_client.SyncConfig(endpoint="https://example.test", vault_path="C:/vault")
    with pytest.raises(SyncError, match=expected):
        sync_client._download(config, "x" * 32, item, 1, 0)


@pytest.mark.parametrize("directory", [False, True])
def test_reparse_target_is_not_downgraded_to_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, directory: bool
) -> None:
    monkeypatch.setenv("MEDLEARN_HOME", str(tmp_path / "home"))
    root = vault(tmp_path)
    sync_client.configure("https://example.test", root)
    item = artifact(
        f"MedLearn/Data/Captures/{CAPTURE}.json", "application/json; charset=utf-8", b"{}\n"
    )
    target = root / item.path
    target.parent.mkdir(parents=True)
    monkeypatch.setattr(sync_client, "load_token", lambda _: "x" * 32)
    monkeypatch.setattr(
        sync_client,
        "_manifest",
        lambda *_: (Manifest(manifest_version="0.1.0", artifacts=[item]), ETAG, "downloaded"),
    )
    sync_client.pull(dry_run=True, p=sync_client.paths())
    outside = tmp_path / "outside"
    if directory:
        outside.mkdir()
    else:
        outside.write_bytes(b"{}\n")
    try:
        target.symlink_to(outside, target_is_directory=directory)
    except OSError:
        pytest.skip("symlink privilege unavailable")
    with pytest.raises(SyncError, match="SYNC_LOCAL_PATH_UNSAFE"):
        sync_client.pull(confirm_first_pull=True, p=sync_client.paths())


def test_parent_reparse_mock_is_not_treated_as_a_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = vault(tmp_path)
    item = artifact(
        f"MedLearn/Data/Captures/{CAPTURE}.json", "application/json; charset=utf-8", b"{}\n"
    )
    parent = root / "MedLearn" / "Data" / "Captures"
    parent.mkdir(parents=True)
    actual = sync_client._is_reparse
    monkeypatch.setattr(sync_client, "_is_reparse", lambda path: path == parent or actual(path))
    with pytest.raises(SyncError, match="SYNC_LOCAL_PATH_UNSAFE"):
        sync_client._target(root, item)


@pytest.mark.skipif(sys.platform != "win32", reason="requires msvcrt")
def test_windows_nonblocking_lock_contention(tmp_path: Path) -> None:
    lock = tmp_path / "sync.lock"
    lock.write_bytes(b"0")
    ready = tmp_path / "ready"
    script = (
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        "from medlearn_vault.sync_client import _lock\n"
        "with _lock(Path(sys.argv[1])):\n"
        "    Path(sys.argv[2]).write_text('ready')\n"
        "    time.sleep(2)\n"
    )
    process = subprocess.Popen([sys.executable, "-c", script, str(lock), str(ready)])
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists()
        with pytest.raises(SyncError, match="SYNC_ALREADY_RUNNING"):
            with sync_client._lock(lock):
                pass
    finally:
        process.wait(timeout=5)
    with sync_client._lock(lock):
        pass


def test_directory_conflict_reports_manifest_relative_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEDLEARN_HOME", str(tmp_path / "home"))
    root = vault(tmp_path)
    sync_client.configure("https://example.test", root)
    item = artifact(
        f"MedLearn/Data/Captures/{CAPTURE}.json", "application/json; charset=utf-8", b"{}\n"
    )
    (root / item.path).mkdir(parents=True)
    monkeypatch.setattr(sync_client, "load_token", lambda _: "x" * 32)
    monkeypatch.setattr(
        sync_client,
        "_manifest",
        lambda *_: (Manifest(manifest_version="0.1.0", artifacts=[item]), ETAG, "downloaded"),
    )
    sync_client.pull(dry_run=True, p=sync_client.paths())
    result = sync_client.pull(confirm_first_pull=True, p=sync_client.paths())
    assert result["conflict_paths"] == [item.path]
