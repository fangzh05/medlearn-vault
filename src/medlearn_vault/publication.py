"""Pure, deterministic Vault publication planning.  This module never writes a Vault."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Literal

from pydantic import Field, model_validator

from medlearn_vault.bundle import ContractBundle
from medlearn_vault.capture import CaptureProposal, materialize_learning_capture
from medlearn_vault.domain.base import DomainModel
from medlearn_vault.domain.learner import LearningCapture
from medlearn_vault.terminology import format_concept_label

DIGEST_PATTERN = r"^sha256:[a-f0-9]{64}$"
CAPTURE_ID_PATTERN = r"^capture_[a-f0-9]{32}$"
PLAN_ID_PATTERN = r"^publication_plan_[a-f0-9]{32}$"


def _bytes(value: object) -> bytes:
    if isinstance(value, DomainModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical_learning_capture_json(capture: LearningCapture) -> bytes:
    return _bytes(capture) + b"\n"


def capture_identity(capture: LearningCapture) -> str:
    return "capture_" + hashlib.sha256(canonical_learning_capture_json(capture)).hexdigest()[:32]


def publication_plan_identity(
    approval_id: str,
    approval_object_digest: str,
    proposal_id: str,
    proposal_object_digest: str,
    base_bundle_digest: str,
    review_digest: str,
) -> str:
    bound = {
        "approval_id": approval_id,
        "approval_object_digest": approval_object_digest,
        "base_bundle_digest": base_bundle_digest,
        "plan_version": "0.1.0",
        "proposal_id": proposal_id,
        "proposal_object_digest": proposal_object_digest,
        "review_digest": review_digest,
    }
    return "publication_plan_" + hashlib.sha256(_bytes(bound)).hexdigest()[:32]


class VaultPublicationArtifact(DomainModel):
    path: str
    media_type: str
    content_digest: str = Field(pattern=DIGEST_PATTERN)
    byte_length: int = Field(ge=1)
    content_utf8: str

    @model_validator(mode="after")
    def validate_bytes_and_path(self) -> VaultPublicationArtifact:
        # --- Raw string checks BEFORE PurePosixPath (which collapses ".", "..", and "//") ---
        if (
            not self.path.startswith("MedLearn/")
            or "\\" in self.path
            or "//" in self.path
            or "\x00" in self.path
            or "./" in self.path
            or "/." in self.path
        ):
            raise ValueError("invalid publication artifact")
        _path = PurePosixPath(self.path)
        if _path.is_absolute() or any(part in {".", ".."} for part in _path.parts):
            raise ValueError("invalid publication artifact")
        # --- Content checks: no BOM, no NUL, no CR/CRLF, exactly one LF terminator ---
        if (
            self.content_utf8.startswith("﻿")
            or "\x00" in self.content_utf8
            or "\r" in self.content_utf8
            or not self.content_utf8.endswith("\n")
            or self.content_utf8.endswith("\n\n")
        ):
            raise ValueError("invalid publication artifact")
        data = self.content_utf8.encode("utf-8")
        if self.byte_length != len(data) or self.content_digest != _digest(data):
            raise ValueError("publication artifact digest or length mismatch")
        return self


class VaultPublicationPlan(DomainModel):
    plan_version: Literal["0.1.0"]
    publication_plan_id: str = Field(pattern=PLAN_ID_PATTERN)
    approval_id: str = Field(pattern=r"^approval_[a-f0-9]{32}$")
    approval_object_digest: str = Field(pattern=DIGEST_PATTERN)
    proposal_id: str = Field(pattern=r"^proposal_[a-f0-9]{32}$")
    proposal_object_digest: str = Field(pattern=DIGEST_PATTERN)
    proposal_semantic_digest: str = Field(pattern=DIGEST_PATTERN)
    base_bundle_digest: str = Field(pattern=DIGEST_PATTERN)
    review_digest: str = Field(pattern=DIGEST_PATTERN)
    approval_decision: Literal["approved"]
    capture_id: str = Field(pattern=CAPTURE_ID_PATTERN)
    capture_object_digest: str = Field(pattern=DIGEST_PATTERN)
    artifacts: tuple[VaultPublicationArtifact, ...] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def validate_identity_and_artifacts(self) -> VaultPublicationPlan:
        # --- Identity ---
        if self.publication_plan_id != publication_plan_identity(
            self.approval_id,
            self.approval_object_digest,
            self.proposal_id,
            self.proposal_object_digest,
            self.base_bundle_digest,
            self.review_digest,
        ):
            raise ValueError("publication_plan_id does not match approved subject")

        # --- Exactly two artifacts with fixed ordering ---
        if len(self.artifacts) != 2:
            raise ValueError("publication plan must have exactly 2 artifacts")

        json_artifact = self.artifacts[0]
        md_artifact = self.artifacts[1]

        if json_artifact.media_type != "application/json; charset=utf-8":
            raise ValueError("first artifact must be JSON")
        if md_artifact.media_type != "text/markdown; charset=utf-8":
            raise ValueError("second artifact must be Markdown")

        # --- JSON artifact path ---
        if json_artifact.path != f"MedLearn/Data/Captures/{self.capture_id}.json":
            raise ValueError("JSON artifact path does not match capture_id")

        # --- JSON content self-consistency ---
        if json_artifact.content_digest != self.capture_object_digest:
            raise ValueError("capture artifact digest mismatch")

        json_bytes = json_artifact.content_utf8.encode("utf-8")
        if json_artifact.content_digest != _digest(json_bytes):
            raise ValueError("JSON artifact digest mismatch")

        computed_capture_id = "capture_" + hashlib.sha256(json_bytes).hexdigest()[:32]
        if self.capture_id != computed_capture_id:
            raise ValueError("capture_id does not match JSON artifact bytes")

        # --- Parse as LearningCapture and re-canonicalize ---
        try:
            capture = LearningCapture.model_validate_json(json_artifact.content_utf8)
        except Exception as exc:
            raise ValueError(
                "JSON artifact does not parse as LearningCapture"
            ) from exc

        recoded = canonical_learning_capture_json(capture)
        if json_bytes != recoded:
            raise ValueError("JSON artifact is not canonical LearningCapture")

        # --- Markdown artifact path from captured_at local timezone ---
        captured = capture.captured_at
        expected_md_path = (
            f"MedLearn/Captures/{captured.year:04d}/{captured.month:02d}/"
            f"{self.capture_id}.md"
        )
        if md_artifact.path != expected_md_path:
            raise ValueError(
                "Markdown artifact path does not match capture_id and captured_at"
            )

        # --- Paths must be unique ---
        if len({item.path for item in self.artifacts}) != 2:
            raise ValueError("publication artifact paths must be unique")

        return self


def canonical_publication_plan_json(plan: VaultPublicationPlan) -> bytes:
    return _bytes(plan) + b"\n"


def publication_plan_object_digest(plan: VaultPublicationPlan) -> str:
    return _digest(canonical_publication_plan_json(plan))


def _yaml(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_learning_capture_markdown(
    bundle: ContractBundle,
    capture: LearningCapture,
    *,
    capture_id: str,
    approval_id: str,
    proposal_id: str,
) -> str:
    """Render only validated persistent observations and supported correction claims."""
    front = [
        "---",
        f"medlearn_type: {_yaml('learning_capture')}",
        f"capture_id: {_yaml(capture_id)}",
        f"schema_version: {_yaml(capture.schema_version)}",
        f"approval_id: {_yaml(approval_id)}",
        f"proposal_id: {_yaml(proposal_id)}",
        f"captured_at: {_yaml(capture.captured_at.isoformat())}",
        f"discipline_id: {_yaml(str(capture.discipline_id))}",
    ]
    if capture.course_id is not None:
        front.append(f"course_id: {_yaml(str(capture.course_id))}")
    if capture.chapter_id is not None:
        front.append(f"chapter_id: {_yaml(str(capture.chapter_id))}")
    front.append("---")
    concepts = {item.concept_id: item for item in bundle.concepts if item.status == "active"}
    claims = {item.claim_id: item for item in bundle.claims}
    concept_lines = [
        f"- {format_concept_label(concepts[item.resolved_concept_id])} ({item.resolved_concept_id})"
        for item in capture.concept_mentions
        if item.resolved_concept_id in concepts
    ]
    evidence_lines = [
        f"- {item.concept_id}: {item.evidence_type} (confidence={item.confidence:g})"
        for item in capture.learner_evidence
    ]
    correction_lines: list[str] = []
    for item in capture.misconception_observations:
        correction_lines.append(f"- 观察到的错误逻辑：{item.observed_error_logic}")
        valid = [claims[cid] for cid in item.correction_claim_ids if cid in claims]
        if valid:
            for claim in valid:
                correction_lines.append(f"  - 纠正（{claim.claim_id}）：{claim.statement}")
        elif item.proposed_correction:
            correction_lines.append(f"  - 待验证建议：{item.proposed_correction}")
    question_lines = [f"- {item.text}" for item in capture.open_questions]

    def section(title: str, lines: list[str]) -> list[str]:
        return [f"## {title}", *(lines or ["- 无"])]

    return "\n".join(
        [
            *front,
            "",
            *section("概念", concept_lines),
            "",
            *section("学习表现", evidence_lines),
            "",
            *section("错误逻辑与纠正", correction_lines),
            "",
            *section("待解决问题", question_lines),
            "",
        ]
    )


def build_vault_publication_plan(
    bundle: ContractBundle,
    exact_proposal_bytes: bytes,
    exact_approval_bytes: bytes,
    review_digest: str,
) -> VaultPublicationPlan:
    """Build a plan from exact already-read control objects; no I/O or clock access."""
    from medlearn_vault.workflow import (
        ProposalApprovalRecord,
        approval_identity,
        canonical_approval_json,
    )

    if not re.fullmatch(DIGEST_PATTERN, review_digest):
        raise ValueError("INVALID_PUBLICATION_INPUT")
    try:
        approval = ProposalApprovalRecord.model_validate_json(exact_approval_bytes)
        proposal = CaptureProposal.model_validate_json(exact_proposal_bytes)
    except ValueError as exc:
        raise ValueError("INVALID_PUBLICATION_INPUT") from exc
    if canonical_approval_json(
        approval
    ) != exact_approval_bytes or approval.approval_id != approval_identity(
        approval.proposal_id, approval.proposal_object_digest, approval.expected_base_bundle_digest
    ):
        raise ValueError("INVALID_PUBLICATION_INPUT")
    if approval.decision != "approved":
        raise ValueError("PUBLICATION_NOT_APPROVED")
    proposal_digest = _digest(exact_proposal_bytes)
    if (
        proposal.proposal_id != approval.proposal_id
        or proposal_digest != approval.proposal_object_digest
        or proposal.base_bundle_digest != approval.expected_base_bundle_digest
    ):
        raise ValueError("INVALID_PUBLICATION_INPUT")
    try:
        capture = materialize_learning_capture(bundle, proposal)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    capture_bytes = canonical_learning_capture_json(capture)
    capture_id = capture_identity(capture)
    captured = capture.captured_at
    markdown = render_learning_capture_markdown(
        bundle,
        capture,
        capture_id=capture_id,
        approval_id=approval.approval_id,
        proposal_id=proposal.proposal_id,
    )
    json_path = f"MedLearn/Data/Captures/{capture_id}.json"
    markdown_path = f"MedLearn/Captures/{captured.year:04d}/{captured.month:02d}/{capture_id}.md"
    artifacts = (
        VaultPublicationArtifact(
            path=json_path,
            media_type="application/json; charset=utf-8",
            content_digest=_digest(capture_bytes),
            byte_length=len(capture_bytes),
            content_utf8=capture_bytes.decode("utf-8"),
        ),
        VaultPublicationArtifact(
            path=markdown_path,
            media_type="text/markdown; charset=utf-8",
            content_digest=_digest(markdown.encode("utf-8")),
            byte_length=len(markdown.encode("utf-8")),
            content_utf8=markdown,
        ),
    )
    return VaultPublicationPlan(
        plan_version="0.1.0",
        approval_decision="approved",
        publication_plan_id=publication_plan_identity(
            approval.approval_id,
            _digest(exact_approval_bytes),
            proposal.proposal_id,
            proposal_digest,
            approval.expected_base_bundle_digest,
            review_digest,
        ),
        approval_id=approval.approval_id,
        approval_object_digest=_digest(exact_approval_bytes),
        proposal_id=proposal.proposal_id,
        proposal_object_digest=proposal_digest,
        proposal_semantic_digest=proposal.proposal_digest,
        base_bundle_digest=approval.expected_base_bundle_digest,
        review_digest=review_digest,
        capture_id=capture_id,
        capture_object_digest=_digest(capture_bytes),
        artifacts=artifacts,
    )
