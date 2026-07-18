"""Dependency-free checks for Medical Note V1 public assets."""

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates" / "medical_note_v1.md"
SPEC = ROOT / "docs" / "medical-note-template-specification.md"
COPD = ROOT / "examples" / "medical_note_v1" / "COPD.md"
PREFIXES = ("实体/", "学科/", "系统/", "病程/", "临床场景/", "指南/")
SECTIONS = (
    "一", "二", "三", "四", "五", "六", "七", "八", "九", "十", "十一", "十二", "十三",
    "十四", "十五", "十六", "十七",
)
SPEC_SECTIONS = SECTIONS[:14]
FRONTMATTER_FIELDS = (
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
)


def read(path: Path) -> str:
    data = path.read_bytes()
    assert b"\r" not in data and data.endswith(b"\n") and not data.endswith(b"\n\n")
    return data.decode("utf-8")


def headings(text: str) -> list[str]:
    return re.findall(r"^## ([一二三四五六七八九十]+)、", text, re.M)


def test_template_encoding_lf_frontmatter_and_fields() -> None:
    text = read(TEMPLATE)
    assert text.startswith("---\n") and "\n---\n\n# " in text
    for field in FRONTMATTER_FIELDS:
        assert field in text


def test_template_sections_placeholders_and_no_copd() -> None:
    text = read(TEMPLATE)
    assert headings(text) == list(SECTIONS)
    assert re.search(r"\{\{.+?\}\}", text)
    assert "COPD" not in text and "慢性阻塞性肺疾病" not in text


def test_template_has_exactly_one_h1() -> None:
    assert len(re.findall(r"^# (?!#)", read(TEMPLATE), re.M)) == 1


def test_specification_sections_tags_and_prohibitions() -> None:
    text = read(SPEC)
    assert headings(text) == list(SPEC_SECTIONS)
    assert all(prefix in text for prefix in PREFIXES)
    assert all(tag in text for tag in ("#COPD", "#重点", "#已掌握", "实体/COPD"))


def test_specification_has_exactly_one_h1() -> None:
    assert len(re.findall(r"^# (?!#)", read(SPEC), re.M)) == 1


def test_copd_frontmatter_placeholders_and_title() -> None:
    text = read(COPD)
    assert text.startswith("---\n") and "\n---\n\n# " in text
    assert not re.search(r"\{\{.+?\}\}", text)
    assert len(re.findall(r"^# (?!#)", text, re.M)) == 1


def test_copd_tag_contract_and_order() -> None:
    tags = re.search(r"^tags:\n((?:  - .+\n)+)", read(COPD), re.M)
    assert tags
    values = [line[4:].strip().strip('"') for line in tags.group(1).splitlines()]
    assert all(value.startswith(PREFIXES) for value in values)
    order = [
        next(i for i, prefix in enumerate(PREFIXES) if value.startswith(prefix)) for value in values
    ]
    assert order == sorted(order)
    assert sum(value.startswith("实体/") for value in values) == 1
    assert sum(value.startswith("学科/") for value in values) == 1


def test_copd_sections_learning_and_sources() -> None:
    text = read(COPD)
    assert headings(text) == list(SECTIONS)
    assert "## 十六、学习记录" in text and "## 十七、证据来源与版本" in text


def test_copd_contains_required_primary_classifications() -> None:
    text = read(COPD)
    assert '  - "实体/疾病"' in text
    assert '  - "学科/内科学/呼吸系统"' in text


def test_assets_have_no_private_source_references() -> None:
    for path in (TEMPLATE, SPEC, COPD):
        private_prefix = "C:" + chr(92) + "Users" + chr(92)
        assert private_prefix not in read(path) and "medlearn.sqlite3" not in read(path)


def test_assets_have_distinct_roles() -> None:
    assert "{{规范中文名称}}" in read(TEMPLATE)
    assert "Composer 输出约束" in read(SPEC)
    assert "慢性阻塞性肺疾病" in read(COPD)


def test_template_and_specification_sha256_contract() -> None:
    expected = {
        TEMPLATE: "61f1d784de1b262a35030270a45d8608d88e909ed866ecb9a2c37bf65691d68a",
        SPEC: "a575cb5aaf03273937df3639843ae4fc8016d6032570fa7767c91d4a2ae6dee5",
    }
    for path, digest in expected.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


def test_copd_learner_record_keeps_provenance_separate() -> None:
    text = read(COPD)
    learning_start = text.index("## 十六、学习记录")
    sources_start = text.index("## 十七、证据来源与版本")
    learning = text[learning_start:sources_start]
    medical = text[:learning_start]
    assert "本节仅记录学习表现，不构成医学事实来源" in learning
    assert "COPD 与吸烟关系不大" not in medical
    assert "纠正状态" in learning and "已验证" in learning
