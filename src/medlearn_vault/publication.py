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
from medlearn_vault.domain.learner import LearnerEvidence, LearningCapture
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
            raise ValueError("JSON artifact does not parse as LearningCapture") from exc

        recoded = canonical_learning_capture_json(capture)
        if json_bytes != recoded:
            raise ValueError("JSON artifact is not canonical LearningCapture")

        # --- Markdown artifact path from captured_at local timezone ---
        captured = capture.captured_at
        expected_md_path = (
            f"MedLearn/Captures/{captured.year:04d}/{captured.month:02d}/{self.capture_id}.md"
        )
        if md_artifact.path != expected_md_path:
            raise ValueError("Markdown artifact path does not match capture_id and captured_at")

        # --- Paths must be unique ---
        if len({item.path for item in self.artifacts}) != 2:
            raise ValueError("publication artifact paths must be unique")

        return self


def canonical_publication_plan_json(plan: VaultPublicationPlan) -> bytes:
    return _bytes(plan) + b"\n"


def publication_plan_object_digest(plan: VaultPublicationPlan) -> str:
    return _digest(canonical_publication_plan_json(plan))


# ── Vault Publication Receipt ─────────────────────────────────────────────


class VaultPublicationReceiptArtifact(DomainModel):
    """Immutable identity of one published artifact — content is never embedded."""

    path: str
    media_type: str
    content_digest: str = Field(pattern=DIGEST_PATTERN)
    byte_length: int = Field(ge=1)


class VaultPublicationReceipt(DomainModel):
    """Deterministic, immutable proof that two artifacts were published.

    Stored at v1/publications/<publication_plan_id>.json in medlearn-vault.
    Never contains timestamps, random IDs, workflow run IDs, GitHub actors,
    mutable state, local paths, or credential information.
    """

    receipt_version: Literal["0.1.0"]
    publication_plan_id: str = Field(pattern=PLAN_ID_PATTERN)
    publication_plan_object_digest: str = Field(pattern=DIGEST_PATTERN)
    capture_id: str = Field(pattern=CAPTURE_ID_PATTERN)
    artifacts: tuple[VaultPublicationReceiptArtifact, ...] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def validate_matches_plan(self) -> VaultPublicationReceipt:
        if len(self.artifacts) != 2:
            raise ValueError("receipt must have exactly 2 artifacts")
        json_artifact = self.artifacts[0]
        md_artifact = self.artifacts[1]
        if json_artifact.media_type != "application/json; charset=utf-8":
            raise ValueError("first receipt artifact must be JSON")
        if md_artifact.media_type != "text/markdown; charset=utf-8":
            raise ValueError("second receipt artifact must be Markdown")
        if not json_artifact.path.startswith("MedLearn/Data/Captures/"):
            raise ValueError("JSON artifact path must be in MedLearn/Data/Captures/")
        if not md_artifact.path.startswith("MedLearn/Captures/"):
            raise ValueError("Markdown artifact path must be in MedLearn/Captures/")
        if json_artifact.path == md_artifact.path:
            raise ValueError("receipt artifact paths must be unique")
        return self


def _receipt_artifact_from_plan_artifact(
    artifact: VaultPublicationArtifact,
) -> VaultPublicationReceiptArtifact:
    return VaultPublicationReceiptArtifact(
        path=artifact.path,
        media_type=artifact.media_type,
        content_digest=artifact.content_digest,
        byte_length=artifact.byte_length,
    )


def build_vault_publication_receipt(plan: VaultPublicationPlan) -> VaultPublicationReceipt:
    """Build a deterministic receipt from a validated publication plan."""
    return VaultPublicationReceipt(
        receipt_version="0.1.0",
        publication_plan_id=plan.publication_plan_id,
        publication_plan_object_digest=publication_plan_object_digest(plan),
        capture_id=plan.capture_id,
        artifacts=tuple(_receipt_artifact_from_plan_artifact(a) for a in plan.artifacts),
    )


def canonical_vault_publication_receipt_json(receipt: VaultPublicationReceipt) -> bytes:
    """Canonical JSON for a receipt: UTF-8, sorted keys, compact separators, one LF."""
    return _bytes(receipt) + b"\n"


def vault_publication_receipt_object_digest(receipt: VaultPublicationReceipt) -> str:
    return _digest(canonical_vault_publication_receipt_json(receipt))


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
    """Render a deterministic mobile-first Chinese learning report (renderer v2)."""
    front = [
        "---",
        f"medlearn_type: {_yaml('learning_capture')}",
        f"capture_id: {_yaml(capture_id)}",
        f"schema_version: {_yaml(capture.schema_version)}",
        f"renderer_version: {_yaml('2.1.0')}",
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
    concepts = {item.concept_id: item for item in bundle.concepts}
    claims = {item.claim_id: item for item in bundle.claims}

    def labels(concept_id: str) -> str:
        if concept_id in concepts:
            return format_concept_label(concepts[concept_id])
        return "未收录概念"

    def explanation(concept_id: str) -> tuple[str, str, str | None]:
        """Resolve one visible explanation without blending provenance tiers."""
        definition_claims = sorted(
            (
                claim
                for claim in bundle.claims
                if claim.claim_status == "active"
                and claim.claim_type == "definition"
                and concept_id in claim.concept_ids
            ),
            key=lambda claim: claim.claim_id,
        )
        for status, label in (
            ("verified_reference", "已验证解释"),
            ("source_backed", "已验证解释"),
        ):
            claim = next(
                (item for item in definition_claims if item.verification_status == status), None
            )
            if claim is not None:
                return label, claim.statement, None
        concept = concepts.get(concept_id)
        if concept is not None and any("\u4e00" <= char <= "\u9fff" for char in concept.scope_note):
            return "目录范围说明", concept.scope_note, None
        chat = next(
            (item for item in capture.conversation_explanations if item.concept_id == concept_id),
            None,
        )
        if chat is not None:
            return "对话内解释", chat.explanation_text, "来自本次学习对话，未经外部核验。"
        generated = next(
            (item for item in capture.generated_explanations if item.concept_id == concept_id),
            None,
        )
        if generated is not None:
            return (
                "GPT 生成解释",
                generated.explanation_text,
                "由 GPT 根据概念名称和当前学习上下文生成，未经教材或指南核验。",
            )
        return "暂无解释", "暂无解释", None

    evidence_labels = {
        "correct_independent": "独立答对",
        "correct_after_hint": "提示后答对",
        "guessed_correct": "猜测答对",
        "partial": "部分掌握",
        "unknown": "尚未掌握",
        "incorrect": "回答错误",
        "high_confidence_incorrect": "高置信度错误",
        "self_report_only": "仅自我报告",
    }
    known = {"correct_independent", "correct_after_hint"}
    wrong = {"incorrect", "high_confidence_incorrect"}
    grouped: dict[str, list[LearnerEvidence]] = {"已掌握": [], "部分掌握": [], "明确错误": []}
    seen_evidence: set[tuple[str, str, str]] = set()
    for item in capture.learner_evidence:
        key = (str(item.concept_id), item.evidence_type, item.message_id)
        if key in seen_evidence:
            continue
        seen_evidence.add(key)
        target = (
            "已掌握"
            if item.evidence_type in known
            else "明确错误"
            if item.evidence_type in wrong
            else "部分掌握"
        )
        grouped[target].append(item)

    def evidence_lines(items: list[LearnerEvidence], include_excerpt: bool = False) -> list[str]:
        lines: list[str] = []
        for item in items:
            evidence = item
            lines.extend(
                [
                    f"- {labels(evidence.concept_id)}",
                    f"  - 表现：{evidence_labels[evidence.evidence_type]}",
                    f"  - 依据：{evidence.rationale}",
                ]
            )
            if include_excerpt and evidence.user_excerpt:
                lines.append(f"  - 我的原回答：{evidence.user_excerpt}")
        return lines

    correction_lines: list[str] = []
    for misconception in capture.misconception_observations:
        names = (
            "、".join(labels(concept_id) for concept_id in misconception.concept_ids) or "相关概念"
        )
        correction_lines.append(f"- {names}")
        if misconception.user_excerpt:
            correction_lines.append(f"  - 我的原回答：{misconception.user_excerpt}")
        correction_lines.append(f"  - 错误逻辑：{misconception.observed_error_logic}")
        valid = [claims[cid] for cid in misconception.correction_claim_ids if cid in claims]
        if valid:
            for claim in valid:
                correction_lines.append(f"  - 纠正：{claim.statement}")
        elif misconception.proposed_correction:
            correction_lines.append(f"  - 待验证建议：{misconception.proposed_correction}")
        else:
            correction_lines.append("  - 纠正：待验证")
    question_lines = [f"- {item.text}" for item in capture.open_questions]
    used_concept_ids = tuple(
        dict.fromkeys(
            concept_id
            for concept_id in (
                *(item.resolved_concept_id for item in capture.concept_mentions),
                *(item.concept_id for item in capture.learner_evidence),
                *(cid for item in capture.misconception_observations for cid in item.concept_ids),
                *(cid for item in capture.open_questions for cid in item.concept_ids),
                *(cid for item in capture.assessment_attempts for cid in item.concept_ids),
                *(item.concept_id for item in capture.conversation_explanations),
                *(item.concept_id for item in capture.generated_explanations),
            )
            if concept_id is not None
        )
    )
    concept_lines: list[str] = []
    for concept_id in used_concept_ids:
        label, text, warning = explanation(concept_id)
        marker = "〔GPT 生成，未核验〕" if label == "GPT 生成解释" else ""
        concept_lines.extend([f"### [[{labels(concept_id)}]]：{text}{marker}", f"#### {label}"])
        if warning:
            concept_lines.extend([f"> 来源：{warning}", ""])

    attempt_lines: list[str] = []
    verdict_labels = {
        "correct": "正确",
        "partial": "部分正确",
        "incorrect": "错误",
        "unresolved": "未判定",
    }
    for attempt in capture.assessment_attempts:
        attempt_lines.append(f"### 题目作答｜{attempt.attempt_id}")
        attempt_lines.append(f"- 题干：{attempt.question_text or '题干未提供'}")
        attempt_lines.append("- 选项：")
        attempt_lines.extend(
            [f"  - {option.label}. {option.text}" for option in attempt.options] or ["  - 未提供"]
        )
        attempt_lines.append(f"- 我的回答：{attempt.learner_answer}")
        judged = attempt.assistant_judged_answer or "未提供"
        attempt_lines.append(
            f"- 当时判定：{verdict_labels[attempt.verdict]}（助手当时判定：{judged}）"
        )
        attempt_lines.append(f"- 对话解析：{attempt.assistant_explanation or '未提供'}")
        linked = "、".join(f"[[{labels(cid)}]]" for cid in attempt.concept_ids) or "未关联"
        attempt_lines.append(f"- 关联概念：{linked}")
        attempt_lines.append("")

    def section(title: str, lines: list[str]) -> list[str]:
        return [f"## {title}", *(lines or ["- 无"])]

    independent = sum(
        item.evidence_type == "correct_independent" for item in capture.learner_evidence
    )
    partial_count = len(grouped["部分掌握"])
    wrong_count = len(grouped["明确错误"]) + len(capture.misconception_observations)
    title = str(capture.chapter_id or capture.course_id or capture.discipline_id)

    return "\n".join(
        [
            *front,
            "",
            f"# 学习记录｜{title}",
            "",
            "> 本次记录",
            (
                f"> 独立掌握 {independent} 项 · 部分掌握 {partial_count} 项 · "
                f"明确错误 {wrong_count} 项 · 未解决 {len(capture.open_questions)} 项"
            ),
            "",
            *section("已掌握", evidence_lines(grouped["已掌握"])),
            "",
            *section("部分掌握", evidence_lines(grouped["部分掌握"])),
            "",
            *section(
                "明确错误",
                evidence_lines(grouped["明确错误"], include_excerpt=True) + correction_lines,
            ),
            "",
            *section("未解决问题", question_lines),
            "",
            *section("题目作答", attempt_lines),
            "",
            *section("本次涉及概念", concept_lines),
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
