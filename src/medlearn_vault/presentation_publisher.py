"""Complete-snapshot publisher for rebuildable Obsidian presentation generations."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Protocol, cast

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from pydantic import Field, model_validator

from medlearn_vault.bundle import ContractBundle
from medlearn_vault.domain.base import DomainModel
from medlearn_vault.domain.learner import LearningCapture
from medlearn_vault.presentation import (
    MARKDOWN_MEDIA,
    PRESENTATION_CONTRACT_VERSION,
    PRESENTATION_RENDERER_VERSION,
    build_presentation,
)
from medlearn_vault.publication import (
    VaultPublicationReceipt,
    canonical_vault_publication_receipt_json,
)
from medlearn_vault.workflow import WorkflowError

GENERATION_RE = r"^presentation_[a-f0-9]{32}$"
DIGEST_RE = r"^sha256:[a-f0-9]{64}$"


def _bytes(value: DomainModel) -> bytes:
    return (
        json.dumps(
            value.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        + b"\n"
    )


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


class PresentationArtifact(DomainModel):
    path: str
    storage_key: str
    media_type: str = MARKDOWN_MEDIA
    content_digest: str = Field(pattern=DIGEST_RE)
    byte_length: int = Field(gt=0)

    @model_validator(mode="after")
    def valid(self) -> PresentationArtifact:
        if (
            not self.path.startswith(("MedLearn/学习记录/", "MedLearn/概念/"))
            or not self.path.endswith(".md")
            or any(part in {"", ".", ".."} for part in self.path.split("/"))
            or "\\" in self.path
            or not self.storage_key.startswith("v1/presentation-generations/")
        ):
            raise ValueError("invalid presentation artifact")
        return self


class PresentationGenerationReceipt(DomainModel):
    presentation_version: str = PRESENTATION_CONTRACT_VERSION
    renderer_version: str = PRESENTATION_RENDERER_VERSION
    presentation_generation_id: str = Field(pattern=GENERATION_RE)
    active_bundle_digest: str = Field(pattern=DIGEST_RE)
    publication_receipt_digests: tuple[str, ...]
    artifacts: tuple[PresentationArtifact, ...]

    @model_validator(mode="after")
    def valid(self) -> PresentationGenerationReceipt:
        if (
            self.presentation_version != PRESENTATION_CONTRACT_VERSION
            or self.renderer_version != PRESENTATION_RENDERER_VERSION
        ):
            raise ValueError("unsupported presentation receipt")
        if tuple(sorted(self.publication_receipt_digests)) != self.publication_receipt_digests:
            raise ValueError("presentation receipt digests are unsorted")
        paths = [item.path for item in self.artifacts]
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise ValueError("presentation paths are not unique and sorted")
        return self


class PresentationCurrentPointer(DomainModel):
    pointer_version: str = "1.0.0"
    active_presentation_generation_id: str = Field(pattern=GENERATION_RE)
    presentation_receipt_object_digest: str = Field(pattern=DIGEST_RE)
    previous_generation_id: str | None = Field(default=None, pattern=GENERATION_RE)


class PresentationStore(Protocol):
    def list_keys(self, prefix: str) -> tuple[str, ...]: ...
    def get(self, key: str) -> bytes | None: ...
    def create(self, key: str, body: bytes, content_type: str) -> bool: ...
    def cas(self, key: str, previous: bytes | None, body: bytes, content_type: str) -> bool: ...


class S3PresentationStore:
    """The only mutable object is the active pointer; generations stay create-only."""

    def __init__(self, endpoint: str, access_key_id: str, secret_access_key: str) -> None:
        if not endpoint or not access_key_id or not secret_access_key:
            raise WorkflowError("VAULT_SERVICE_MISCONFIGURED")
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name="auto",
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )

    def list_keys(self, prefix: str) -> tuple[str, ...]:
        try:
            paginator = self.client.get_paginator("list_objects_v2")
            return tuple(
                sorted(
                    item["Key"]
                    for page in paginator.paginate(Bucket="medlearn-vault", Prefix=prefix)
                    for item in page.get("Contents", ())
                )
            )
        except Exception as exc:
            raise WorkflowError("VAULT_STORE_FAILURE") from exc

    def get(self, key: str) -> bytes | None:
        try:
            return cast(
                bytes, self.client.get_object(Bucket="medlearn-vault", Key=key)["Body"].read()
            )
        except ClientError as exc:
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
                return None
            raise WorkflowError("VAULT_STORE_FAILURE") from exc

    def create(self, key: str, body: bytes, content_type: str) -> bool:
        try:
            self.client.put_object(
                Bucket="medlearn-vault",
                Key=key,
                Body=body,
                ContentType=content_type,
                IfNoneMatch="*",
            )
            return True
        except ClientError as exc:
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") in {409, 412}:
                return False
            raise WorkflowError("VAULT_STORE_FAILURE") from exc

    def cas(self, key: str, previous: bytes | None, body: bytes, content_type: str) -> bool:
        current = self.get(key)
        if current != previous:
            return False
        if previous is None:
            return self.create(key, body, content_type)
        try:
            # R2 evaluates this conditional update against the current ETag.
            etag = self.client.head_object(Bucket="medlearn-vault", Key=key)["ETag"]
            self.client.put_object(
                Bucket="medlearn-vault", Key=key, Body=body, ContentType=content_type, IfMatch=etag
            )
            return True
        except ClientError as exc:
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") in {409, 412}:
                return False
            raise WorkflowError("VAULT_STORE_FAILURE") from exc


def canonical_presentation_receipt_json(receipt: PresentationGenerationReceipt) -> bytes:
    return _bytes(receipt)


def presentation_receipt_digest(receipt: PresentationGenerationReceipt) -> str:
    return _digest(canonical_presentation_receipt_json(receipt))


def canonical_presentation_pointer_json(pointer: PresentationCurrentPointer) -> bytes:
    return _bytes(pointer)


class PresentationPublisher:
    """Publishes all receipt-backed captures as one immutable generation, then CAS-activates it."""

    def __init__(self, store: PresentationStore, repository_root: Path) -> None:
        self.store = store
        self.repository_root = repository_root

    def run(self, bundle_path: str) -> PresentationGenerationReceipt:
        try:
            bundle = ContractBundle.from_directory(self.repository_root / bundle_path)
            bundle_bytes = json.dumps(
                bundle.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            receipt_keys = self.store.list_keys("v1/publications/")
            captures: list[tuple[str, LearningCapture, str]] = []
            receipt_digests: list[str] = []
            for key in receipt_keys:
                body = self.store.get(key)
                if body is None:
                    raise WorkflowError("PRESENTATION_RECEIPT_NOT_FOUND")
                canonical_receipt = VaultPublicationReceipt.model_validate_json(body)
                if canonical_vault_publication_receipt_json(canonical_receipt) != body:
                    raise WorkflowError("INVALID_VAULT_PUBLICATION_RECEIPT")
                receipt_digests.append(_digest(body))
                capture_artifact = canonical_receipt.artifacts[0]
                capture_body = self.store.get(capture_artifact.path)
                if (
                    capture_body is None
                    or _digest(capture_body) != capture_artifact.content_digest
                    or len(capture_body) != capture_artifact.byte_length
                ):
                    raise WorkflowError("PRESENTATION_CAPTURE_INTEGRITY_FAILURE")
                capture = LearningCapture.model_validate_json(capture_body)
                captures.append((canonical_receipt.capture_id, capture, _digest(body)))
            generation = build_presentation(bundle, tuple(captures))
        except WorkflowError:
            raise
        except Exception as exc:
            raise WorkflowError("INVALID_PRESENTATION_INPUT") from exc
        receipt = PresentationGenerationReceipt(
            presentation_generation_id=generation.generation_id,
            active_bundle_digest=_digest(bundle_bytes),
            publication_receipt_digests=tuple(sorted(receipt_digests)),
            artifacts=tuple(
                PresentationArtifact(
                    path=item.path,
                    storage_key=f"v1/presentation-generations/{generation.generation_id}/artifacts/{item.content_digest[7:]}.md",
                    content_digest=item.content_digest,
                    byte_length=item.byte_length,
                )
                for item in generation.artifacts
            ),
        )
        for source, artifact in zip(generation.artifacts, receipt.artifacts, strict=True):
            if not self.store.create(
                artifact.storage_key, source.content_utf8.encode(), MARKDOWN_MEDIA
            ):
                winner = self.store.get(artifact.storage_key)
                if winner != source.content_utf8.encode():
                    raise WorkflowError("PRESENTATION_ARTIFACT_CONFLICT")
        receipt_key = (
            f"v1/presentation-generations/{receipt.presentation_generation_id}/receipt.json"
        )
        receipt_body = canonical_presentation_receipt_json(receipt)
        if not self.store.create(receipt_key, receipt_body, "application/json; charset=utf-8"):
            if self.store.get(receipt_key) != receipt_body:
                raise WorkflowError("PRESENTATION_RECEIPT_CONFLICT")
        current_key = "v1/presentation-current.json"
        previous = self.store.get(current_key)
        previous_id = None
        if previous is not None:
            old = PresentationCurrentPointer.model_validate_json(previous)
            if canonical_presentation_pointer_json(old) != previous:
                raise WorkflowError("INVALID_PRESENTATION_POINTER")
            previous_id = old.active_presentation_generation_id
        pointer = PresentationCurrentPointer(
            active_presentation_generation_id=receipt.presentation_generation_id,
            presentation_receipt_object_digest=presentation_receipt_digest(receipt),
            previous_generation_id=previous_id,
        )
        if not self.store.cas(
            current_key,
            previous,
            canonical_presentation_pointer_json(pointer),
            "application/json; charset=utf-8",
        ):
            raise WorkflowError("PRESENTATION_POINTER_CONFLICT")
        return receipt
