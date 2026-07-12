# MedLearn Cloud Setup Record

记录日期：2026-07-11  
项目仓库：`fangzh05/medlearn-vault`  
当前主分支版本：`0.4.0`  
当前目标分支：`codex/reviewable-capture-proposals`

---

## 1. 已确定的最终架构

日常使用目标：

```text
手机 ChatGPT Work
→ Work 内置模型解析当前学习聊天
→ 生成结构化 CaptureDraft
→ Cloudflare Worker 接收并暂存任务
→ Worker 触发 GitHub Actions
→ GitHub Actions 运行 MedLearn
→ MedLearn 对照 ContractBundle 生成 CaptureProposal
→ 用户确认
→ 后续 GitHub Actions 写入加密 R2 Vault
→ Remotely Save 同步到手机 Obsidian
```

核心职责边界：

```text
ChatGPT Work
负责自然语言理解、知识点提取、错误识别和候选结构化。

MedLearn
不调用 LLM API。
只做确定性概念匹配、校验、去重、证据状态控制、提案生成和后续写入。

Cloudflare Worker
负责接收请求、鉴权、任务暂存、幂等控制和触发 GitHub Actions。

GitHub Actions
相当于临时云端执行环境，按需运行 MedLearn。

Cloudflare R2
保存控制数据和后续加密 Obsidian Vault。

Remotely Save
负责把 R2 中的 Vault 同步到手机 Obsidian。
```

因此日常运行不依赖个人电脑开机。电脑主要用于初始配置、开发和维护。

---

## 2. 当前仓库状态

当前 `main` 已实现：

- 冻结的医学领域模型；
- `ContractBundle`；
- 跨记录完整性校验；
- 精确 alias resolver；
- 双语医学术语渲染；
- GERD（胃食管反流病）和 COPD（慢性阻塞性肺疾病）示例；
- 通用主题 Markdown preview；
- CLI、Schema snapshot、pytest、ruff、mypy 和 CI。

当前尚未实现：

- `CaptureDraft`；
- `CaptureProposal`；
- Cloudflare Worker API；
- GitHub Actions `workflow_dispatch` 业务流程；
- R2 Vault adapter；
- Remotely Save 加密兼容写入；
- Work Skill；
- Obsidian writer；
- proposal approval / commit。

当前正在开发：

```text
PR #5
feat: add reviewable medical learning capture proposals
```

目标 package version：

```text
0.5.0
```

---

## 3. 已完成的 Cloudflare 配置

### 3.1 Cloudflare Worker

已创建 Worker：

```text
medlearn-cloud
```

当前 Worker 地址：

```text
https://medlearn-cloud.fzh050531.workers.dev
```

当前状态：

- 已部署；
- 仍为 Hello World 空壳；
- 暂未实现 `/v1/captures` 等业务接口；
- 暂未绑定 R2；
- 暂未触发 GitHub Actions。

后续计划接口：

```text
POST /v1/captures
GET  /v1/jobs/{job_id}
GET  /v1/proposals/{proposal_id}
POST /v1/proposals/{proposal_id}/commit
```

注意：这些接口必须等 PR #5 的 CaptureDraft / CaptureProposal Schema 稳定后再实现。

### 3.2 Cloudflare R2 Buckets

已创建两个 Bucket：

```text
medlearn-control
medlearn-vault
```

用途：

```text
medlearn-control
├── captures/
├── proposals/
├── jobs/
├── locks/
└── audit/
```

```text
medlearn-vault
└── Remotely Save / Rclone Crypt 管理的 Obsidian Vault 对象
```

设计要求：

- 控制数据与 Vault 数据分离；
- proposal、job 和审计记录不得混入 Obsidian 同步目录；
- Worker 后续优先通过 R2 Binding 访问 `medlearn-control`；
- GitHub Actions 后续通过 S3-compatible API 访问 R2；
- Vault 写入阶段再处理 Rclone Crypt。

### 3.3 R2 API Token

已创建：

```text
MedLearn-RW
```

权限：

```text
对象读取和写入
```

应用范围：

```text
所有存储桶
```

当前用于 GitHub Actions 访问 R2。

安全要求：

- Access Key ID 和 Secret Access Key 不得写入仓库；
- Secret Access Key 只在创建时完整显示；
- 若泄漏，应立即撤销并重新创建；
- 后续可以缩小到指定 Bucket 权限。

---

## 4. 已完成的 GitHub Secrets

仓库：

```text
fangzh05/medlearn-vault
```

位置：

```text
Settings
→ Secrets and variables
→ Actions
→ Repository secrets
```

已配置的 R2 Secrets：

```text
R2_ACCOUNT_ID
R2_ACCESS_KEY_ID
R2_SECRET_ACCESS_KEY
```

用途：

- GitHub Actions 访问 Cloudflare R2；
- 后续 propose workflow 下载 CaptureDraft；
- 后续 commit workflow 读取和写入 Vault。

禁止：

- 在 workflow 日志中输出这些值；
- 使用 `set -x`；
- 将密钥写入 artifact；
- 在来自 fork 的 PR workflow 中使用写权限 Secret。

---

## 5. Cloudflare Worker Secrets 规划

Worker 后续需要两个 Secret：

```text
GITHUB_TOKEN
WORKER_SHARED_SECRET
```

### 5.1 GITHUB_TOKEN

用途：

```text
Cloudflare Worker
→ 调用 GitHub REST API
→ 触发 workflow_dispatch
```

建议使用：

```text
GitHub Fine-grained Personal Access Token
```

建议权限：

```text
Repository access:
Only select repositories
→ fangzh05/medlearn-vault

Repository permissions:
Actions: Read and write
```

保存位置：

```text
Cloudflare
→ Workers & Pages
→ medlearn-cloud
→ Settings
→ Variables and Secrets
→ Secret
```

Secret 名称：

```text
GITHUB_TOKEN
```

MVP 先使用 Fine-grained PAT。后续如需要更严格的安全模型，可迁移到 GitHub App。

### 5.2 WORKER_SHARED_SECRET

用途：

```text
手机快捷指令 / Work App
→ 请求 medlearn-cloud Worker
→ Bearer Token 鉴权
```

建议生成 64 位随机字符串。

Secret 名称：

```text
WORKER_SHARED_SECRET
```

未来请求形式：

```http
Authorization: Bearer <WORKER_SHARED_SECRET>
```

注意：

- 该值放在 Cloudflare Worker Secret；
- 不需要放到 GitHub Actions；
- 不得写在 Worker 代码中；
- 不得以普通明文变量保存。

---

## 6. 当前尚未配置的 Secret

暂未配置：

```text
RCLONE_CRYPT_PASSWORD
RCLONE_CRYPT_SALT
```

原因：

- 当前还没有 Vault writer；
- 还未配置 Remotely Save 的 Rclone Crypt；
- 不应提前生成后再忘记密码来源。

这些将在 Rclone Crypt / R2 Vault adapter 阶段统一配置。

---

## 7. Remotely Save 规划

目标：

```text
Cloudflare R2
→ Remotely Save
→ 手机 Obsidian
```

Provider：

```text
S3-compatible
```

Endpoint 格式：

```text
https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com
```

Bucket：

```text
medlearn-vault
```

Region：

```text
auto
```

推荐实施顺序：

1. 先在测试 Vault 中验证未加密同步；
2. 确认手机可以上传和下载 Markdown；
3. 再切换到 Rclone Crypt；
4. GitHub Actions 使用相同 crypt 配置；
5. 最后再接 MedLearn commit workflow。

注意：

- Remotely Save 在手机后台不保证即时同步；
- 通常需要打开 Obsidian 或手动触发同步；
- 后端状态只能写“已写入远端 Vault，等待 Obsidian 同步”；
- 不得宣称“已同步到手机”，除非客户端确认。

---

## 8. Capture 工作流设计

### 8.1 Work 负责解析

Work 内置模型负责：

- 识别学习主题；
- 提取疾病、药物、检查和机制概念；
- 识别用户正确回答；
- 识别错误逻辑；
- 提取 assistant 的候选纠正；
- 生成结构化 CaptureDraft。

MedLearn 不需要连接 OpenAI API。

### 8.2 CaptureDraft

CaptureDraft 是：

```text
不可信、结构化、可校验的 Work 输出
```

不应默认包含完整聊天原文。

只应包含：

- 会话上下文；
- message ID；
- 必要短摘录；
- concept mentions；
- claim candidates；
- misconception candidates。

### 8.3 CaptureProposal

CaptureProposal 是：

```text
MedLearn 对照当前 ContractBundle 后生成的可审查提案
```

必须包含：

- proposal ID；
- draft digest；
- base bundle digest；
- proposal digest；
- concept resolutions；
- new concept candidates；
- claim proposals；
- learning capture candidate；
- issues；
- status。

状态：

```text
ready_for_review
blocked
```

任何 error 或 review issue 都应 blocked。

---

## 9. 两阶段写入原则

严禁：

```text
聊天
→ Worker
→ 直接写入 Vault
```

必须：

```text
阶段一：propose

CaptureDraft
→ MedLearn reconciliation
→ CaptureProposal
→ 用户审查
```

```text
阶段二：commit

用户确认
→ 校验 proposal_digest
→ 校验 base_bundle_digest
→ 生成 Vault diff
→ 写入
```

提交时至少携带：

```json
{
  "proposal_id": "proposal_...",
  "proposal_digest": "sha256:...",
  "expected_base_bundle_digest": "sha256:..."
}
```

目的：

- 防止审批后 proposal 被篡改；
- 防止知识库已变化仍按旧提案写入；
- 防止重复执行；
- 支持幂等。

---

## 10. 第一版 Vault 写入策略

第一版建议只做 append-only：

```text
MedLearn/Captures/
└── <timestamp>-<proposal_id>.md
```

暂不直接修改：

```text
Concepts/*.md
Reviews/*.md
Courses/*.md
```

原因：

- 手机 Remotely Save 可能同时同步；
- GitHub Actions 也可能写入；
- 多写入者修改同一个文件容易产生冲突；
- append-only 新文件更安全。

后续再通过 rebuild / compact job 派生概念页和复习页。

---

## 11. GitHub Actions 规划

当前仓库现有 CI 只负责：

```text
push / pull_request
→ ruff
→ mypy
→ pytest
→ schema check
```

后续新增两个业务 workflow：

```text
.github/workflows/medlearn-propose.yml
.github/workflows/medlearn-commit.yml
```

### propose workflow

触发方式：

```text
workflow_dispatch
```

输入只传：

```text
job_id
draft_object_key
draft_digest
```

不得传：

- 完整聊天；
- 医学笔记正文；
- R2 Secret；
- Vault 密码。

流程：

```text
下载 CaptureDraft
→ 安装 medlearn-vault
→ 读取当前 bundle
→ medlearn capture propose
→ 生成 CaptureProposal
→ 写回 medlearn-control
→ 更新 job 状态
```

### commit workflow

输入：

```text
proposal_id
proposal_digest
expected_base_bundle_digest
```

流程：

```text
读取 proposal
→ 验证 digest
→ 验证 bundle revision
→ 下载和解密 Vault
→ 生成 append-only capture
→ 加密上传 R2
→ 更新 job 状态
```

---

## 12. 当前开发顺序

推荐顺序：

```text
PR #5
CaptureDraft / CaptureProposal
确定性 reconciliation
Markdown review
workflow schema
```

```text
PR #6
Cloud intake contracts
Worker 请求、认证、幂等和 job 状态
```

```text
PR #7
GitHub Actions propose workflow
```

```text
PR #8
Approval contract
proposal digest + base bundle digest 校验
```

```text
PR #9
Rclone Crypt / R2 Vault adapter
append-only capture 写入
```

```text
PR #10
GitHub Actions commit workflow
```

```text
PR #11
iOS 快捷指令入口
```

```text
PR #12
Work Skill / App
```

---

## 13. 当前立即可做的操作

现在可以做：

- 检查 GitHub R2 Secrets 是否全部存在；
- 创建 Worker Secret `GITHUB_TOKEN`；
- 创建 Worker Secret `WORKER_SHARED_SECRET`；
- 建立测试 Obsidian Vault；
- 在 Remotely Save 中测试未加密 R2 同步；
- 创建最小 `workflow_dispatch` 测试 Action；
- 等待 PR #5 完成。

现在不要做：

- 正式 Worker API；
- 正式 GitHub Actions propose/commit；
- Rclone Crypt 写入；
- Work Skill；
- MCP Server；
- 自动写 Obsidian；
- 直接修改真实 Vault。

---

## 14. 安全检查清单

- [ ] GitHub 仓库为私有或不包含敏感数据；
- [ ] R2 Secret 不写入代码；
- [ ] Worker Secret 使用 Secret 类型；
- [ ] PAT 只授权单一仓库；
- [ ] PAT 只给 Actions read/write；
- [ ] Worker 请求使用 Bearer Token；
- [ ] Draft 不包含完整聊天；
- [ ] 日志不输出 excerpt；
- [ ] GitHub Action 不上传明文 Vault artifact；
- [ ] 不对 fork PR 暴露写权限 Secret；
- [ ] Vault writer 使用临时目录；
- [ ] job 结束删除明文；
- [ ] 第一版只 append-only；
- [ ] commit 前验证 proposal digest；
- [ ] commit 前验证 base bundle digest；
- [ ] 重复 job 必须幂等。

---

## 15. 当前进度总结

已完成：

```text
✅ Cloudflare Worker: medlearn-cloud
✅ Worker URL
✅ R2 Bucket: medlearn-control
✅ R2 Bucket: medlearn-vault
✅ R2 API Token: MedLearn-RW
✅ GitHub R2 Repository Secrets
✅ 核心架构确认
✅ Work / MedLearn 职责边界确认
```

进行中：

```text
🔄 PR #5 CaptureDraft / CaptureProposal
```

待完成：

```text
⏳ Worker Secrets
⏳ Remotely Save 测试
⏳ workflow_dispatch 测试
⏳ Cloud intake
⏳ propose workflow
⏳ approval
⏳ encrypted Vault writer
⏳ commit workflow
⏳ iOS Shortcut
⏳ Work Skill / App
```
