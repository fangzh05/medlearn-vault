"""Immutable, create-only medlearn-vault writer.

This module consumes a verified VaultPublicationPlan and writes its exact
planned bytes to the medlearn-vault R2 bucket. It never regenerates,
re-renders, or re-materializes artifact content; it only verifies identities
and digests before writing the exact planned bytes.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Protocol

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from medlearn_vault.domain.base import DomainModel
from medlearn_vault.publication import (
    DIGEST_PATTERN,
    PLAN_ID_PATTERN,
    VaultPublicationPlan,
    canonical_publication_plan_json,
    publication_plan_object_digest,
)
from medlearn_vault.workflow import (
    JOB_ID_PATTERN,
    ApprovalAttestor,
    ReadOnlyObjectStore,
    WorkflowError,
)

VAULT_BUCKET = "medlearn-vault"


class VaultStoredObject(DomainModel):
    body: bytes
    etag: str
    content_type: str


class VaultObjectStore(Protocol):
    """Read-only-from-existing + create-only boundary for medlearn-vault R2.

    Only get and create are allowed; no overwrite, delete, list, rename,
    or compare-and-swap is exposed.
    """

    def get(self, key: str) -> VaultStoredObject | None: ...

    def create(
        self, key: str, body: bytes, *, content_type: str
    ) -> bool: ...


class S3VaultObjectStore:
    """R2 S3 adapter fixed to the medlearn-vault bucket.

    Credentials are taken from VAULT_R2_* environment variables and may
    never be shared with the medlearn-control adapter.
    """

    def __init__(
        self,
        endpoint: str,
        access_key_id: str,
        secret_access_key: str,
    ) -> None:
        if not endpoint or not access_key_id or not secret_access_key:
            raise WorkflowError("VAULT_SERVICE_MISCONFIGURED")
        self._client: Any = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name="auto",
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
            ),
        )

    def get(self, key: str) -> VaultStoredObject | None:
        try:
            response = self._client.get_object(
                Bucket=VAULT_BUCKET, Key=key
            )
            return VaultStoredObject(
                body=response["Body"].read(),
                etag=response["ETag"],
                content_type=response.get(
                    "ContentType", "application/octet-stream"
                ),
            )
        except ClientError as exc:
            if (
                exc.response.get("ResponseMetadata", {}).get(
                    "HTTPStatusCode"
                )
                == 404
            ):
                return None
            raise WorkflowError("VAULT_STORE_FAILURE") from exc
        except Exception as exc:
            raise WorkflowError("VAULT_STORE_FAILURE") from exc

    def create(
        self, key: str, body: bytes, *, content_type: str
    ) -> bool:
        try:
            self._client.put_object(
                Bucket=VAULT_BUCKET,
                Key=key,
                Body=body,
                ContentType=content_type,
                IfNoneMatch="*",
            )
            return True
        except ClientError as exc:
            if (
                exc.response.get("ResponseMetadata", {}).get(
                    "HTTPStatusCode"
                )
                in {409, 412}
            ):
                return False
            raise WorkflowError("VAULT_STORE_FAILURE") from exc
        except Exception as exc:
            raise WorkflowError("VAULT_STORE_FAILURE") from exc


class VaultPublicationResult(DomainModel):
    """Sanitized result of one create-only vault-publication operation."""

    publication_plan_id: str
    publication_plan_object_digest: str
    capture_id: str
    created_paths: tuple[str, ...] = ()
    reused_paths: tuple[str, ...] = ()


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class VaultPublicationWriter:
    """Read one verified VaultPublicationPlan from medlearn-control, attest
    its provenance, then create-only write its exact artifact bytes to
    medlearn-vault.
    """

    def __init__(
        self,
        control_store: ReadOnlyObjectStore,
        vault_store: VaultObjectStore,
    ) -> None:
        self.control_store = control_store
        self.vault_store = vault_store

    def run(
        self,
        publication_plan_id: str,
        expected_publication_plan_object_digest: str,
        source_job_id: str,
    ) -> VaultPublicationResult:
        # ── 1. Validate input formats before any I/O ──
        if (
            re.fullmatch(PLAN_ID_PATTERN, publication_plan_id) is None
            or re.fullmatch(DIGEST_PATTERN, expected_publication_plan_object_digest) is None
            or re.fullmatch(JOB_ID_PATTERN, source_job_id) is None
        ):
            raise WorkflowError("INVALID_VAULT_PUBLICATION_INPUT")

        # ── 2. Read plan from medlearn-control ──
        plan_key = f"v1/publication-plans/{publication_plan_id}.json"
        try:
            plan_stored = self.control_store.get(plan_key)
        except WorkflowError:
            raise
        except Exception as exc:
            raise WorkflowError("CONTROL_STORE_FAILURE") from exc
        if plan_stored is None:
            raise WorkflowError("PUBLICATION_PLAN_NOT_FOUND")

        # ── 3. Validate stored plan bytes ──
        if _sha256(plan_stored.body) != expected_publication_plan_object_digest:
            raise WorkflowError("PUBLICATION_PLAN_OBJECT_DIGEST_MISMATCH")
        try:
            plan = VaultPublicationPlan.model_validate_json(plan_stored.body)
        except Exception as exc:
            raise WorkflowError("INVALID_PUBLICATION_PLAN") from exc
        if canonical_publication_plan_json(plan) != plan_stored.body:
            raise WorkflowError("INVALID_PUBLICATION_PLAN")
        if plan.publication_plan_id != publication_plan_id:
            raise WorkflowError("INVALID_PUBLICATION_PLAN")

        # The plan's own model-validator already checks artifacts,
        # capture_id, paths, digests, byte_lengths, identity, and ordering.
        # Recompute plan digest for final consistency.
        plan_digest = publication_plan_object_digest(plan)
        if plan_digest != expected_publication_plan_object_digest:
            raise WorkflowError("PUBLICATION_PLAN_OBJECT_DIGEST_MISMATCH")

        # ── 4. Fresh provenance attestation ──
        try:
            attestation = ApprovalAttestor(self.control_store).run(
                plan.approval_id,
                source_job_id,
                plan.proposal_id,
                plan.proposal_object_digest,
                plan.base_bundle_digest,
                expected_decision="approved",
                expected_rejection_code=None,
                expected_approval_object_digest=plan.approval_object_digest,
            )
        except WorkflowError:
            raise
        except Exception as exc:
            raise WorkflowError("CONTROL_STORE_FAILURE") from exc

        # Cross-check attestation result against plan fields
        if (
            attestation.approval_id != plan.approval_id
            or attestation.approval_object_digest != plan.approval_object_digest
            or attestation.proposal_id != plan.proposal_id
            or attestation.proposal_object_digest != plan.proposal_object_digest
            or attestation.proposal_semantic_digest != plan.proposal_semantic_digest
            or attestation.review_digest != plan.review_digest
        ):
            raise WorkflowError("PUBLICATION_PLAN_PROVENANCE_MISMATCH")

        # ── 5. Write artifacts in fixed plan order ──
        created_paths: list[str] = []
        reused_paths: list[str] = []

        for artifact in plan.artifacts:
            key = artifact.path
            body = artifact.content_utf8.encode("utf-8")
            content_type = artifact.media_type

            try:
                if self.vault_store.create(key, body, content_type=content_type):
                    created_paths.append(key)
                    continue
            except WorkflowError:
                raise
            except Exception as exc:
                raise WorkflowError("VAULT_STORE_FAILURE") from exc

            # Key exists — verify winner matches exactly
            try:
                winner = self.vault_store.get(key)
            except WorkflowError:
                raise
            except Exception as exc:
                raise WorkflowError("VAULT_STORE_FAILURE") from exc
            if winner is None:
                raise WorkflowError("VAULT_STORE_FAILURE")
            if winner.body != body or winner.content_type != content_type:
                raise WorkflowError("VAULT_ARTIFACT_CONFLICT")
            reused_paths.append(key)

        return VaultPublicationResult(
            publication_plan_id=plan.publication_plan_id,
            publication_plan_object_digest=plan_digest,
            capture_id=plan.capture_id,
            created_paths=tuple(created_paths),
            reused_paths=tuple(reused_paths),
        )
