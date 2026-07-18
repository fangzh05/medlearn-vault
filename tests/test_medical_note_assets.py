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
TEMPLATE_SENTENCES = (
    "不得在这里堆叠病因、治疗和流行病学信息。",
    "危险因素不得写成确定病因。",
    "不得把所有可能症状无层次地排列，应突出常见表现、特异表现和危险表现。",
    "本节必须区分筛查、确诊、严重度评估、病因判断和随访监测。",
    "鉴别诊断必须给出可操作的区分依据，不能只列疾病名称。",
    "所有剂量、疗程和阈值应注明适用条件；不明确时不得自行补全。",
    "本节只保留高辨识度、可迁移的考试信息，不重复正文。",
    "学习者回答与医学知识正文必须保持隔离。",
    "引用不足、版本不明或互相冲突的内容必须明确标记，不得用确定语气隐藏不确定性。",
)
TEMPLATE_MARKERS = (
    "- {{疾病阶段、适用人群或讨论范围}}",
    "- {{不属于当前概念本体的内容}}",
    "- {{必要时说明诊断标准的版本或来源}}",
    "#### 2. {{机制名称}}",
    "### 典型表现",
    "### 非典型表现",
    "### 影像学与功能检查",
    "### 不推荐常规使用的检查",
    "3. {{条件三}}",
    "> [!warning] {{本病}} vs {{鉴别疾病}}",
    "#### {{疾病状态、分型或阶段二}}",
    "- {{首选治疗}}",
    "- {{替代方案}}",
    "- {{升级条件}}",
    "生命支持\n→ 评估严重程度\n→ 病因治疗\n→ 纠正关键生理紊乱\n→ 监测与升级治疗",
    "### 特殊人群",
    "### 不推荐或已淘汰的治疗",
    "### 病例题决策信号",
    "### 学习记录链接",
    "### 证据说明",
)
ENTITY_TAGS = (
    "实体/疾病",
    "实体/综合征",
    "实体/症状",
    "实体/体征",
    "实体/检查",
    "实体/药物",
    "实体/治疗",
    "实体/病理过程",
    "实体/机制",
    "实体/解剖结构",
    "实体/病原体",
    "实体/评分工具",
    "实体/指南",
)
PROHIBITED_TAGS = (
    "#COPD",
    "#慢阻肺",
    "#重点",
    "#高频",
    "#已掌握",
    "#未掌握",
    "#待复习",
    "#已审核",
    "#最新指南",
    "#难",
    "#常考",
)
COMPOSER_STAGES = (
    "系统规则与事实来源规则", "当前模板", "当前概念的规范元数据", "当前已有笔记",
    "本次学习记录", "检索出的教材和指南片段", "黄金样例", "本次生成任务",
)
QUALITY_CHECKLIST = (
    "标题与规范名称一致。", "标签来自受控词表。", "医学正文与学习记录隔离。",
    "病因、机制、表现、检查和治疗存在清晰因果关系。", "诊断标准与严重程度评估没有混淆。",
    "鉴别诊断具有实际区分依据。", "数值、阈值和疗程包含条件或来源。", "指南敏感内容注明版本。",
    "无重复章节和大段重复文字。", "无未经验证的医学断言。", "相关概念链接稳定、可解析。",
    "末尾包含来源与版本说明。", "输出为完整可直接写入 Obsidian 的 Markdown。",
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
    assert all(marker in text for marker in TEMPLATE_MARKERS)
    assert all(sentence in text for sentence in TEMPLATE_SENTENCES)


def test_template_has_exactly_one_h1() -> None:
    assert len(re.findall(r"^# (?!#)", read(TEMPLATE), re.M)) == 1


def test_specification_sections_tags_and_prohibitions() -> None:
    text = read(SPEC)
    assert headings(text) == list(SPEC_SECTIONS)
    assert all(prefix in text for prefix in PREFIXES)
    assert all(tag in text for tag in ENTITY_TAGS)
    assert all(tag in text for tag in PROHIBITED_TAGS)
    assert all(tag in text for tag in ("实体/COPD", "实体/慢性阻塞性肺疾病", "实体/沙丁胺醇"))
    assert (
        "原因是这些标签分别属于别名、学习状态、审核状态、版本状态或主观评价，"
        "不具有稳定分类意义。" in text
    )


def test_specification_preserves_evidence_learning_empty_content_and_quality_contract() -> None:
    text = read(SPEC)
    required = (
        "四层之间不得相互污染。", "未经来源支持的模型补充", "unverified_chat 状态的 Claim",
        "只有学习者在没有直接照抄助手答案的情况下，独立表达正确知识，才可记录。",
        "助手刚讲完后，学习者只回复“对”“懂了”。", "学习者复述仍存在关键错误。",
        "学习者只选择了选项但没有体现推理。", "助手提供了完整答案后，学习者重复原话。",
        "必须保留学习者真实出现的错误逻辑，不得美化成正确答案。",
        "学习者明确表示不知道的问题", "对话结束时仍未确认的问题", "来源之间存在冲突的问题",
        "需要进一步查阅教材或指南的问题", "无适用内容时写“暂无”。",
        "未检索到可靠来源时写“暂无可靠来源支持”。", "仍需验证时写“待验证”。",
        "不适用于当前概念时删除对应三级标题，不能用虚构内容填充。",
        "不允许用“可能”“一般认为”等模糊表达掩盖来源不足。", "只输出完整 Markdown。",
        "不得把学习者错误写成医学事实。", "不得把未经验证的助手讲解写成确定结论。",
        "所有 Markdown 使用 LF 换行并以一个换行符结尾。",
    )
    assert all(item in text for item in required)
    assert all(item in text for item in QUALITY_CHECKLIST)


def test_composer_input_stages_are_complete_and_in_order() -> None:
    text = read(SPEC)
    composer_section = text[text.index("## 十三、推荐的 Composer 输入顺序"):]
    positions = [composer_section.index(stage) for stage in COMPOSER_STAGES]
    assert positions == sorted(positions)


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
        TEMPLATE: "7da5936abafbd3f43b68dcbd18c1318ad4522b8eb8f108bb4cead5ce1a6c015c",
        SPEC: "8860f76e9288c057929c960e0e03ca4b3b20e09c70c3f5979091f55ffda32330",
    }
    for path, digest in expected.items():
        # Changing either digest requires an intentional template-contract/version change.
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


def test_copd_learner_record_keeps_provenance_separate() -> None:
    text = read(COPD)
    learning_start = text.index("## 十六、学习记录")
    sources_start = text.index("## 十七、证据来源与版本")
    learning = text[learning_start:sources_start]
    medical = text[:learning_start]
    assert "本节仅记录学习表现，不构成医学事实来源" in learning
    assert "COPD 与吸烟关系不大" not in medical
    assert "COPD 与吸烟关系不大" not in learning
    assert "已验证" not in learning
    assert "### 已独立掌握\n\n- 暂无\n\n### 错误与纠正\n\n暂无" in learning
