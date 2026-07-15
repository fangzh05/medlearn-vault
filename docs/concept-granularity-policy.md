# Atomic medical-entity graph policy

A concept answers “what medical entity is this?”  A relation answers how two
entities relate; claims and note sections retain diagnostic boundaries,
mechanisms, examination statements, treatments, and examination teaching.

New candidates must be independently definable, reusable, stably named, and
not better represented as a relation, claim, note section, assessment item, or
learner evidence. Compound/contextual labels are rejected before promotion.

## Controlled relations

| Relation | Direction | Inverse presentation |
| --- | --- | --- |
| `subtype_of` | subtype → broader entity | has subtype |
| `part_of` | component → whole | has part |
| `associated_with`, `differential_with`, `contraindicated_with` | symmetric | same label |
| `may_cause` | cause → outcome | may be caused by |
| `may_be_complicated_by` | disease → complication | complicates |
| `has_manifestation` | disease → manifestation | manifestation of |
| `investigated_by`, `diagnosed_by`, `treated_by`, `measured_by` | entity → investigation/treatment/measure | investigates/diagnoses/treats/measures |
| `indicated_for` | drug/procedure → condition | has indicated treatment |
| `affects_anatomy` | entity → anatomy | affected by |
| `biomarker_for` | biomarker → condition | has biomarker |

Every persisted edge uses this vocabulary and at least one reviewed supporting
claim. Conversation co-occurrence never creates an edge.

## Published-catalog audit

The audit classifies the current sample catalog without mutating historical
Captures. Old IDs remain audit anchors; future extraction redirects to the
atomic targets below.

| Existing concept | Classification | Deterministic replacement / presentation |
| --- | --- | --- |
| 抗磷脂综合征抗体谱与诊断边界 | SPLIT_INTO_ATOMIC_CONCEPTS | APS, relevant antibodies; diagnostic claim |
| D-二聚体形成机制 | DEMOTE_TO_NOTE_SECTION | D-二聚体 mechanism section |
| HLA-B27诊断边界 | DEMOTE_TO_CLAIM | HLA-B27 plus disease entities |
| 类风湿关节炎典型分布、分类评分、甲氨蝶呤与Felty综合征 | SPLIT_INTO_ATOMIC_CONCEPTS | 类风湿关节炎, 甲氨蝶呤, Felty综合征; claims/relations |
| 甲氨蝶呤治疗RA | DEMOTE_TO_CLAIM | 甲氨蝶呤 `indicated_for` 类风湿关节炎 when supported |
| 苯溴马隆与尿酸性肾结石 | SPLIT_INTO_ATOMIC_CONCEPTS | 苯溴马隆, 尿酸性肾结石; contraindication claim |
| SLE活动与狼疮性肾炎 | SPLIT_INTO_ATOMIC_CONCEPTS | SLE, 狼疮性肾炎; supported complication claim |
| 诊断金标准 / DIC诊断 / DIC治疗 | DEMOTE_TO_NOTE_SECTION | disease note diagnostic/treatment sections |
| 8分 | DEMOTE_TO_CLAIM | assessment result only |

The presentation projection excludes deprecated and `split_pending` concepts,
Capture JSON, renderer projections, `.base` files, and `MedLearn/学习记录`.
Use `path:"MedLearn/概念"` in Obsidian Graph View to see only atomic concept
notes. Disease notes aggregate 定义、病因与危险因素、发病机制、病理、临床表现、检查、诊断、
鉴别、并发症、治疗、相关药物、考试要点、学习记录; drug notes aggregate 药物分类、作用机制、
适应证、用法信息（已核验时）、不良反应、禁忌证、相互作用、相关疾病、考试要点、学习记录.
