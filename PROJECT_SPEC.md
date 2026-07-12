# MedLearn Vault v2.1 — 医学知识编译、跨学科关联与学习状态系统

> 日期：2026-07-11（北京时间）  
> 目标：把课件、教材、考试范围、题库、指南和学习对话，编译成可追溯、跨学科、考试导向、可持续更新的 Obsidian 医学知识库。  
> 最终发布：Obsidian 知识库、章节复习讲义、病例推理训练和 PDF。

---

## 1. 项目定位

MedLearn Vault 不是聊天记录导出器，也不是普通笔记整理器。

它包含四个核心子系统：

1. **课程资料库**：管理课件、教材、考试范围、教师强调、题库、指南和对话。
2. **医学知识编译器**：把分散资料编译为教材式章节，解释“现象 → 机制 → 临床意义 → 检查/治疗意义 → 考试怎么考”。
3. **跨学科概念图谱**：同一种疾病、药物、检查或机制可以出现在多个科目中，但共享统一概念实体。
4. **学习状态系统**：记录用户的真实作答证据、错误逻辑、复习记录和掌握变化。

总体链路：

```text
课件 / 教材 / 考试范围 / 题库 / 指南 / 学习对话
                         ↓
                   Source Registry
                         ↓
            Medical Claims + Exam Signals
                         ↓
             Canonical Concept Registry
                         ↓
          Discipline-specific Chapter Dossiers
                         ↓
    Learner Model / Misconceptions / Review Scheduler
                         ↓
        Obsidian / Dashboard / Markdown / PDF
```

---

## 2. 最关键的知识架构

### 2.1 “概念”不属于某一个科目

疾病、药物、检查、解剖结构和病理机制是医学实体，不能被文件夹结构绑死。

例如“胃食管反流病”可能同时出现在：

- 内科学：发病机制、症状、诊断、药物治疗；
- 外科学：抗反流手术适应证、术式和并发症；
- 病理学：食管炎、Barrett 食管、上皮内瘤变；
- 药理学：PPI、H2RA、促动力药机制和不良反应；
- 医学影像学：钡餐、裂孔疝和并发症影像；
- 生理学：LES、食管清除和胃排空；
- 肿瘤学：Barrett 食管与食管腺癌风险。

因此系统必须建立唯一概念实体：

```yaml
concept_id: disease_gerd
canonical_name: 胃食管反流病
aliases: [GERD, gastroesophageal reflux disease, 反流病]
concept_type: disease
```

科目章节只保存该概念在本学科中的**知识视角**，不重复创造概念。

### 2.2 统一概念实体 + 多学科视角

```text
[[胃食管反流病]]
    ├── 内科学视角
    ├── 外科学视角
    ├── 病理学视角
    ├── 药理学视角
    ├── 影像学视角
    └── 肿瘤学视角
```

每个 `DisciplineLens` 包含：

- 学科；
- 课程；
- 该学科关心的问题；
- 对应章节；
- 核心知识单元；
- 该学科考试重点；
- 与其他学科的接口；
- 该学科独有术语和标准。

### 2.3 一词多义和别名消歧

同一词可能有多种医学含义，例如：

- “休克”可指综合征，也可能出现在病理生理、急诊、麻醉和外科；
- “阻滞”可指神经阻滞、传导阻滞或受体阻滞；
- “CA”可能指钙、癌或心脏骤停。

系统不能只靠字符串自动建双链。必须执行：

```text
Term Mention
  → Alias Resolver
  → Candidate Concepts
  → Context + Discipline + Neighbouring Terms
  → Canonical Concept ID
  → Confidence
  → 人工确认（低置信度）
```

### 2.4 概念页和课程页分离

概念页回答：

> 这个医学实体整体是什么？它与哪些实体、学科和章节有关？

课程章节回答：

> 在本课程和本章中，我需要学这个实体的哪些部分？考试怎么考？

不得把所有学科内容硬塞进一个超长概念页，也不得让每个学科产生互不关联的重复页面。

---

## 3. 六层知识模型

### Layer 0：原始来源层 Source Evidence

包括：

- 考试范围；
- 教师课件；
- 教师强调；
- 指定教材；
- 题库；
- 指南、共识、论文和权威数据库；
- 用户课堂笔记；
- 学习对话；
- 图片、表格和病例资料。

每条来源保存页码、幻灯片号、题号、消息 ID 或时间戳。

### Layer 1：医学陈述层 Medical Claims

最小可验证陈述：

```yaml
claim_id: cl_xxx
subject: "GERD 的核心机制"
statement: "抗反流屏障功能异常和食管清除下降是核心机制。"
claim_type: mechanism
concept_ids: [disease_gerd, anatomy_les]
citations: [...]
verification_status: verified_reference
medical_authority: 5
course_relevance: 4
```

### Layer 2：统一概念图谱 Concept Graph

节点：

- disease；
- syndrome；
- symptom；
- sign；
- anatomy；
- physiology；
- pathology；
- mechanism；
- investigation；
- imaging_sign；
- drug；
- procedure；
- complication；
- score；
- guideline；
- organism；
- gene；
- biomarker。

关系：

```text
is_a
part_of
causes
risk_factor_for
leads_to
manifests_as
diagnosed_by
differentiated_from
treated_by
contraindicates
complicated_by
associated_with
progresses_to
measured_by
located_in
acts_on
exam_confused_with
```

### Layer 3：教材式章节层 Chapter Dossier

按专题类型编译，形成可替代普通教科书进行课程复习的章节。

### Layer 4：考试映射层 Exam Intelligence

记录考试范围、教师强调、题库频次、题型、病例触发线索和错误选项。

### Layer 5：学习状态层 Learner Model

只根据用户作答和复习表现更新掌握状态。

### Layer 6：视图与发布层

生成：

- 概念总览页；
- 学科章节页；
- 跨学科关系页；
- 每日学习日志；
- 错题本；
- 复习 Dashboard；
- 章节讲义；
- PDF。

---

## 4. 双轴来源评价

### 4.1 医学权威性 medical_authority

反映陈述是否可靠和当前有效：

- 5：当前权威指南、共识、指定教材明确支持；
- 4：权威参考教材、系统综述、权威数据库；
- 3：高质量论文或课程课件；
- 2：题库答案、教师非正式材料；
- 1：用户笔记或学习对话；
- 0：来源不明。

### 4.2 课程相关性 course_relevance

反映该内容在当前课程中的应试价值：

- 5：考试范围或教师明确要求；
- 4：课件重点、题库反复出现；
- 3：指定教材核心正文；
- 2：课程相关补充；
- 1：跨学科拓展；
- 0：与当前课程无关。

这两个维度必须独立。

课件与指南冲突时应保存：

```yaml
course_answer: "本课程题库采用的答案"
current_evidence: "当前指南标准"
conflict_note: "考试作答与临床现行标准的差异"
```

---

## 5. 教材式知识内容架构

所有重点解释应尽量使用：

```text
现象
  → 为什么发生
  → 对患者意味着什么
  → 检查为什么异常
  → 治疗为什么有效
  → 考试如何包装题干
```

### 5.1 疾病专题 DiseaseTopic

1. 学习目标；
2. 在医学体系中的位置；
3. 核心定义与通俗解释；
4. 病因、诱因和危险因素；
5. 发病机制 / 病理生理；
6. 病理改变；
7. 临床表现；
8. 实验室检查；
9. 辅助检查 / 影像 / 特殊检查；
10. 诊断依据、确诊依据、分型分级；
11. 鉴别诊断；
12. 治疗原则及机制；
13. 并发症；
14. 病例推理；
15. 高频考点；
16. 易错点与陷阱；
17. 记忆框架；
18. 跨学科关联。

### 5.2 操作专题 ProcedureTopic

1. 临床用途；
2. 相关解剖；
3. 适应证；
4. 禁忌证；
5. 术前评估；
6. 器材；
7. 体位和定位；
8. 无菌、麻醉；
9. 操作步骤；
10. 成功标志；
11. 标本处理；
12. 并发症；
13. 失败原因；
14. OSCE 评分点；
15. 高频失分点；
16. 跨学科关联。

### 5.3 药物专题 DrugTopic

1. 分类；
2. 靶点和作用机制；
3. 药代动力学；
4. 适应证；
5. 剂量与给药；
6. 不良反应及机制；
7. 禁忌证；
8. 相互作用；
9. 特殊人群；
10. 同类药比较；
11. 监测；
12. 中毒与解救；
13. 高频考点；
14. 临床与各科应用关联。

### 5.4 检查 / 影像专题 InvestigationTopic

1. 原理；
2. 适应证；
3. 禁忌证；
4. 检查过程；
5. 正常表现；
6. 典型异常；
7. 阈值；
8. 诊断价值；
9. 局限；
10. 与其他检查比较；
11. 结果解释路径；
12. 病例判读；
13. 高频误判；
14. 跨学科使用场景。

### 5.5 机制专题 MechanismTopic

1. 正常生理；
2. 初始扰动；
3. 因果链；
4. 代偿；
5. 失代偿；
6. 器官后果；
7. 临床现象；
8. 检查对应；
9. 治疗干预点；
10. 反事实问题；
11. 高频混淆；
12. 跨疾病与跨学科关联。

模板允许省略不适用部分，但必须记录省略原因。不得为了满足模板制造空洞内容。

---

## 6. 章节中的结构化组件

### 6.1 MechanismChain

```text
诱因 / 原始病变
  → 分子或生理改变
  → 器官功能变化
  → 症状与体征
  → 实验室 / 影像异常
  → 治疗靶点
```

### 6.2 ClinicalFeature

字段：

- 表现；
- 机制；
- 阶段；
- 严重度；
- 临床意义；
- 题干中的常见表达；
- 鉴别价值。

### 6.3 InvestigationEntry

字段：

- 检查看什么；
- 为什么异常；
- 阈值；
- 适应证；
- 局限；
- 诊断价值；
- 常见误判；
- 来源版本。

### 6.4 DifferentialMatrix

至少比较：

- 病因；
- 病史；
- 症状；
- 体征；
- 实验室；
- 影像；
- 确诊依据；
- 治疗方向；
- 排除关键点。

### 6.5 TreatmentDecision

```text
患者状态
  → 是否紧急
  → 适应证 / 禁忌证
  → 首选处理
  → 为什么
  → 监测
  → 无效后的升级
```

### 6.6 CaseReasoning

1. 主诉；
2. 关键病史；
3. 阳性和阴性体征；
4. 检查；
5. 问题表征；
6. 候选诊断；
7. 排除路径；
8. 最可能诊断；
9. 下一步检查；
10. 初始处理；
11. 题目陷阱。

---

## 7. 跨学科知识关联

### 7.1 Concept Registry

所有术语先进入统一注册表：

```yaml
concept_id: disease_gerd
canonical_name: 胃食管反流病
preferred_english: gastroesophageal reflux disease
aliases:
  - GERD
  - 反流病
concept_type: disease
status: active
```

### 7.2 DisciplineLens

```yaml
lens_id: lens_gerd_internal_medicine
concept_id: disease_gerd
discipline_id: internal_medicine
course_id: internal_medicine_2026
focus_questions:
  - 为什么发生反酸和烧心？
  - 如何诊断和选择检查？
  - 如何进行药物治疗？
chapter_refs:
  - chapter_internal_gerd
knowledge_unit_refs: [...]
exam_point_refs: [...]
```

### 7.3 Concept Hub Page

Obsidian 中每个核心概念生成一个 Hub：

```markdown
# 胃食管反流病

## 基本身份
## 别名
## 核心定义
## 关键关系
## 各学科视角
- [[内科学/胃食管反流病]]
- [[外科学/抗反流手术]]
- [[病理学/反流性食管炎与Barrett食管]]
- [[药理学/PPI与抑酸治疗]]
- [[影像学/食管钡餐与裂孔疝]]

## 相关概念
## 资料来源
## 用户掌握概览
```

### 7.4 跨学科关系类型

除普通医学关系外，增加：

```text
discipline_view_of
taught_in
examined_in
prerequisite_for
clinically_connects_to
pathology_basis_of
pharmacologic_target_of
imaging_correlate_of
surgical_management_of
```

### 7.5 去重规则

以下内容不得重复建概念：

- 同一疾病的中英文名；
- 缩写和全称；
- 教材旧译名和新译名；
- 同一药物的通用名和商品名；
- 同一评分的英文和中文名。

不同概念不得因为名称相近而自动合并。合并置信度不足 0.90 必须人工确认。

---

## 8. 图像和表格系统

### 8.1 图片来源优先级

1. 上传课件 / 教材 / 病例原图；
2. 系统重绘的机制图、流程图、解剖图；
3. 外部权威图片。

每张图保存：

- 来源；
- 页码 / 幻灯片；
- 版权许可；
- 图像用途；
- 必须观察的结构；
- 图注；
- “这张图要让我看懂什么”；
- 对应知识点和考试点。

### 8.2 FigureSpec

必须支持：

- anatomy；
- mechanism；
- pathology；
- procedure；
- imaging；
- diagnostic_flow；
- treatment_flow；
- differential；
- drug_mechanism；
- case_reasoning。

### 8.3 表格质量

高质量表格应拥有统一比较轴或决策价值。系统应检测：

- 列是否有明确比较维度；
- 是否存在重复正文；
- 是否过宽；
- 是否适合拆分；
- 是否需要转换为流程图。

---

## 9. 考试映射

### 9.1 CourseMap

```text
课程
  ├── 章节
  │   ├── 考试范围
  │   ├── 教师学习目标
  │   ├── 课件重点
  │   ├── 题库知识点
  │   ├── 对应概念
  │   └── 对应章节知识单元
```

### 9.2 高频证据

只有以下情况可标记高频：

- 题库反复出现；
- 历年题重复；
- 教师明确强调；
- 考试范围明确要求；
- 同一概念以多种题型出现。

模型主观判断不能产生 `high_frequency`。

### 9.3 每章结尾输出

- 本章必背；
- 本章必须理解；
- 本章容易出病例题；
- 本章容易出选择题陷阱；
- 本章只需知道、不应过度展开。

---

## 10. 学习状态

掌握状态：

```text
not_assessed
unknown
misconceived
partial
prompted
independent
stable
```

证据类型：

```text
correct_independent
correct_after_hint
guessed_correct
partial
unknown
incorrect
high_confidence_incorrect
self_report_only
```

规则：

- 助手解释过不等于用户掌握；
- 猜对不能算独立掌握；
- 自评“会了”不能直接升级；
- `stable` 必须有跨时间独立答对；
- 高信心错优先复习；
- 同一错误逻辑跨学科复发时，链接到同一 `Misconception`。

---

## 11. 数据模型

### 11.1 ConceptEntity

见 `schemas/concept_entity.schema.json`。

### 11.2 MedicalClaim

```yaml
claim_id: cl_<stable-hash>
claim_type: mechanism
statement: string
concept_ids: [...]
discipline_ids: [...]
citations: [...]
verification_status: source_backed
medical_authority: 4
course_relevance:
  internal_medicine_2026: 5
```

### 11.3 ChapterDossier

见 `schemas/chapter_dossier.schema.json`。

### 11.4 LearningCapture

见 `schemas/learning_capture.schema.json`。

---

## 12. Vault 目录

```text
MedLearnVault/
├── 00_Inbox/
│   ├── Sources/
│   ├── Captures/
│   └── Pending-Claims/
├── 10_Sources/
│   ├── Course-Scope/
│   ├── Slides/
│   ├── Textbooks/
│   ├── Question-Banks/
│   ├── Guidelines/
│   └── Learning-Chats/
├── 20_Concepts/
│   ├── Diseases/
│   ├── Mechanisms/
│   ├── Anatomy/
│   ├── Investigations/
│   ├── Drugs/
│   └── Procedures/
├── 30_Disciplines/
│   ├── 内科学/
│   ├── 外科学/
│   ├── 病理学/
│   ├── 药理学/
│   ├── 麻醉学/
│   └── 医学影像学/
├── 40_Course-Maps/
├── 50_Learner/
│   ├── Misconceptions/
│   ├── Mastery/
│   ├── Open-Questions/
│   └── Review-Logs/
├── 60_Exam/
│   ├── High-Frequency/
│   ├── Case-Patterns/
│   └── Traps/
├── 70_Media/
│   ├── Source-Images/
│   ├── Redrawn/
│   └── Figure-Specs/
├── 80_Publications/
│   ├── Markdown/
│   └── PDF/
├── 90_Dashboards/
└── 98_System/
    ├── Transactions/
    ├── Conflicts/
    ├── Audit/
    └── Index/
```

---

## 13. 核心模块

```text
src/medlearn_vault/
├── domain/
│   ├── concepts.py
│   ├── sources.py
│   ├── claims.py
│   ├── chapters.py
│   ├── learner.py
│   ├── exams.py
│   ├── media.py
│   └── mutations.py
├── ingest/
├── registry/
│   ├── concept_registry.py
│   ├── alias_resolver.py
│   ├── source_registry.py
│   └── course_map.py
├── graph/
│   ├── relations.py
│   ├── backlinks.py
│   ├── discipline_lenses.py
│   └── graph_queries.py
├── extraction/
├── compiler/
│   ├── archetypes.py
│   ├── chapter_compiler.py
│   ├── concept_aligner.py
│   ├── mechanism_builder.py
│   ├── differential_builder.py
│   ├── treatment_builder.py
│   ├── case_builder.py
│   └── conflict_resolver.py
├── learner/
├── media/
├── quality/
├── vault/
├── index/
├── publish/
└── mcp/
```

---

## 14. CLI

```text
medlearn init
medlearn doctor

medlearn source ingest <path>
medlearn source inspect <source-id>

medlearn concept resolve "<term>" --discipline internal_medicine
medlearn concept show <concept-id>
medlearn concept backlinks <concept-id>
medlearn concept merge <a> <b> --dry-run
medlearn concept split <concept-id> --dry-run

medlearn course build-map <course-id>
medlearn course coverage <course-id>

medlearn chapter compile <chapter-id> --dry-run
medlearn chapter validate <chapter-id>
medlearn chapter publish <chapter-id>

medlearn capture current <transcript>
medlearn review due
medlearn audit
medlearn reindex
```

---

## 15. Skill 触发与流程

触发：

- 归档本次学习；
- 更新错题本；
- 把这次学习同步到 Obsidian；
- 编译这一章；
- 建立跨学科关联；
- 这个病在其他科目学过什么；
- 生成本章复习讲义。

Skill 处理当前会话时：

1. 保存原始对话；
2. 识别概念 mention；
3. 解析到 canonical concept；
4. 识别用户掌握证据；
5. 对齐各学科章节；
6. 更新统一概念 Hub 的 backlinks；
7. 更新本学科章节；
8. 生成变更预览；
9. 确认后写入。

---

## 16. 技术栈

```text
Python 3.12+
Pydantic v2
Typer
Jinja2
httpx
ruamel.yaml
sqlite3 + FTS5
pytest
mcp Python SDK（可选）
```

MVP 不引入：

- LangChain；
- LlamaIndex；
- Neo4j；
- 外部向量数据库；
- Redis；
- 云数据库。

图谱第一版用 SQLite 关系表 + Markdown 双链实现，可重建。后续再评估专用图数据库。

---

## 17. 质量门禁

章节发布前必须检查：

1. 考试范围是否全覆盖；
2. 教师重点是否映射；
3. 每个核心陈述是否有来源；
4. 机制是否存在完整因果链；
5. 诊断阈值和药物剂量是否有版本；
6. 课程答案和当前指南是否冲突；
7. 必须配图内容是否有 FigureSpec；
8. 图注是否解释“看什么”；
9. 是否包含病例推理；
10. 高频是否有题库或教师证据；
11. 是否存在重复概念；
12. 每个主要概念是否建立跨学科链接；
13. 是否将助手解释误判为用户掌握；
14. 是否存在低置信度自动合并。

章节状态：

```text
draft
source_gap
conflict_review
content_review
publishable
published
```

---

## 18. 开发阶段

### P0：核心契约

- ConceptEntity；
- DisciplineLens；
- MedicalClaim；
- ChapterDossier；
- LearningCapture；
- stable ID；
- CLI；
- config；
- tests；
- CI。

### P1：概念注册表和图谱

- alias resolver；
- canonical concept；
- relation store；
- discipline lens；
- concept hub renderer；
- merge/split preview；
- backlinks。

### P2：来源摄取和课程地图

- PDF / PPTX / DOCX / Markdown；
- source locator；
- course scope；
- question bank；
- coverage matrix。

### P3：章节编译器

- TopicArchetype；
- 机制链；
- 检查；
- 鉴别；
- 治疗决策；
- 病例；
- FigureSpec；
- exam summary。

### P4：学习会话归档

- mastery evidence；
- misconception；
- open questions；
- review scheduler；
- chapter alignment。

### P5：Obsidian 和 MCP

- REST adapter；
- preview / commit；
- transaction；
- rollback；
- Skill；
- MCP tools。

### P6：PDF 发布

- 图文布局；
- 表格；
-目录；
- 页码；
- 引用；
- 质量报告。

---

## 19. 第一轮 Codex 开发范围

只实施 P0，不连接真实 Obsidian，不调用 LLM，不解析 PDF。

必须优先建立：

- `ConceptEntity`
- `ConceptAlias`
- `ConceptRelation`
- `DisciplineLens`
- `MedicalClaim`
- `ChapterDossier`
- `LearningCapture`
- stable fingerprint
- timezone-aware datetime
- Vault 相对路径验证
- JSON Schema
- tests
- CI

并加入以下测试：

1. “GERD”“胃食管反流病”解析到同一概念；
2. 同一概念可以拥有多个 DisciplineLens；
3. 两个学科章节引用同一 concept_id；
4. 同名异义词不会自动合并；
5. 概念 merge 必须生成预览；
6. 中文别名 round-trip；
7. stable ID 跨运行一致；
8. 关系图可序列化；
9. 学习证据不改变概念医学事实；
10. 不允许绝对 Vault 路径。

不要一次性实现完整系统。
