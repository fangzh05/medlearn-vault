# MedLearn Vault

## Local source search

`medlearn sources index --input-root <chunked-root> --output <index.sqlite3>`
builds a private local SQLite index from existing chunking outputs; `medlearn
sources search QUERY --index <index.sqlite3>` provides deterministic lexical
search. It has no network, semantic, embedding, or publication behavior. See
[local source search](docs/local-source-search.md).

## Deterministic chapter-aware chunking

`medlearn sources chunk --input-root <source-root> --output-root <output-root>`
builds local `sections.jsonl`, `chunks.jsonl`, and a privacy-safe report from
normalized pages only. It is deterministic, page-mapped, transaction-safe, and
does not perform OCR, indexing, retrieval, embeddings, or cloud processing. See
[chapter chunking](docs/chapter-chunking.md).

## Private native PDF source extraction

An optional, local-only native-text extractor is available as `medlearn sources extract-pdf`; it preserves PDF page order in JSONL/TXT inspection outputs and does not perform OCR, indexing, or publication. See [native PDF extraction](docs/native-pdf-extraction.md). Private PDFs and generated text must not be committed.

## Source normalization

`medlearn sources normalize` conservatively normalizes immutable extracted page JSONL into local normalized JSONL, with explicit local page exclusions and no OCR, retrieval, indexing, or LLM behavior. See [source normalization](docs/source-normalization.md).

[中文](#中文说明) · [English](#english)

## 中文说明

MedLearn Vault 是一个面向跨学科医学学习知识库的本地优先契约层。它用于管理规范化医学概念、跨学科章节资料、带来源约束的医学陈述、学习表现与错误纠正，并通过严格的身份、摘要、Schema 和不可变写入规则保证数据可追踪、可审计、可重建。

当前仓库已经实现：

- 永久医学概念标识、匹配指纹与外部编码标识；
- 版本化 JSON Schema、严格 Pydantic 模型、CLI、测试与 CI；
- `CaptureDraft → CaptureProposal → Approval → VaultPublicationPlan → Vault publication` 的确定性控制链路；
- Cloudflare Worker + R2 的单用户云端采集与只读发布接口；
- Windows 端 `medlearn sync` 只读增量同步，可将读者优先的学习记录与概念图写入现有 Obsidian Vault 的 `MedLearn/` 目录；
- `MedLearnHandoff` 结构化导入，以及缺失来源或概念时的人工目录更新与显式 reproposal 流程。
- 可选的完整题目作答记录、对话内解释和持久化 GPT 生成解释；渲染与 Windows 同步不会重复调用模型。

系统不会调用 LLM API。语言理解由 ChatGPT Work 完成，MedLearn 只接收结构化输入，并以确定性规则进行校验、提案、审批、发布和同步。

### 当前架构

```text
ChatGPT Work
→ authenticated Worker intake
→ medlearn-control R2
→ deterministic Proposal / Review / automatic Approval when eligible
→ immutable VaultPublicationPlan
→ medlearn-vault R2
→ authenticated read-only manifest/files API
→ Windows sync client
→ Obsidian Vault/MedLearn
```

控制面与发布面严格分离：

- `medlearn-control` 保存 intake、job、execution、proposal、review、approval 和 publication plan；
- `medlearn-vault` 保留经过批准后发布的不可变 Capture JSON、Markdown 和 publication receipt，并提供可重建的读者投影；
- Worker 的采集凭据与 Vault 同步凭据完全隔离；
- 发布使用 create-only 语义，不覆盖既有对象；
- Windows 同步只允许写入本地 Vault 的 `MedLearn/` 子目录，并对 manifest、digest、字节长度和本地冲突进行校验。

### 关键契约

- `ConceptEntity`：一个永久医学概念身份，别名、语义范围、外部编码、学科 lens 和关系均独立建模。
- `MedicalClaim`：受来源治理的医学陈述。未验证聊天内容不能标记为已支持；source-backed claim 必须具有引用。
- `ChapterDossier`：只保存前向概念引用，反向链接由系统派生。
- `LearningCapture`：不可变的学习观察记录；`LearnerState` 是可重建投影。
- `VaultPublicationPlan`：绑定精确 JSON 与 Markdown 字节的不可变发布计划。
- `VaultPublicationReceipt`：发布完成后的不可变收据，也是只读 manifest 的来源。
- Obsidian 概念图：由已解析目录概念生成的 Markdown 投影；详见 `docs/obsidian-concept-graph.md`。
- ID 为永久且不透明的身份；匹配指纹只用于去重与候选解析，不能替代永久 ID。

### Handoff 与目录补全

`MedLearnHandoff` 只包含上下文、消息 ID、短证据摘录和结构化候选，不保存完整对话。陈述归属只能来自被引用消息的角色。

当 Handoff 中出现仓库目录尚未收录的学习来源或医学概念时，Proposal 会以 `CATALOG_UPDATE_REQUIRED` 阻断。系统不会自动创建正式医学概念。正确流程为：生成目录更新提案、人工补全元数据并审查、合并目录 PR，然后通过显式 reproposal 使用原始不可变 Intake 重新生成 Proposal。

### 安装与本地验证

要求 Python 3.12+。

```powershell
python -m pip install -e ".[dev]"
medlearn doctor
medlearn schema export
medlearn schema check
medlearn concept validate concept.json
medlearn bundle validate examples/gerd
medlearn bundle validate examples/copd
medlearn preview render examples/gerd preview.md --topic GERD
medlearn capture validate-draft examples/capture/copd-session/draft.json
medlearn capture propose examples/copd examples/capture/copd-session/draft.json proposal.json
medlearn capture review examples/copd proposal.json proposal.md
pytest
```

Worker 验证：

```powershell
cd worker
npm install
npm run lint
npm run typecheck
npm test
npm run contracts:check
```

### Schema 与 CI

持久化 Schema 位于 `schemas/current/`，工作流 Schema 位于 `schemas/workflow/current/`，控制面 Schema 位于 `schemas/control/current/`。CI 会在内存中重新生成 Schema；模型发生变化但没有同步更新快照和迁移说明时，检查会失败。

Bundle warning 会输出警告但仍返回成功；完整性错误会返回非零退出码。Preview 主题缺失、歧义、已废弃或仍待 split review 时同样返回非零退出码。

### 使用边界

该项目是单用户、审计优先的医学学习基础设施，不是临床决策系统，也不应被视为医学事实自动验证器。未经权威来源支持的聊天内容不会被提升为医学事实；审批步骤有意保留人工确认。

当前没有自动概念合并、自动 claim 验证、完整对话归档、任意 Vault 写入、删除或远程修改能力。Obsidian 同步是只读远端、受限本地写入模型。

更详细的契约、部署和迁移说明见 `docs/`。

### Medical Note V1

- `templates/medical_note_v1.md` 是固定输出结构契约。
- `docs/medical-note-template-specification.md` 定义生成与维护规则。
- `examples/medical_note_v1/COPD.md` 是完成的黄金示例：只提供样式和信息密度指导，不是模板，也不得作为其他概念的医学证据。
- 主章节的稳定语义顺序保持不变；概念特异的子章节可以调整。学习者表现与医学事实始终隔离。

---

## English

MedLearn Vault is a local-first contract layer for a cross-disciplinary medical learning vault. It manages canonical medical concepts, cross-disciplinary chapter dossiers, source-governed medical claims, learner evidence, and misconception correction through strict identity, digest, schema, and immutable-write rules.

Fast composition is a separate local draft-note preview: 结构或存储不安全才失败；知识不完整只告警。It never certifies medical correctness or changes the strict approval/publication path.

`medlearn compose preview` defaults to the local `stub` composer. Explicit `--composer deepseek --prompt prompts/deepseek_note_composer_v1.md` is a network opt-in that writes only a local unreviewed preview; it never publishes or syncs notes. See [fast composition](docs/fast-composition.md).

The repository currently implements:

- permanent medical concept identifiers, matching fingerprints, and external coding identifiers;
- versioned JSON Schema, strict Pydantic models, a CLI, tests, and CI;
- a deterministic `CaptureDraft → CaptureProposal → Approval → VaultPublicationPlan → Vault publication` control chain;
- a single-user Cloudflare Worker + R2 intake and read-only publication API;
- a read-only Windows `medlearn sync` client that writes published content only under `MedLearn/` in an existing Obsidian Vault;
- structured `MedLearnHandoff` import and a manually reviewed catalog-update/reproposal lifecycle for missing sources or concepts.
- optional full assessment attempts, conversation-derived explanations, and persisted GPT-generated explanations; rendering and Windows sync never regenerate prose.

MedLearn does not call an LLM API. ChatGPT Work performs language understanding; MedLearn accepts structured input and applies deterministic validation, proposal, approval, publication, and synchronization rules.

### Architecture

```text
ChatGPT Work
→ authenticated Worker intake
→ medlearn-control R2
→ deterministic Proposal / Review / automatic Approval when eligible
→ immutable VaultPublicationPlan
→ medlearn-vault R2
→ authenticated read-only manifest/files API
→ Windows sync client
→ Obsidian Vault/MedLearn
```

The control plane and publication plane are strictly separated:

- `medlearn-control` stores intakes, jobs, executions, proposals, reviews, approvals, and publication plans;
- `medlearn-vault` stores only approved immutable Capture JSON, Markdown, and publication receipts;
- intake credentials and Vault synchronization credentials are isolated;
- publication uses create-only semantics and never overwrites existing objects;
- the Windows client writes only inside the local Vault's `MedLearn/` directory and verifies manifests, digests, byte lengths, and local conflicts.

### Core contracts

- `ConceptEntity`: one permanent medical identity. Aliases, semantic scope, external identifiers, discipline lenses, and relations are modeled independently.
- `MedicalClaim`: source-governed medical evidence. Unverified chat content cannot be marked supported, and source-backed claims require citations.
- `ChapterDossier`: stores forward concept references only; backlinks are derived.
- `LearningCapture`: an immutable learner-observation record; `LearnerState` is a rebuildable projection.
- `VaultPublicationPlan`: an immutable plan containing exact JSON and Markdown publication bytes.
- `VaultPublicationReceipt`: an immutable publication receipt and the source for the read-only manifest.
- IDs are opaque and permanent. Computed fingerprints are used only for matching and deduplication.

### Handoff and catalog completion

`MedLearnHandoff` contains context, message IDs, short evidence excerpts, and structured candidates, not complete chat transcripts. Assertion ownership is derived only from referenced message roles.

When a Handoff introduces a learning source or medical concept that is absent from the repository-controlled catalog, the Proposal is blocked with `CATALOG_UPDATE_REQUIRED`. The system does not auto-promote candidates into permanent concepts. The safe lifecycle is: generate a catalog-update proposal, manually complete and review metadata, merge the catalog PR, then explicitly repropose from the original immutable Intake.

### Installation and local validation

Python 3.12+ is required.

```powershell
python -m pip install -e ".[dev]"
medlearn doctor
medlearn schema export
medlearn schema check
medlearn concept validate concept.json
medlearn bundle validate examples/gerd
medlearn bundle validate examples/copd
medlearn preview render examples/gerd preview.md --topic GERD
medlearn capture validate-draft examples/capture/copd-session/draft.json
medlearn capture propose examples/copd examples/capture/copd-session/draft.json proposal.json
medlearn capture review examples/copd proposal.json proposal.md
pytest
```

Worker validation:

```powershell
cd worker
npm install
npm run lint
npm run typecheck
npm test
npm run contracts:check
```

### Schema and CI

Persistent schemas live in `schemas/current/`, workflow schemas in `schemas/workflow/current/`, and control-plane schemas in `schemas/control/current/`. CI regenerates schemas in memory and fails when a model changes without an intentional snapshot and migration-note update.

Bundle warnings are printed but return success; integrity errors return a nonzero exit code. Missing, ambiguous, deprecated, or pending-split preview topics also return nonzero.

### Operational boundary

This project is single-user, audit-first medical learning infrastructure. It is not a clinical decision system and must not be treated as an automatic medical-fact verifier. Chat content is never promoted to medical truth without authoritative source support, and manual approval is intentionally retained.

The project does not provide automatic concept merging, automatic claim verification, full-transcript storage, arbitrary Vault writes, deletion, or remote modification. Obsidian synchronization is a read-only remote and bounded local-write model.

See `docs/` for detailed contracts, deployment notes, and migration records.
