from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Sequence


@dataclass(frozen=True)
class ConditionRule:
    canonical_name: str
    condition_type: str
    aliases: Sequence[str]
    default_scope: str = "table"
    base_priority: int = 200
    requires_condition_cue: bool = False


CONDITION_RULES: List[ConditionRule] = [
    ConditionRule("试验温度", "environment", ("试验温度", "实验温度", "测试温度")),
    ConditionRule("环境温度", "environment", ("环境温度", "室温", "常温")),
    ConditionRule("老化温度", "operation", ("老化温度",)),
    ConditionRule("干燥温度", "process", ("干燥温度", "烘干温度")),
    ConditionRule("结晶温度", "process", ("结晶温度",)),
    ConditionRule("保温温度", "process", ("保温温度",)),
    ConditionRule("温度", "environment", ("温度",), requires_condition_cue=True),
    ConditionRule("压力", "environment", ("试验压力", "实验压力", "环境压力", "压力"), requires_condition_cue=True),
    ConditionRule("湿度", "environment", ("相对湿度", "湿度", "RH")),
    ConditionRule("真空度", "environment", ("真空度", "真空")),
    ConditionRule("气氛", "environment", ("气氛", "保护气氛", "环境气氛")),
    ConditionRule("介质", "environment", ("试验介质", "测试介质", "介质")),
    ConditionRule("溶剂", "process", ("溶剂", "反溶剂")),
    ConditionRule("加热速率", "operation", ("加热速率", "升温速率", "升温速度")),
    ConditionRule("加载速率", "operation", ("加载速率", "加载速度")),
    ConditionRule("应变率", "operation", ("应变率",)),
    ConditionRule("频率", "operation", ("测试频率", "试验频率", "频率")),
    ConditionRule("老化时间", "operation", ("老化时间", "老化时长")),
    ConditionRule("保温时间", "process", ("保温时间", "保温时长")),
    ConditionRule("干燥时间", "process", ("干燥时间", "烘干时间")),
    ConditionRule("试验时间", "operation", ("试验时间", "实验时间", "测试时间")),
    ConditionRule("循环次数", "operation", ("循环次数", "循环数")),
    ConditionRule("样品质量", "operation", ("样品质量", "试样质量", "试料质量")),
    ConditionRule("落锤质量", "operation", ("落锤质量", "锤重")),
    ConditionRule("落高", "operation", ("落高", "落锤高度")),
    ConditionRule("装药密度", "sample_state", ("装药密度",)),
    ConditionRule("试验密度", "sample_state", ("试验密度", "测试密度", "样品密度", "试样密度")),
    ConditionRule("粒径", "sample_state", ("粒径", "平均粒径"), requires_condition_cue=True),
    ConditionRule("粒度", "sample_state", ("粒度", "粒度范围"), requires_condition_cue=True),
    ConditionRule("晶型", "sample_state", ("晶型", "多晶型")),
    ConditionRule("纯度", "sample_state", ("纯度",), requires_condition_cue=True),
    ConditionRule("含水量", "sample_state", ("含水量", "水分"), requires_condition_cue=True),
    ConditionRule("浓度", "process", ("浓度", "溶液浓度"), requires_condition_cue=True),
    ConditionRule("固含量", "composition", ("固含量", "固体含量")),
    ConditionRule("质量比", "composition", ("质量比", "重量比", "配比")),
    ConditionRule("摩尔比", "composition", ("摩尔比", "物质的量比")),
    ConditionRule("当量比", "composition", ("当量比",)),
    ConditionRule("搅拌速度", "process", ("搅拌速度", "搅拌转速", "转速")),
    ConditionRule("加料顺序", "process", ("加料顺序", "添加顺序")),
]


RESULT_ONLY_TERMS = {
    "峰顶温度", "分解温度", "熔点", "沸点", "闪点", "燃点", "自燃点", "爆发点",
    "玻璃化温度", "热分解温度", "爆速", "爆压", "燃速", "比冲", "生成热", "生成焓",
}

CONDITION_CUES = (
    "条件", "在", "下", "时", "为", "保持", "控制", "设定", "采用", "经", "于", "分别",
)

FOLLOWING_NOTE_CUES = (
    "注", "说明", "试验条件", "实验条件", "测试条件", "其中", "备注", "环境条件",
)

METHOD_TOKENS = (
    "DTA", "DSC", "TG", "TGA", "DMA", "SEM", "TEM", "XRD", "NMR", "HPLC", "GC", "FTIR",
    "FT-IR", "Raman", "CARS", "微热量热", "差示扫描量热", "热重", "红外光谱", "拉曼光谱",
    "X射线衍射", "核磁共振", "毛细管法", "落锤法", "摩擦感度仪", "爆速仪",
)

INSTRUMENT_WORDS = ("仪", "仪器", "装置", "设备", "传感器", "量规", "色谱", "光谱仪", "显微镜")


def all_aliases() -> Iterable[tuple[ConditionRule, str]]:
    for rule in CONDITION_RULES:
        for alias in sorted(rule.aliases, key=len, reverse=True):
            yield rule, alias


__all__ = [
    "ConditionRule",
    "CONDITION_RULES",
    "RESULT_ONLY_TERMS",
    "CONDITION_CUES",
    "FOLLOWING_NOTE_CUES",
    "METHOD_TOKENS",
    "INSTRUMENT_WORDS",
    "all_aliases",
]
