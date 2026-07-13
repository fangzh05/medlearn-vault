import hashlib
from pathlib import Path

import pytest

from medlearn_vault import sync_client
from medlearn_vault.sync_models import Manifest, ManifestArtifact, SyncError

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


def vault(tmp_path: Path) -> Path:
    result = tmp_path / "知识库"
    (result / ".obsidian").mkdir(parents=True, exist_ok=True)
    return result


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
    first = sync_client.pull(p=sync_client.paths())
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
    result = sync_client.pull(p=sync_client.paths())
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
