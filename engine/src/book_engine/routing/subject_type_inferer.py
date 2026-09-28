from __future__ import annotations

import re
from typing import Tuple, List

_DEVICE_TOKENS = (
    "火帽", "底火", "雷管", "点火具", "起爆器", "导火索", "导爆索", "导爆管", "延期元件",
    "传爆元件", "爆炸开关", "驱动器", "燃气发生器", "半导体桥", "MEMS火工",
)
_FORMULATION_TOKENS = (
    "配方", "体系", "复合推进剂", "固体推进剂", "发射药", "PBX", "CMDB", "NEPE", "推进剂药浆",
    "延期药", "点火药", "击发药", "针刺药", "传爆药", "烟火药", "混合炸药", "浇铸炸药",
    "熔铸炸药", "塑料粘结炸药", "胶状炸药", "乳化炸药",
)
_FAMILY_TOKENS = (
    "含能基团", "含能聚合物", "含能预聚物", "含能单体", "含能热塑性弹性体", "氧化剂", "粘结剂",
    "黏结剂", "增塑剂", "键合剂", "固化剂", "燃速调节剂", "催化剂", "安定剂", "阻燃剂",
)
_SAMPLE_TOKENS = ("样品", "试样", "批次", "试件")
_POLYMER_TOKENS = ("聚合物", "预聚物", "弹性体", "共聚物", "均聚物", "树脂", "橡胶")


def infer_subject_type(name: str) -> Tuple[str, float, List[str]]:
    value = name or ""
    reasons: List[str] = []
    upper = value.upper()

    if any(token in value for token in _DEVICE_TOKENS):
        reasons.append("device_token")
        return "火工品器件", 0.94, reasons
    if any(token in value for token in _FORMULATION_TOKENS) or re.match(r"^(?:PBX|LX|JOB|B|NQ|NEPE|CMDB)[A-Z0-9-]+$", upper):
        reasons.append("formulation_token")
        return "配方/材料体系", 0.88, reasons
    if any(token in value for token in _SAMPLE_TOKENS):
        reasons.append("sample_token")
        return "样品", 0.78, reasons
    if value in _FAMILY_TOKENS or any(value.endswith(token) for token in _FAMILY_TOKENS):
        reasons.append("material_family_token")
        return "材料类别", 0.82, reasons
    if any(token in value for token in _POLYMER_TOKENS):
        reasons.append("polymer_token")
        return "高聚物", 0.84, reasons
    reasons.append("generic_material_default")
    return "材料", 0.62, reasons


__all__ = ["infer_subject_type"]
