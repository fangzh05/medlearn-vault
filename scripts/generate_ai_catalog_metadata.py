"""Generate explicitly labelled AI-assisted catalog metadata for review.

This is an operator aid, not an automatic medical publishing path.  Every
generated scope note records that GPT wrote/organized the entry and names the
source priority.  The resulting metadata still has to pass the normal catalog
patch and CI checks.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

TYPE_HINTS = {
    "ADAMTS13": "biomarker",
    "HbA2": "biomarker",
    "HbH": "biomarker",
    "铁蛋白": "biomarker",
    "MCV": "biomarker",
    "RBC计数": "investigation",
    "TIBC": "investigation",
    "电泳": "investigation",
    "Rous试验": "investigation",
    "血红蛋白电泳": "investigation",
    "血浆置换": "procedure",
    "透析": "procedure",
    "依库珠单抗": "drug",
    "C5抑制剂": "drug_class",
    "支持治疗": "guideline",
    "微血管内皮": "anatomy",
    "肾小管": "anatomy",
    "肾脏微循环": "physiology",
    "补体": "physiology",
    "补体调节系统": "physiology",
    "内皮损伤": "pathology",
    "机械剪切": "mechanism",
    "血管内溶血": "pathology",
    "慢性血管内溶血": "pathology",
    "直接补体溶血": "pathology",
    "微血栓": "pathology",
    "血小板微血栓": "pathology",
    "碎片细胞": "sign",
    "地中海贫血": "disease",
    "α地中海贫血": "disease",
    "β地中海贫血": "disease",
    "缺铁性贫血": "disease",
    "血栓性血小板减少性紫癜": "disease",
    "TTP": "disease",
    "溶血尿毒综合征": "syndrome",
    "HUS": "syndrome",
    "补体介导性溶血尿毒综合征": "syndrome",
    "补体介导性HUS": "syndrome",
    "典型HUS": "syndrome",
    "aHUS": "syndrome",
    "PNH": "disease",
    "地中海贫血携带状态": "other",
    "α链": "other",
    "β4": "other",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("catalog_update", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--source", required=True)
    args = parser.parse_args()
    update = json.loads(args.catalog_update.read_text(encoding="utf-8"))
    rows = []
    for item in update["incomplete_concept_metadata"]:
        text = item["surface_text"]
        concept_type = TYPE_HINTS.get(text, "other")
        rows.append(
            {
                "resolution_id": item["resolution_id"],
                "canonical_name": text,
                "concept_type": concept_type,
                "scope_note": (
                    "GPT编写/整理的学习目录候选；不是个体化医疗建议。"
                    f" 来源优先级：项目内人卫教材（若存在）→权威指南/专业机构；"
                    f"参考：{args.source}。"
                ),
            }
        )
    args.output.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
