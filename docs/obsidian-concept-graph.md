# Obsidian 概念图

Obsidian Graph View 的节点是文件。因此 MedLearn 以可重建的 presentation projection
生成一个概念 Markdown 文件，而不是把普通词语或原始 ID 当作节点。

可见 Vault 布局仅为：

- `MedLearn/学习记录/YYYY/MM/<主题>｜<日期>〔<capture 后八位>〕.md`
- `MedLearn/概念/<规范中文名>.md`；若规范名冲突，使用
  `<规范中文名>〔<concept_id 后八位>〕.md`。

文件名会拒绝路径穿越、反斜杠、NUL、点段、Windows 保留设备名和尾随空格/点。
所有标识符仍保存在 YAML frontmatter；正文不显示 Capture、Claim 或 Proposal ID。

只有 active catalog concept、稳定 `concept_id` 且被 resolved `ConceptMention` 引用的概念才
生成节点。为完成已经审查的关系邻域，可以额外生成关系目标。未解析、歧义、拒绝的提及，原始缩写、数字答案、普通词语、未审查 proposal 和聊天散文都不会成为节点。

学习记录链接到其已解析概念；概念记录只依据已审查 `ConceptRelation` 链接到其它概念，并反向链接引用它的学习记录。系统不会以同一 Capture 中的共现推断关系。

概念解释的优先级是：有引用的 `verified_reference` definition、有引用的
`source_backed` definition、中文 `scope_note`、`暂无已验证解释`。它不会使用未验证聊天、
未评估 claim、proposal、学习者答案或生成推断。

在 Obsidian Graph View 输入以下筛选条件可只查看医学概念图：

```text
path:"MedLearn/概念"
```

不修改 `.obsidian/graph.json`，不安装第三方图谱插件。不可变 Capture JSON 和旧 canonical
Markdown 继续保存于 R2，用于审计；它们不会出现在新版用户 manifest 中。

## 迁移

同步客户端仅删除以前由自身管理、并且当前字节摘要仍与旧 state 一致且已不在新 manifest 中的
`MedLearn/Data/Captures/**`、`MedLearn/Captures/**`、`MedLearn/Views/Captures/**` 文件。
任何本地修改、目录或 reparse point 都保留并报告冲突。新文件保持 create-only/conflict-safe
语义；成功后 state 精确记录新 manifest。重复同步不下载相同内容，也不改写相同字节。
