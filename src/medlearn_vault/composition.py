"""Local, deterministic note-composition preview from persisted intake data."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol

from pydantic import ValidationError

from medlearn_vault.capture import IntakeEnvelope
from medlearn_vault.handoff import LearningSegment, MedLearnHandoff
from medlearn_vault.source_index import SourceIndexError, search_index

SECTION_NUMBERS = (
    "一",
    "二",
    "三",
    "四",
    "五",
    "六",
    "七",
    "八",
    "九",
    "十",
    "十一",
    "十二",
    "十三",
    "十四",
    "十五",
    "十六",
    "十七",
)
SECTION_HEADINGS = tuple(
    f"## {number}、{title}"
    for number, title in zip(
        SECTION_NUMBERS,
        (
            "概念边界",
            "基本信息",
            "定义与诊断核心",
            "流行病学与疾病负担",
            "病因与危险因素",
            "发病机制与病理生理",
            "临床表现",
            "实验室检查与辅助检查",
            "诊断、分型与严重程度评估",
            "鉴别诊断",
            "治疗",
            "并发症与共病",
            "预后、随访与预防",
            "高频考点与易错点",
            "相关概念",
            "学习记录",
            "证据来源与版本",
        ),
        strict=True,
    )
)
TAG_VALUES = {
    "实体": {
        "疾病",
        "综合征",
        "症状",
        "体征",
        "检查",
        "药物",
        "治疗",
        "病理过程",
        "机制",
        "解剖结构",
        "病原体",
        "评分工具",
        "指南",
    },
    "学科": {
        "内科学/呼吸系统",
        "内科学/循环系统",
        "内科学/消化系统",
        "内科学/泌尿系统",
        "内科学/血液系统",
        "内科学/内分泌系统",
        "内科学/风湿免疫",
        "外科学/普通外科",
        "外科学/骨科",
        "外科学/泌尿外科",
        "神经病学",
        "儿科学",
        "妇产科学",
        "药理学",
        "病理学",
        "病理生理学",
        "诊断学",
        "医学影像学",
        "急诊医学",
        "重症医学",
        "肿瘤学",
    },
    "系统": {
        "呼吸系统",
        "循环系统",
        "消化系统",
        "泌尿系统",
        "血液系统",
        "神经系统",
        "内分泌系统",
        "免疫系统",
        "运动系统",
        "生殖系统",
        "感觉系统",
        "皮肤",
        "全身性",
    },
    "病程": {"急性", "亚急性", "慢性", "复发性", "进展性", "自限性"},
    "临床场景": {"急诊", "门诊", "住院", "重症", "围手术期", "筛查", "长期管理"},
    "指南": {"GOLD", "GINA", "ESC", "AHA", "ACC", "KDIGO", "NCCN", "CSCO", "WHO", "中国指南"},
}


def _tags_from_frontmatter(frontmatter: str) -> tuple[str, ...]:
    match = re.search(r"^tags:\n((?:  - [^\n]+\n?)*)", frontmatter, re.M)
    if match is None:
        return ()
    return tuple(line[4:].strip().strip("\"'") for line in match.group(1).splitlines())


@dataclass(frozen=True)
class CompositionIssue:
    severity: Literal["blocker", "warning"]
    code: str
    message: str


@dataclass(frozen=True)
class RetrievedSource:
    query: str
    rank: int
    score: int
    chunk_id: str
    source_relative_path: str
    source_file: str
    section_id: str
    section_titles: tuple[str, ...]
    start_pdf_page_number: int
    end_pdf_page_number: int
    text: str
    text_sha256: str


@dataclass(frozen=True)
class CompositionContext:
    source_job_id: str | None
    source_record_id: str
    intake_digest: str
    discipline_id: str | None
    course_id: str | None
    chapter_id: str | None
    learning_content: tuple[str, ...]
    concept_candidates: tuple[str, ...]
    learner_evidence: tuple[str, ...]
    misconceptions: tuple[str, ...]
    unresolved_questions: tuple[str, ...]
    isolated_items: tuple[str, ...]
    warnings: tuple[CompositionIssue, ...]
    proposed_target_path: str
    template: str
    current_note: str | None
    retrieved_sources: tuple[RetrievedSource, ...]
    retrieval_digest: str | None


@dataclass(frozen=True)
class CompositionResult:
    markdown: str
    target_path: str
    warnings: tuple[CompositionIssue, ...]
    isolated_items: tuple[str, ...]


@dataclass(frozen=True)
class CompositionValidationResult:
    status: Literal["accepted", "accepted_with_warnings", "rejected"]
    blockers: tuple[CompositionIssue, ...] = ()
    warnings: tuple[CompositionIssue, ...] = ()
    isolated_items: tuple[str, ...] = ()


class NoteComposer(Protocol):
    def compose(self, context: CompositionContext) -> str: ...


class StubNoteComposer:
    """Deterministic local preview renderer; it has no external dependencies."""

    def compose(self, context: CompositionContext) -> str:
        sections = [context.template.rstrip(), "# Learning composition preview"]
        sections.append(
            "## Learning content\n" + "\n".join(f"- {x}" for x in context.learning_content)
        )
        if context.concept_candidates:
            sections.append(
                "## Concept candidates\n" + "\n".join(f"- {x}" for x in context.concept_candidates)
            )
        if context.learner_evidence:
            sections.append(
                "## Learner evidence\n" + "\n".join(f"- {x}" for x in context.learner_evidence)
            )
        if context.misconceptions:
            sections.append(
                "## Misconceptions\n" + "\n".join(f"- {x}" for x in context.misconceptions)
            )
        if context.unresolved_questions:
            sections.append(
                "## Unresolved questions\n"
                + "\n".join(f"- {x}" for x in context.unresolved_questions)
            )
        if context.retrieved_sources:
            source_sections = ["## Retrieved source context"]
            for number, source in enumerate(context.retrieved_sources, 1):
                titles = " > ".join(source.section_titles)
                source_sections.append(
                    f"### Source {number}\n\n"
                    f"- Source: `{source.source_relative_path}`\n"
                    f"- Pages: `{source.start_pdf_page_number}-{source.end_pdf_page_number}`\n"
                    f"- Section: `{titles}`\n"
                    f"- Chunk: `{source.chunk_id}`\n"
                    f"- Retrieval query: `{source.query}`\n\n"
                    f"```text\n{source.text}\n```"
                )
            sections.append("\n\n".join(source_sections))
        if context.current_note:
            sections.append("## Current note\n" + context.current_note.rstrip())
        return "\n\n".join(sections) + "\n"


def validate_target_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or "\\" in value
        or "\0" in value
        or ".." in path.parts
        or not value.startswith("MedLearn/")
        or path.suffix != ".md"
    ):
        raise ValueError("UNSAFE_COMPOSITION_TARGET_PATH")
    return path.as_posix()


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _source_job_id(value: str | None) -> str | None:
    if value is None:
        return None
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value) is None:
        raise ValueError("INVALID_SOURCE_JOB_ID")
    return value


def build_context(
    raw: bytes,
    *,
    template: str,
    current_note: str | None = None,
    source_job_id: str | None = None,
    expected_intake_digest: str | None = None,
) -> CompositionContext:
    """Parse an IntakeEnvelope, Handoff, or LearningSegment without strict proposal resolution."""
    digest = _digest(raw)
    if expected_intake_digest is not None and expected_intake_digest != digest:
        raise ValueError("COMPOSITION_INPUT_DIGEST_MISMATCH")
    isolated: tuple[str, ...] = ()
    try:
        envelope = IntakeEnvelope.model_validate_json(raw)
    except ValidationError:
        envelope = None
    if envelope is not None:
        draft = envelope.draft
        content = tuple(x.statement for x in draft.claim_candidates) + tuple(
            x.explanation_text for x in draft.generated_explanations
        )
        concepts = tuple(x.surface_text for x in draft.concept_mentions)
        evidence = tuple(x.rationale for x in draft.learner_evidence_candidates)
        misconceptions = tuple(x.observed_error_logic for x in draft.misconception_candidates)
        questions = tuple(x.statement for x in draft.claim_candidates if x.claim_type == "question")
        discipline, course, chapter = (
            draft.context.discipline_id,
            draft.context.course_id,
            draft.context.chapter_id,
        )
    else:
        tolerant = False
        try:
            handoff = MedLearnHandoff.model_validate_json(raw)
        except ValidationError:
            try:
                handoff = LearningSegment.model_validate_json(raw).handoff
            except ValidationError:
                try:
                    payload = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError("INVALID_COMPOSITION_INTAKE") from exc
                segment_input = isinstance(payload, dict) and "handoff" in payload
                handoff_payload = payload.get("handoff") if segment_input else payload
                if (
                    not isinstance(handoff_payload, dict)
                    or "session" not in handoff_payload
                    or "evidence_messages" not in handoff_payload
                ):
                    raise ValueError("INVALID_COMPOSITION_INTAKE") from None
                if not isinstance(handoff_payload["evidence_messages"], list):
                    raise ValueError("INVALID_COMPOSITION_INTAKE") from None
                roles = {
                    x.get("local_id"): x.get("role")
                    for x in handoff_payload.get("evidence_messages", [])
                    if isinstance(x, dict)
                }

                def conflict(item: object, key: str, required: str | None = None) -> bool:
                    refs = item.get(key, []) if isinstance(item, dict) else []
                    seen = {roles.get(x) for x in refs}
                    return (
                        len(seen) != 1
                        or None in seen
                        or (required is not None and seen != {required})
                    )

                for collection, key, required in (
                    ("claims", "evidence_local_ids", None),
                    ("learner_evidence", "evidence_local_ids", "user"),
                ):
                    if not isinstance(handoff_payload.get(collection, []), list):
                        raise ValueError("INVALID_COMPOSITION_INTAKE") from None
                    kept = []
                    for index, item in enumerate(handoff_payload[collection]):
                        if not isinstance(item, dict):
                            raise ValueError("INVALID_COMPOSITION_INTAKE") from None
                        if conflict(item, key, required):
                            isolated += (f"{collection}[{index}]:EVIDENCE_ROLE_CONFLICT",)
                        else:
                            kept.append(item)
                    handoff_payload[collection] = kept
                if not isolated:
                    raise ValueError("INVALID_COMPOSITION_INTAKE") from None
                try:
                    handoff = (
                        LearningSegment.model_validate(payload).handoff
                        if segment_input
                        else MedLearnHandoff.model_validate(handoff_payload)
                    )
                except ValidationError as exc:
                    raise ValueError("INVALID_COMPOSITION_INTAKE") from exc
                tolerant = False
        if not tolerant:
            assert handoff is not None
            content = (
                tuple(x.statement for x in handoff.claims)
                + tuple(x.explanation_text for x in handoff.generated_explanations)
                + tuple(handoff.learning_goals)
            )
            concepts = tuple(x.name for x in handoff.concepts)
            evidence = tuple(x.rationale for x in handoff.learner_evidence)
            misconceptions = tuple(x.observed_error_logic for x in handoff.misconceptions)
            questions = tuple(x.statement for x in handoff.unresolved_questions)
            discipline, course, chapter = (
                handoff.session.discipline_id,
                handoff.session.course_id,
                handoff.session.chapter_id,
            )
    usable_content = bool(content or evidence or misconceptions or questions)
    if not usable_content:
        raise ValueError("NO_USABLE_LEARNING_CONTENT")
    warnings: list[CompositionIssue] = []
    if isolated:
        warnings.append(
            CompositionIssue("warning", "EVIDENCE_ROLE_CONFLICT", "conflicted items isolated")
        )
    if not concepts:
        warnings.append(
            CompositionIssue("warning", "UNRESOLVED_CONCEPT", "no stable concept target")
        )
    if course is None:
        warnings.append(CompositionIssue("warning", "MISSING_COURSE_ID", "course_id is missing"))
    if chapter is None:
        warnings.append(CompositionIssue("warning", "MISSING_CHAPTER_ID", "chapter_id is missing"))
    job_id = _source_job_id(source_job_id)
    source_record_id = "preview_" + digest[7:23]
    target = validate_target_path(f"MedLearn/Inbox/{job_id or source_record_id}.md")
    return CompositionContext(
        job_id,
        source_record_id,
        digest,
        discipline,
        course,
        chapter,
        content,
        concepts,
        evidence,
        misconceptions,
        questions,
        isolated,
        tuple(warnings),
        target,
        template,
        current_note,
        (),
        None,
    )


def _query_candidates(concepts: tuple[str, ...]) -> tuple[str, ...]:
    selected: list[str] = []
    seen: set[str] = set()
    for concept in concepts:
        query = " ".join(concept.split())
        key = query.casefold()
        if query and key not in seen:
            selected.append(query)
            seen.add(key)
        if len(selected) == 3:
            break
    return tuple(selected)


def _retrieval_digest(sources: tuple[RetrievedSource, ...]) -> str:
    value = [
        {
            "query": source.query,
            "chunk_id": source.chunk_id,
            "source_relative_path": source.source_relative_path,
            "section_id": source.section_id,
            "start_pdf_page_number": source.start_pdf_page_number,
            "end_pdf_page_number": source.end_pdf_page_number,
            "text_sha256": source.text_sha256,
            "score": source.score,
        }
        for source in sources
    ]
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return _digest(raw)


def attach_retrieval(
    context: CompositionContext, index: Path, retrieval_limit: int = 6
) -> CompositionContext:
    """Attach deterministic local lexical retrieval without changing source-index behavior."""
    if not 1 <= retrieval_limit <= 12:
        raise ValueError("INVALID_RETRIEVAL_LIMIT")
    queries = _query_candidates(context.concept_candidates)
    warnings = tuple(issue for issue in context.warnings if issue.code != "SOURCE_MISSING")
    if not queries:
        return replace(
            context,
            warnings=warnings
            + (
                CompositionIssue(
                    "warning", "SOURCE_QUERY_UNAVAILABLE", "no concept query available"
                ),
            ),
        )
    selected: list[RetrievedSource] = []
    seen_chunk_ids: set[str] = set()
    try:
        for query in queries:
            result = search_index(index, query, limit=2)
            for row in result["results"]:
                chunk_id = row["chunk_id"]
                if chunk_id in seen_chunk_ids:
                    continue
                seen_chunk_ids.add(chunk_id)
                selected.append(
                    RetrievedSource(
                        query=query,
                        rank=row["rank"],
                        score=row["score"],
                        chunk_id=chunk_id,
                        source_relative_path=row["source_relative_path"],
                        source_file=row["source_file"],
                        section_id=row["section_id"],
                        section_titles=tuple(row["section_titles"]),
                        start_pdf_page_number=row["start_pdf_page_number"],
                        end_pdf_page_number=row["end_pdf_page_number"],
                        text=row["text"],
                        text_sha256=row["text_sha256"],
                    )
                )
                if len(selected) == retrieval_limit:
                    break
            if len(selected) == retrieval_limit:
                break
    except SourceIndexError as exc:
        raise ValueError("COMPOSITION_SOURCE_INDEX_FAILED") from exc
    sources = tuple(selected)
    if not sources:
        warnings += (CompositionIssue("warning", "SOURCE_NOT_FOUND", "no local source matched"),)
        return replace(context, warnings=warnings)
    return replace(
        context,
        retrieved_sources=sources,
        retrieval_digest=_retrieval_digest(sources),
        warnings=warnings,
    )


def compose_preview(
    context: CompositionContext, composer: NoteComposer | None = None
) -> CompositionResult:
    markdown = (composer or StubNoteComposer()).compose(context)
    if not markdown.strip():
        raise ValueError("EMPTY_COMPOSER_OUTPUT")
    return CompositionResult(
        markdown,
        validate_target_path(context.proposed_target_path),
        context.warnings,
        context.isolated_items,
    )


def validate_composition(context: CompositionContext) -> CompositionValidationResult:
    return CompositionValidationResult(
        "accepted_with_warnings" if context.warnings else "accepted",
        warnings=context.warnings,
        isolated_items=context.isolated_items,
    )


def validate_generated_note(
    context: CompositionContext, markdown: str
) -> CompositionValidationResult:
    """Validate deterministic output-contract safety, not medical correctness."""
    blockers: list[CompositionIssue] = []
    warnings = list(context.warnings)
    if not markdown.strip():
        blockers.append(CompositionIssue("blocker", "GENERATED_NOTE_EMPTY", "empty output"))
    if markdown.lstrip().startswith("```"):
        blockers.append(
            CompositionIssue("blocker", "GENERATED_NOTE_FENCED_OUTPUT", "fenced output")
        )
    if not markdown.startswith("---\n") or "\n---\n" not in markdown:
        blockers.append(
            CompositionIssue("blocker", "GENERATED_NOTE_FRONTMATTER_INVALID", "frontmatter")
        )
        return CompositionValidationResult(
            "rejected", tuple(blockers), tuple(warnings), context.isolated_items
        )
    frontmatter, body = markdown[4:].split("\n---\n", 1)
    for field in (
        "medlearn_type:",
        "template_version:",
        "canonical_name:",
        "english_name:",
        "concept_type:",
        "aliases:",
        "external_identifiers:",
        "primary_discipline:",
        "related_disciplines:",
        "body_systems:",
        "guidelines:",
        "knowledge_status:",
        "review_status:",
        "last_reviewed_at:",
        "tags:",
    ):
        if field not in frontmatter:
            blockers.append(CompositionIssue("blocker", "GENERATED_NOTE_FIELD_MISSING", field))
    if "{{" in markdown or "}}" in markdown:
        blockers.append(
            CompositionIssue("blocker", "GENERATED_NOTE_PLACEHOLDER_REMAINS", "placeholder")
        )
    title = re.findall(r"^# (.+)$", body, re.M)
    canonical = re.search(r"^canonical_name:\s*[\"']?(.+?)[\"']?\s*$", frontmatter, re.M)
    if len(title) != 1 or canonical is None or title[0] != canonical.group(1):
        blockers.append(CompositionIssue("blocker", "GENERATED_NOTE_H1_INVALID", "title"))
    h2 = tuple(re.findall(r"^## (?!#).+$", body, re.M))
    if h2 != SECTION_HEADINGS:
        blockers.append(
            CompositionIssue("blocker", "GENERATED_NOTE_SECTION_ORDER_INVALID", "sections")
        )
    tags = _tags_from_frontmatter(frontmatter)
    dimensions = ("实体", "学科", "系统", "病程", "临床场景", "指南")
    parsed = [tag.split("/", 1) for tag in tags]
    orders = [
        dimensions.index(parts[0]) if len(parts) == 2 and parts[0] in dimensions else -1
        for parts in parsed
    ]
    if (
        not tags
        or -1 in orders
        or orders != sorted(orders)
        or len(tags) != len(set(tags))
        or sum(parts[0] == "实体" for parts in parsed if len(parts) == 2) != 1
        or sum(parts[0] == "学科" for parts in parsed if len(parts) == 2) != 1
        or any(
            len(parts) != 2 or parts[1] not in TAG_VALUES.get(parts[0], set()) for parts in parsed
        )
    ):
        blockers.append(CompositionIssue("blocker", "GENERATED_NOTE_TAG_INVALID", "tags"))
    if not re.search(r"^review_status:\s*unreviewed\s*$", frontmatter, re.M) or not re.search(
        r"^last_reviewed_at:\s*null\s*$", frontmatter, re.M
    ):
        blockers.append(
            CompositionIssue("blocker", "GENERATED_NOTE_REVIEW_STATUS_INVALID", "review fields")
        )
    learning_at = body.find("## 十六、学习记录")
    stable = body if learning_at < 0 else body[:learning_at]
    if re.search(
        r"[A-Za-z]:\\|\\\\|/(?:Users|home|mnt|tmp|var|opt|srv)/|file://|medlearn\.sqlite3|sqlite:",
        markdown,
        re.I,
    ):
        blockers.append(
            CompositionIssue("blocker", "GENERATED_NOTE_PRIVATE_PATH_LEAK", "private path")
        )
    if "Retrieved source context" in markdown or any(
        item in stable
        for item in context.misconceptions + context.learner_evidence + context.isolated_items
    ):
        blockers.append(
            CompositionIssue("blocker", "GENERATED_NOTE_ROLE_CONTAMINATION", "role contamination")
        )
    if learning_at >= 0 and "已验证" in body[learning_at:]:
        blockers.append(
            CompositionIssue(
                "blocker", "GENERATED_NOTE_UNSUPPORTED_VERIFIED_CORRECTION", "verified"
            )
        )
    if not context.retrieved_sources:
        warnings.append(CompositionIssue("warning", "SOURCE_NOT_FOUND", "no retrieved source"))
    if context.current_note is None:
        warnings.append(CompositionIssue("warning", "CURRENT_NOTE_NOT_SUPPLIED", "no current note"))
    return CompositionValidationResult(
        "rejected" if blockers else ("accepted_with_warnings" if warnings else "accepted"),
        tuple(blockers),
        tuple(warnings),
        context.isolated_items,
    )
