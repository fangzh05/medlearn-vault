"""Deterministic, reader-facing Obsidian projections.

Canonical publication plans deliberately keep their original bytes.  This
module is the rebuildable presentation plane: it receives a validated bundle
and immutable captures and emits only Markdown that is useful in Obsidian.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from medlearn_vault.bundle import ContractBundle
from medlearn_vault.domain.claims import MedicalClaim
from medlearn_vault.domain.concepts import ConceptEntity, ConceptRelation
from medlearn_vault.domain.learner import LearningCapture

PRESENTATION_RENDERER_VERSION = "1.1.0"
PRESENTATION_CONTRACT_VERSION = "1.0.0"
MARKDOWN_MEDIA = "text/markdown; charset=utf-8"
NO_EXPLANATION = "暂无解释"
_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


@dataclass(frozen=True)
class ConceptExplanation:
    text: str
    source_type: str
    claim_id: str | None = None


@dataclass(frozen=True)
class PresentationArtifact:
    path: str
    content_utf8: str
    content_digest: str
    byte_length: int


@dataclass(frozen=True)
class PresentationGeneration:
    generation_id: str
    artifacts: tuple[PresentationArtifact, ...]
    diagnostics: tuple[str, ...]


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _yaml(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _chinese(value: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", value))


def _eligible_definition(bundle: ContractBundle, concept_id: str) -> list[MedicalClaim]:
    sources = {str(item.source_id): item for item in bundle.sources}
    eligible = []
    for claim in bundle.claims:
        if (
            claim.claim_type == "definition"
            and claim.evidence_state == "supported"
            and claim.verification_status in {"source_backed", "verified_reference"}
            and claim.claim_status == "active"
            and tuple(str(item) for item in claim.concept_ids) == (concept_id,)
            and claim.citations
            and all(
                (source := sources.get(str(citation.source_id))) is not None
                and source.source_type != "learning_chat"
                and source.authority > 0
                for citation in claim.citations
            )
        ):
            eligible.append(claim)
    return eligible


def resolve_concept_explanation(
    bundle: ContractBundle,
    concept_id: str,
    captures: tuple[LearningCapture, ...] = (),
) -> ConceptExplanation:
    """Resolve one explanation without blending or relabeling provenance tiers."""
    candidates = _eligible_definition(bundle, concept_id)
    if candidates:
        winner = sorted(
            candidates,
            key=lambda item: (
                0 if item.verification_status == "verified_reference" else 1,
                str(item.claim_id),
            ),
        )[0]
        return ConceptExplanation(
            winner.statement, winner.verification_status, str(winner.claim_id)
        )
    concept = next((item for item in bundle.concepts if str(item.concept_id) == concept_id), None)
    if concept is not None and _chinese(concept.scope_note):
        return ConceptExplanation(concept.scope_note, "scope_note")
    ordered = sorted(
        captures, key=lambda item: (item.captured_at, str(item.session_id)), reverse=True
    )
    for capture in ordered:
        chat = next(
            (
                item
                for item in capture.conversation_explanations
                if str(item.concept_id) == concept_id
            ),
            None,
        )
        if chat is not None:
            return ConceptExplanation(chat.explanation_text, "unverified_chat")
    persisted_chat = next(
        (
            item
            for item in bundle.conversation_explanations
            if str(item.concept_id) == concept_id
        ),
        None,
    )
    if persisted_chat is not None:
        return ConceptExplanation(persisted_chat.explanation_text, "unverified_chat")
    for capture in ordered:
        generated = next(
            (item for item in capture.generated_explanations if str(item.concept_id) == concept_id),
            None,
        )
        if generated is not None:
            return ConceptExplanation(generated.explanation_text, "gpt_generated")
    persisted = sorted(
        (
            item
            for item in bundle.generated_explanations
            if str(item.concept_id) == concept_id
        ),
        key=lambda item: (
            item.generated_at,
            item.generation_context_digest,
            item.generator_id,
        ),
        reverse=True,
    )
    if persisted:
        return ConceptExplanation(persisted[0].explanation_text, "gpt_generated")
    return ConceptExplanation(NO_EXPLANATION, "unavailable")


def sanitize_filename(value: str) -> str:
    """Return one Windows-safe filename segment, never a path."""
    if (
        not value
        or "\x00" in value
        or "\\" in value
        or value.startswith("/")
        or any(part in {".", ".."} for part in value.split("/"))
    ):
        raise ValueError("unsafe presentation filename")
    result = _INVALID_FILENAME.sub("＿", value).strip().rstrip(". ")
    if not result or result in {".", ".."} or result.upper().split(".", 1)[0] in _RESERVED:
        raise ValueError("unsafe presentation filename")
    return result


def _short_id(value: str) -> str:
    match = re.search(r"([a-f0-9]{8})$", value)
    if match is None:
        raise ValueError("stable identifier required")
    return match.group(1)


def concept_paths(concepts: tuple[ConceptEntity, ...]) -> dict[str, str]:
    """Map active concepts to deterministic, readable Obsidian paths."""
    active = sorted(
        (item for item in concepts if item.status == "active"),
        key=lambda item: str(item.concept_id),
    )
    names: dict[str, list[ConceptEntity]] = {}
    for concept in active:
        names.setdefault(sanitize_filename(concept.canonical_name), []).append(concept)
    result: dict[str, str] = {}
    for name, items in names.items():
        for concept in items:
            suffix = "" if len(items) == 1 else f"〔{_short_id(str(concept.concept_id))}〕"
            result[str(concept.concept_id)] = f"MedLearn/概念/{name}{suffix}.md"
    return result


def _capture_title(capture: LearningCapture, concepts: dict[str, ConceptEntity]) -> str:
    names = [concepts[item].canonical_name for item in _resolved_ids(capture, concepts)[:3]]
    return sanitize_filename("、".join(names) if names else "学习记录")


def capture_path(
    capture: LearningCapture, capture_id: str, concepts: dict[str, ConceptEntity]
) -> str:
    return (
        f"MedLearn/学习记录/{capture.captured_at.year:04d}/{capture.captured_at.month:02d}/"
        f"{_capture_title(capture, concepts)}｜{capture.captured_at.date().isoformat()}"
        f"〔{_short_id(capture_id)}〕.md"
    )


def _wikilink(concept: ConceptEntity, paths: dict[str, str]) -> str:
    path = paths[str(concept.concept_id)]
    return f"[[{path.removesuffix('.md')}|{concept.canonical_name}]]"


def _resolved_ids(capture: LearningCapture, concepts: dict[str, ConceptEntity]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            str(mention.resolved_concept_id)
            for mention in capture.concept_mentions
            if mention.resolution_status == "resolved"
            and mention.resolved_concept_id is not None
            and str(mention.resolved_concept_id) in concepts
            and concepts[str(mention.resolved_concept_id)].status == "active"
        )
    )


def _used_ids(capture: LearningCapture, concepts: dict[str, ConceptEntity]) -> tuple[str, ...]:
    """Return every safely renderable active concept referenced by visible capture content."""
    candidates = (
        *(
            str(item.resolved_concept_id)
            for item in capture.concept_mentions
            if item.resolved_concept_id
        ),
        *(str(item.concept_id) for item in capture.learner_evidence),
        *(str(cid) for item in capture.misconception_observations for cid in item.concept_ids),
        *(str(cid) for item in capture.open_questions for cid in item.concept_ids),
        *(str(cid) for item in capture.assessment_attempts for cid in item.concept_ids),
        *(str(item.concept_id) for item in capture.conversation_explanations),
        *(str(item.concept_id) for item in capture.generated_explanations),
    )
    return tuple(
        dict.fromkeys(
            concept_id
            for concept_id in candidates
            if concept_id in concepts and concepts[concept_id].status == "active"
        )
    )


def _explanation_block(explanation: ConceptExplanation) -> list[str]:
    if explanation.source_type in {"verified_reference", "source_backed"}:
        return ["## 已验证解释", "", explanation.text]
    if explanation.source_type == "scope_note":
        return ["## 目录范围说明", "", explanation.text]
    if explanation.source_type == "unverified_chat":
        return [
            "## 对话内解释",
            "",
            explanation.text,
            "",
            "> 来源：本次学习对话，未经外部核验。",
        ]
    if explanation.source_type == "gpt_generated":
        return [
            "## GPT 生成解释",
            "",
            explanation.text,
            "",
            "> 由 GPT 根据概念名称和当前学习上下文生成，未经教材或指南核验。",
        ]
    return ["## 暂无解释", "", NO_EXPLANATION]


def _display(concept_id: str, concepts: dict[str, ConceptEntity], paths: dict[str, str]) -> str:
    concept = concepts.get(concept_id)
    return (
        _wikilink(concept, paths) if concept is not None and concept_id in paths else "未收录概念"
    )


def render_capture_note(
    bundle: ContractBundle, capture: LearningCapture, capture_id: str, paths: dict[str, str]
) -> str:
    concepts = {str(item.concept_id): item for item in bundle.concepts}
    explanations = {
        concept_id: resolve_concept_explanation(bundle, concept_id, (capture,))
        for concept_id in paths
    }
    visible = set(_used_ids(capture, concepts))
    front = [
        "---",
        f"medlearn_type: {_yaml('learning_capture')}",
        f"renderer_version: {_yaml(PRESENTATION_RENDERER_VERSION)}",
        f"capture_id: {_yaml(capture_id)}",
        f"captured_at: {_yaml(capture.captured_at.isoformat())}",
        "---",
        "",
        f"# 学习记录｜{_capture_title(capture, concepts)}",
        "",
    ]

    def evidence(title: str, kinds: set[str]) -> list[str]:
        lines = [f"## {title}"]
        items = [item for item in capture.learner_evidence if item.evidence_type in kinds]
        for item in items:
            concept_id = str(item.concept_id)
            text = _display(concept_id, concepts, paths)
            lines.extend([f"- {text}", f"  - 表现：{item.rationale}"])
        return [*lines, *(["- 无"] if not items else [])]

    body = [
        *front,
        *evidence("已掌握", {"correct_independent", "correct_after_hint"}),
        "",
        *evidence("部分掌握", {"guessed_correct", "partial", "unknown", "self_report_only"}),
        "",
        *evidence("明确错误", {"incorrect", "high_confidence_incorrect"}),
        "",
        "## 错误逻辑与纠正",
    ]
    claims = {str(item.claim_id): item for item in bundle.claims}
    if capture.misconception_observations:
        for observation in capture.misconception_observations:
            linked = [
                _display(str(concept_id), concepts, paths)
                for concept_id in observation.concept_ids
                if str(concept_id) in paths
            ]
            body.append(f"- {'、'.join(linked) or '相关概念'}")
            if observation.user_excerpt:
                body.append(f"  - 我的原回答：{observation.user_excerpt}")
            body.append(f"  - 错误逻辑：{observation.observed_error_logic}")
            corrections = [
                claims.get(str(claim_id)) for claim_id in observation.correction_claim_ids
            ]
            verified = [
                claim
                for claim in corrections
                if claim is not None
                and claim.claim_status == "active"
                and claim.evidence_state == "supported"
                and claim.verification_status in {"source_backed", "verified_reference"}
                and claim.citations
            ]
            if verified:
                for claim in verified:
                    body.append(f"  - 纠正：{claim.statement}")
            elif observation.proposed_correction:
                body.append(f"  - 待验证建议：{observation.proposed_correction}")
            else:
                body.append("  - 纠正：待验证")
    else:
        body.append("- 无")
    body.extend(
        [
            "",
            "## 未解决问题",
        ]
    )
    if capture.open_questions:
        for question in capture.open_questions:
            linked = [
                _display(str(concept_id), concepts, paths)
                for concept_id in question.concept_ids
                if str(concept_id) in visible
            ]
            body.append(f"- {question.text}" + (f"（{'、'.join(linked)}）" if linked else ""))
    else:
        body.append("- 无")
    body.extend(["", "## 题目作答"])
    verdicts = {
        "correct": "正确",
        "partial": "部分正确",
        "incorrect": "错误",
        "unresolved": "未判定",
    }
    for attempt in capture.assessment_attempts:
        body.extend(
            [
                f"### {attempt.attempt_id}",
                f"- 题干：{attempt.question_text or '题干未提供'}",
                "- 选项：",
                *(
                    [f"  - {option.label}. {option.text}" for option in attempt.options]
                    or ["  - 未提供"]
                ),
                f"- 我的回答：{attempt.learner_answer}",
                f"- 当时判定：{verdicts[attempt.verdict]}"
                + (
                    f"（助手当时判定：{attempt.assistant_judged_answer}）"
                    if attempt.assistant_judged_answer
                    else ""
                ),
                f"- 对话解析：{attempt.assistant_explanation or '未提供'}",
                "- 关联概念："
                + (
                    "、".join(_display(str(cid), concepts, paths) for cid in attempt.concept_ids)
                    or "未关联"
                ),
                "",
            ]
        )
    if not capture.assessment_attempts:
        body.append("- 无")
    body.extend(["", "## 本次涉及概念"])
    for concept_id in sorted(visible, key=lambda item: paths[item]):
        explanation = explanations[concept_id]
        marker = "〔GPT 生成，未核验〕" if explanation.source_type == "gpt_generated" else ""
        body.append(f"- {_display(concept_id, concepts, paths)}：{explanation.text}{marker}")
    if not visible:
        body.append("- 无")
    return "\n".join(body).rstrip("\n") + "\n"


def _reviewed_relations(
    bundle: ContractBundle, concept_id: str, paths: dict[str, str]
) -> tuple[ConceptRelation, ...]:
    claims = {str(item.claim_id): item for item in bundle.claims}
    relations = []
    for relation in bundle.relations:
        if (
            str(relation.source_concept_id) != concept_id
            or str(relation.target_concept_id) not in paths
        ):
            continue
        support = [claims.get(str(item)) for item in relation.supporting_claim_ids]
        if all(
            item is not None
            and item.claim_status == "active"
            and item.evidence_state == "supported"
            and item.verification_status in {"source_backed", "verified_reference"}
            and item.citations
            for item in support
        ):
            relations.append(relation)
    return tuple(
        sorted(
            relations,
            key=lambda item: (
                item.relation_type,
                str(item.target_concept_id),
                str(item.relation_id),
            ),
        )
    )


def render_concept_note(
    bundle: ContractBundle,
    concept: ConceptEntity,
    paths: dict[str, str],
    capture_notes: tuple[tuple[str, LearningCapture, str], ...],
) -> tuple[str, tuple[str, ...]]:
    concept_id = str(concept.concept_id)
    explanation = resolve_concept_explanation(
        bundle, concept_id, tuple(capture for _, capture, _ in capture_notes)
    )
    aliases = sorted({alias.text for alias in concept.aliases})
    front = [
        "---",
        f"medlearn_type: {_yaml('concept')}",
        f"renderer_version: {_yaml(PRESENTATION_RENDERER_VERSION)}",
        f"concept_id: {_yaml(concept_id)}",
        f"canonical_name: {_yaml(concept.canonical_name)}",
        f"concept_type: {_yaml(concept.concept_type)}",
        "aliases:",
        *(f"  - {_yaml(alias)}" for alias in aliases),
        f"status: {_yaml('active')}",
        "---",
        "",
        f"# {concept.canonical_name}",
        "",
        *_explanation_block(explanation),
        "",
        "## 基本信息",
        f"- 英文名：{concept.preferred_english or '暂无'}",
        f"- 类型：{concept.concept_type}",
        f"- 别名：{'、'.join(aliases) or '暂无'}",
        "",
        "## 相关概念",
    ]
    diagnostics: list[str] = []
    relations = _reviewed_relations(bundle, concept_id, paths)
    if relations:
        for relation in relations:
            target = next(
                item
                for item in bundle.concepts
                if str(item.concept_id) == str(relation.target_concept_id)
            )
            front.append(f"- {relation.relation_type}：{_wikilink(target, paths)}")
    else:
        front.append("- 无")
    front.extend(["", "## 学习记录"])
    backlinks = []
    for capture_id, capture, path in capture_notes:
        if concept_id in _used_ids(
            capture, {str(item.concept_id): item for item in bundle.concepts}
        ):
            concepts = {str(item.concept_id): item for item in bundle.concepts}
            backlinks.append(
                (capture.captured_at, capture_id, path, _capture_title(capture, concepts))
            )
    for _, _, path, title in sorted(backlinks, key=lambda item: (item[0], item[1]), reverse=True):
        front.append(f"- [[{path.removesuffix('.md')}|{title}]]")
    if not backlinks:
        front.append("- 无")
    return "\n".join(front).rstrip("\n") + "\n", tuple(diagnostics)


def build_presentation(
    bundle: ContractBundle, captures: tuple[tuple[str, LearningCapture, str], ...]
) -> PresentationGeneration:
    """Build the visible projection from immutable capture identities and bundle data."""
    paths = concept_paths(bundle.concepts)
    concepts = {str(item.concept_id): item for item in bundle.concepts}
    used = {concept_id for _, capture, _ in captures for concept_id in _used_ids(capture, concepts)}
    # Add only reviewed relation targets; capture co-occurrence never creates an edge.
    pending = list(used)
    while pending:
        source_id = pending.pop()
        for relation in _reviewed_relations(bundle, source_id, paths):
            target_id = str(relation.target_concept_id)
            if target_id not in used:
                used.add(target_id)
                pending.append(target_id)
    paths = {concept_id: path for concept_id, path in paths.items() if concept_id in used}
    capture_notes = tuple(
        sorted(
            (
                (capture_id, capture, capture_path(capture, capture_id, concepts))
                for capture_id, capture, _ in captures
            ),
            key=lambda item: item[0],
        )
    )
    artifacts: list[PresentationArtifact] = []
    for capture_id, capture, path in capture_notes:
        content = render_capture_note(bundle, capture, capture_id, paths)
        data = content.encode("utf-8")
        artifacts.append(PresentationArtifact(path, content, _digest(data), len(data)))
    diagnostics: list[str] = []
    for concept_id in sorted(paths, key=lambda item: paths[item]):
        content, warnings = render_concept_note(bundle, concepts[concept_id], paths, capture_notes)
        diagnostics.extend(warnings)
        data = content.encode("utf-8")
        artifacts.append(PresentationArtifact(paths[concept_id], content, _digest(data), len(data)))
    artifacts.sort(key=lambda item: item.path)
    identity = {
        "contract_version": PRESENTATION_CONTRACT_VERSION,
        "renderer_version": PRESENTATION_RENDERER_VERSION,
        "bundle": bundle.model_dump(mode="json"),
        "captures": [(capture_id, digest) for capture_id, _, digest in sorted(captures)],
        "artifacts": [(item.path, item.content_digest) for item in artifacts],
    }
    generation_id = (
        "presentation_"
        + hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()[:32]
    )
    return PresentationGeneration(generation_id, tuple(artifacts), tuple(sorted(diagnostics)))
