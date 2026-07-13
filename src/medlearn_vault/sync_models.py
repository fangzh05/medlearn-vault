"""Strict, token-free models for the Windows read-only sync client."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DIGEST_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
CAPTURE_RE = re.compile(r"^capture_[a-f0-9]{32}$")
PLAN_RE = re.compile(r"^publication_plan_[a-f0-9]{32}$")
JSON_MEDIA = "application/json; charset=utf-8"
MARKDOWN_MEDIA = "text/markdown; charset=utf-8"


class SyncError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SyncConfig(StrictModel):
    config_version: Literal["0.1.0"] = "0.1.0"
    endpoint: str
    vault_path: str


class ManifestArtifact(StrictModel):
    path: str
    media_type: str
    content_digest: str
    byte_length: int = Field(gt=0)
    capture_id: str
    publication_plan_id: str

    @model_validator(mode="after")
    def valid_artifact(self) -> ManifestArtifact:
        parts = PurePosixPath(self.path).parts
        if (
            not self.path.startswith("MedLearn/")
            or "\\" in self.path
            or "\x00" in self.path
            or "%" in self.path
            or "//" in self.path
            or self.path.startswith("/")
            or any(part in {".", ".."} for part in parts)
            or not DIGEST_RE.fullmatch(self.content_digest)
            or not CAPTURE_RE.fullmatch(self.capture_id)
            or not PLAN_RE.fullmatch(self.publication_plan_id)
        ):
            raise ValueError("invalid manifest artifact")
        json_path = f"MedLearn/Data/Captures/{self.capture_id}.json"
        markdown = re.fullmatch(
            rf"MedLearn/Captures/(\d{{4}})/(\d{{2}})/{re.escape(self.capture_id)}\.md", self.path
        )
        if self.media_type == JSON_MEDIA and self.path == json_path:
            return self
        if self.media_type == MARKDOWN_MEDIA and markdown and 1 <= int(markdown.group(2)) <= 12:
            return self
        raise ValueError("manifest artifact path or media type mismatch")


class Manifest(StrictModel):
    manifest_version: str
    artifacts: list[ManifestArtifact]

    @model_validator(mode="after")
    def valid_manifest(self) -> Manifest:
        if self.manifest_version != "0.1.0" or len(self.artifacts) > 10000:
            raise ValueError("unsupported manifest")
        paths = [artifact.path for artifact in self.artifacts]
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise ValueError("manifest paths are not unique and sorted")
        return self


class ManagedArtifact(StrictModel):
    content_digest: str
    media_type: str
    byte_length: int = Field(gt=0)


class SyncState(StrictModel):
    state_version: Literal["0.1.0"] = "0.1.0"
    endpoint: str
    vault_path: str
    manifest_etag: str
    manifest_artifacts: list[ManifestArtifact]
    managed_artifacts: dict[str, ManagedArtifact]

    @model_validator(mode="after")
    def valid_state(self) -> SyncState:
        if not re.fullmatch(r'"sha256:[a-f0-9]{64}"', self.manifest_etag):
            raise ValueError("invalid manifest ETag")
        Manifest(manifest_version="0.1.0", artifacts=self.manifest_artifacts)
        return self
