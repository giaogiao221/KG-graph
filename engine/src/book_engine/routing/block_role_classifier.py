from __future__ import annotations

import re
from typing import List, Sequence, Tuple

from book_engine.document.block_segmenter import TextBlock
from book_engine.routing.heading_subject_resolver import classify_heading_subject

_GENERIC_HEADINGS = (
    "概述", "导言", "绪论", "引论", "原理", "机理", "定义", "分类", "发展史", "基本条件", "一般特征",
    "特点", "要求", "研究进展", "影响因素", "小结", "结论", "性能", "性质", "方法", "模型", "应用",
    "安全", "工艺", "测试", "分析", "表征", "试验", "实验", "设备", "结果", "讨论",
)
_MATERIAL_HINT_RE = re.compile(
    r"(?:HMX|RDX|TNT|PETN|CL-?20|NTO|ADN|FOX-?7|GAP|HTPB|BAMO|NIMMO|AP|AN|NG|NC|"
    r"硝酸|硝基|叠氮|高氯酸|聚[\u4e00-\u9fffA-Za-z0-9-]{1,30}|炸药|推进剂|药剂|弹性体|预聚物)",
    re.I,
)
_IDENTITY_RE = re.compile(r"(?:中文名称|英文名称|CAS(?:登记号)?|分子式|化学式|分子量|相对分子质量)\s*[：:]")
_FORMULATION_RE = re.compile(r"(?:配方|体系|PBX|CMDB|NEPE|推进剂|混合炸药|延期药|点火药|传爆药)", re.I)
_DEVICE_RE = re.compile(r"(?:火帽|底火|雷管|点火具|导爆索|导火索|延期元件|火工品|起爆器|点火器|装置)")
_PROCESS_RE = re.compile(r"(?:制备|合成|加工|工艺|结晶|粉碎|混合|包覆|微胶囊|干燥|固化|聚合|装配|装压药)")
_EXPERIMENT_RE = re.compile(r"(?:试验|实验|测试|测定|检验|表征|分析方法|仪器)")
_MODEL_RE = re.compile(r"(?:模型|仿真|模拟|计算方法|专家系统|算法|有限元|数据库|知识库)")
_SAFETY_RE = re.compile(r"(?:安全|毒性|防护|储存|运输|危险|消防|泄漏|废弃|相容性|安定性)")
_PROPERTY_RE = re.compile(r"(?:物理性质|化学性质|理化性质|性能|密度|熔点|爆速|燃速|比冲|感度|流变|力学)")
_COMPARISON_RE = re.compile(r"(?:比较|对比|差异|影响|关系|变化规律)")
_THEORY_RE = re.compile(r"(?:原理|机理|理论|定义|概念|分类|基本条件|发展史|一般特征|导论|概述)")


def _material_heading_candidate(title: str, text: str) -> bool:
    candidate = classify_heading_subject(title)
    if candidate.is_entity:
        return True
    # Identity fields are independent local evidence.  They may establish a
    # material profile even when the heading itself is only a topic such as
    # “基本性质”.
    return bool(_IDENTITY_RE.search(text or ""))


def classify_block(block: TextBlock) -> TextBlock:
    title = block.heading_title or ""
    joined = f"{title}\n{block.text[:1200]}"
    reasons: List[str] = []

    if _DEVICE_RE.search(title):
        role, confidence = "device_system", 0.88
        reasons.append("device_heading")
    elif _FORMULATION_RE.search(title) and ("配方" in title or "体系" in title or "推进剂" in title or "药" in title):
        role, confidence = "formulation", 0.84
        reasons.append("formulation_heading")
    elif _material_heading_candidate(title, block.text):
        role, confidence = "material_profile", 0.82 if _IDENTITY_RE.search(block.text) else 0.74
        reasons.append("material_heading_candidate")
    elif _PROCESS_RE.search(title):
        role, confidence = "process", 0.84
        reasons.append("process_heading")
    elif _EXPERIMENT_RE.search(title):
        role, confidence = "experiment", 0.82
        reasons.append("experiment_heading")
    elif _MODEL_RE.search(title):
        role, confidence = "method_model", 0.85
        reasons.append("model_heading")
    elif _SAFETY_RE.search(title):
        role, confidence = "application_safety", 0.82
        reasons.append("safety_heading")
    elif _COMPARISON_RE.search(title):
        role, confidence = "comparison", 0.76
        reasons.append("comparison_heading")
    elif _PROPERTY_RE.search(title):
        role, confidence = "property", 0.80
        reasons.append("property_heading")
    elif _THEORY_RE.search(title):
        role, confidence = "theory", 0.84
        reasons.append("theory_heading")
    elif _IDENTITY_RE.search(joined):
        role, confidence = "material_profile", 0.78
        reasons.append("identity_fields_in_block")
    else:
        role, confidence = "unknown", 0.40
        reasons.append("no_strong_role_signal")

    block.role = role
    block.role_confidence = confidence
    block.role_reasons = reasons
    return block


def classify_blocks(blocks: Sequence[TextBlock]) -> List[TextBlock]:
    return [classify_block(block) for block in blocks]


__all__ = ["classify_block", "classify_blocks"]
