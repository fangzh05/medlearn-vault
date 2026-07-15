"""Strict, token-free models for the Windows read-only sync client."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DIGEST_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
CAPTURE_RE = re.compile(r"^capture_[a-f0-9]{32}$")
PLAN_RE = re.compile(r"^publication_plan_[a-f0-9]{32}$")
PRESENTATION_RE = re.compile(r"^presentation_[a-f0-9]{32}$")
JSON_MEDIA = "application/json; charset=utf-8"
MARKDOWN_MEDIA = "text/markdown; charset=utf-8"


def _safe_managed_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return (
        path.startswith("MedLearn/")
        and "\\" not in path
        and "\x00" not in path
        and "%" not in path
        and "//" not in path
        and not path.startswith("/")
        and all(part not in {".", ".."} for part in parts)
    )


class SyncError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SyncConfig(StrictModel):
    config_version: Literal["0.1.0"] = "0.1.0"
    endpoint: str
    vault_path: str


class ManifestArtifact(StrictModel):
    path: str
    media_type: str
    content_digest: str
    byte_length: int = Field(gt=0)
    capture_id: str | None = None
    publication_plan_id: str | None = None
    presentation_generation_id: str | None = None
    # Manifest 0.2 carries this server-side lookup key. The client never
    # writes or renders it; file requests remain addressed by public path.
    storage_key: str | None = None

    @model_validator(mode="after")
    def valid_artifact(self) -> ManifestArtifact:
        if (
            not _safe_managed_path(self.path)
            or not DIGEST_RE.fullmatch(self.content_digest)
        ):
            raise ValueError("invalid manifest artifact")
        json_path = f"MedLearn/Data/Captures/{self.capture_id}.json" if self.capture_id else ""
        markdown = (
            re.fullmatch(
                rf"MedLearn/Captures/(\d{{4}})/(\d{{2}})/{re.escape(self.capture_id)}\.md",
                self.path,
            )
            if self.capture_id
            else None
        )
        if (
            self.capture_id is not None
            and self.publication_plan_id is not None
            and self.presentation_generation_id is None
            and not CAPTURE_RE.fullmatch(self.capture_id)
        ) or (
            self.publication_plan_id is not None and not PLAN_RE.fullmatch(self.publication_plan_id)
        ):
            raise ValueError("invalid manifest artifact")
        if self.capture_id is not None and self.media_type == JSON_MEDIA and self.path == json_path:
            return self
        if (
            self.capture_id is not None
            and self.media_type == MARKDOWN_MEDIA
            and markdown
            and 1 <= int(markdown.group(2)) <= 12
        ):
            return self
        if (
            self.presentation_generation_id is not None
            and PRESENTATION_RE.fullmatch(self.presentation_generation_id)
            and self.capture_id is None
            and self.publication_plan_id is None
            and self.media_type == MARKDOWN_MEDIA
            and (
                self.path.startswith("MedLearn/学习记录/") or self.path.startswith("MedLearn/概念/")
            )
            and self.path.endswith(".md")
        ):
            return self
        raise ValueError("manifest artifact path or media type mismatch")


class Manifest(StrictModel):
    manifest_version: Literal["0.1.0", "0.2.0"]
    presentation_generation_id: str | None = None
    presentation_receipt_digest: str | None = None
    previous_generation_id: str | None = None
    artifacts: list[ManifestArtifact]

    @model_validator(mode="after")
    def valid_manifest(self) -> Manifest:
        if self.manifest_version not in {"0.1.0", "0.2.0"} or len(self.artifacts) > 10000:
            raise ValueError("unsupported manifest")
        if self.manifest_version == "0.1.0" and any(
            item.capture_id is None
            or item.publication_plan_id is None
            or item.presentation_generation_id is not None
            for item in self.artifacts
        ):
            raise ValueError("legacy manifest contains presentation artifact")
        if self.manifest_version == "0.2.0" and any(
            item.presentation_generation_id is None
            or item.capture_id is not None
            or item.publication_plan_id is not None
            for item in self.artifacts
        ):
            raise ValueError("presentation manifest contains legacy artifact")
        if self.manifest_version == "0.2.0" and (
            self.presentation_generation_id is None
            or not PRESENTATION_RE.fullmatch(self.presentation_generation_id)
            or self.presentation_receipt_digest is None
            or not DIGEST_RE.fullmatch(self.presentation_receipt_digest)
            or (
                self.previous_generation_id is not None
                and not PRESENTATION_RE.fullmatch(self.previous_generation_id)
            )
            or any(
                item.presentation_generation_id != self.presentation_generation_id
                for item in self.artifacts
            )
        ):
            raise ValueError("invalid presentation manifest identity")
        if self.manifest_version == "0.1.0" and any(
            value is not None
            for value in (
                self.presentation_generation_id,
                self.presentation_receipt_digest,
                self.previous_generation_id,
            )
        ):
            raise ValueError("legacy manifest has presentation identity")
        paths = [artifact.path for artifact in self.artifacts]
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise ValueError("manifest paths are not unique and sorted")
        return self


class ManagedArtifact(StrictModel):
    content_digest: str
    media_type: str
    byte_length: int = Field(gt=0)


class SyncState(StrictModel):
    state_version: Literal["0.1.0", "0.2.0"] = "0.2.0"
    endpoint: str
    vault_path: str
    manifest_etag: str
    manifest_version: Literal["0.1.0", "0.2.0"] = "0.1.0"
    presentation_generation_id: str | None = None
    presentation_receipt_digest: str | None = None
    previous_generation_id: str | None = None
    manifest_artifacts: list[ManifestArtifact]
    managed_artifacts: dict[str, ManagedArtifact]
    unresolved_conflict_paths: list[str] = Field(default_factory=list)
    pending_cleanup_artifacts: dict[str, ManagedArtifact] = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_state(self) -> SyncState:
        if not re.fullmatch(r'"sha256:[a-f0-9]{64}"', self.manifest_etag):
            raise ValueError("invalid manifest ETag")
        manifest = Manifest(
            manifest_version=self.manifest_version,
            presentation_generation_id=self.presentation_generation_id,
            presentation_receipt_digest=self.presentation_receipt_digest,
            previous_generation_id=self.previous_generation_id,
            artifacts=self.manifest_artifacts,
        )
        artifacts = {artifact.path: artifact for artifact in manifest.artifacts}
        for path, managed in self.managed_artifacts.items():
            artifact = artifacts.get(path)
            if artifact is None or (
                managed.content_digest != artifact.content_digest
                or managed.media_type != artifact.media_type
                or managed.byte_length != artifact.byte_length
            ):
                raise ValueError("managed artifact does not match manifest")
        known_paths = set(artifacts)
        if (
            self.unresolved_conflict_paths != sorted(set(self.unresolved_conflict_paths))
            or any(not _safe_managed_path(path) for path in self.unresolved_conflict_paths)
            or any(path in known_paths for path in self.pending_cleanup_artifacts)
            or any(not _safe_managed_path(path) for path in self.pending_cleanup_artifacts)
        ):
            raise ValueError("invalid sync recovery state")
        return self


class RolloutState(StrictModel):
    """Token-free state used to make a production first pull explicit."""

    rollout_version: Literal["0.1.0"] = "0.1.0"
    endpoint: str
    vault_path: str
    dry_run_succeeded: bool = False
    first_pull_completed: bool = False
