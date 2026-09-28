from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Sequence, Tuple

from book_engine.document.block_segmenter import TextBlock
from book_engine.routing.text_subject_anchor_resolver import TextSubjectAnchor, is_plausible_subject_name
from book_engine.text.text_fact_extractor import TextFactRecord
from book_engine.text.process_step_llm_adjudicator import ProcessStepLLMAdjudicator

_ACTION_ALIASES: Dict[str, Tuple[str, ...]] = {
    "称量": ("称量", "称取", "计量"),
    "精选": ("精选", "挑选"),
    "脱皮": ("脱皮", "去皮"),
    "压扁": ("压扁",),
    "提取": ("浸提", "提取"),
    "脱胶": ("脱胶",),
    "脱酸": ("脱酸",),
    "脱色": ("脱色",),
    "脱臭": ("脱臭",),
    "漂白": ("漂白",),
    "除杂": ("除杂质", "除杂"),
    "冷藏": ("冷藏",),
    "盐析": ("盐析",),
    "捏合": ("捏合",),
    "塑化": ("塑化",),
    "造粒": ("造粒",),
    "脱泡": ("真空脱泡", "脱泡"),
    "喷雾": ("喷雾",),
    "挤出": ("挤出",),
    "分散": ("分散",),
    "乳化": ("乳化",),
    "加入": ("缓慢滴加", "滴加", "缓慢加入", "加入", "添加", "投入", "倒入", "注入", "放入"),
    "混合": ("预混", "混合", "混匀", "拌和"),
    "搅拌": ("搅拌", "搅和"),
    "加热": ("升温", "加热", "预热"),
    "保温": ("保温", "恒温"),
    "冷却": ("降温", "冷却"),
    "静置": ("静置", "陈化", "老化"),
    "溶解": ("溶解", "溶化", "溶于"),
    "分离": ("分离", "分出", "分开"),
    "交联": ("交联",),
    "浸渍": ("抗湿处理", "浸渍", "浸入"),
    "浸出": ("浸出", "萃取"),
    "焙烧": ("氧化焙烧", "煅烧", "焙烧"),
    "中和": ("中和",),
    "离心": ("离心",),
    "脱水": ("脱水",),
    "分解": ("热分解", "分解"),
    "酯化": ("酯化",),
    "环化": ("环化",),
    "缩合": ("缩合",),
    "还原": ("还原",),
    "氧化": ("氧化",),
    "水解": ("水解",),
    "聚合": ("共聚", "聚合"),
    "蒸馏": ("精馏", "蒸馏"),
    "微胶囊化": ("微胶囊化", "包覆成微胶囊"),
    "除去": ("除去", "脱除", "去除"),
    "反应": ("继续反应", "反应"),
    "过滤": ("抽滤", "过滤", "滤出"),
    "洗涤": ("洗涤", "水洗"),
    "干燥": ("真空干燥", "烘干", "干燥"),
    "蒸发": ("蒸发", "浓缩", "蒸出"),
    "结晶": ("重结晶", "结晶"),
    "粉碎": ("粉碎", "破碎", "研磨"),
    "筛分": ("过筛", "筛分"),
    "压制": ("压片", "压制", "成型"),
    "浇注": ("浇铸", "浇注"),
    "固化": ("固化", "交联"),
    "脱模": ("脱模",),
    "切割": ("切割", "裁切"),
    "包覆": ("包覆", "包衣"),
    "装填": ("装填", "填装", "装药"),
    "装配": ("装好", "结合", "装配", "安装", "连接", "固定"),
    "校准": ("校准", "标定", "调零"),
    "测试": ("测试", "试验", "测量", "测定"),
    "记录": ("记录", "采集"),
}
_ACTION_TERMS = sorted({term for terms in _ACTION_ALIASES.values() for term in terms}, key=len, reverse=True)
_ACTION_RE = re.compile("|".join(re.escape(term) for term in _ACTION_TERMS))
_PROCESS_HEADING_RE = re.compile(r"(?:制备|合成|加工|生产|装配|操作|试验|测试|处理|工艺|流程|方法)")
_EXPLICIT_STEP_RE = re.compile(
    r"(?:^|[\n。；;：:])\s*(?P<label>(?:步骤\s*[0-9一二三四五六七八九十]+|第\s*[一二三四五六七八九十0-9]+\s*步|[①②③④⑤⑥⑦⑧⑨⑩]|[（(]?[0-9]{1,2}[）)、．]|[0-9]{1,2}[.](?!\d)\s+))\s*[-—:：、.．)]?"
)
_STRONG_PROCESS_HEADING_RE = re.compile(r"(?:操作步骤|制备步骤|合成步骤|工艺流程|操作流程|操作程序|试验步骤|测试步骤|装配步骤|制备方法|合成方法|操作方法|处理方法|装配方法|生产方法|加工方法)")
_TOC_LINE_RE = re.compile(r"^\s*\d+(?:[.．]\d+){1,5}\s*.+?(?:…+|\.{2,}|\s)\s*\d+\s*$")
_ACTION_NOUN_SUFFIX_RE = re.compile(r"^(?:基本原理|原理|设备|工艺|缺陷|过程|产品|质量|模型|模拟|温度|压力|密度|比例|率|剂|方法|法|性能|特性|现象|行为|机理|理论|炉|器|机|釜|罐|槽|的)")
_CONNECTOR_RE = re.compile(r"(?:，|,|；|;|。|\n)\s*(?=(?:然后|再|随后|接着|其次|最后|之后|待|并(?:再|随后)))")
_LEADING_CONNECTOR_RE = re.compile(r"^(?:首先|先|然后|再|随后|接着|其次|最后|之后|待|并(?:再|随后))[，,、\s]*")
_SAFE_ADJACENT_ACTION_PAIRS = {
    ("冷却", "洗涤"), ("冷却", "过滤"), ("冷却", "干燥"),
    ("过滤", "洗涤"), ("过滤", "干燥"), ("洗涤", "干燥"),
    ("离心", "脱水"), ("冷藏", "过滤"),
}
_PROCESS_PREFIX_CONDITION_RE = re.compile(
    r"^(?:以|在|于|用|采用|通过|按照|按)[^。；;]{1,120}(?:"
    r"催化剂|溶剂|介质|原料|调节剂|干燥剂|气氛|条件|温度|压力|质量比|摩尔比|配比|比例|下|中)$",
    re.I,
)
_CONDITION_PATTERNS = (
    ("温度", re.compile(r"(?:在|于|升至|降至|冷却至|控制在|保持在)?\s*(?:温度)?\s*(?:为|至|到|=)?\s*[-+]?\d+(?:\.\d+)?(?:\s*[~～—–至到]\s*[-+]?\d+(?:\.\d+)?)?\s*(?:℃|°C|K)")),
    ("时间", re.compile(r"(?:持续|保持|搅拌|反应|保温|静置|干燥)?\s*\d+(?:\.\d+)?(?:\s*[~～—–至到]\s*\d+(?:\.\d+)?)?\s*(?:s|min|h|d|秒|分钟|小时|天)")),
    ("压力", re.compile(r"(?:压力)?\s*(?:为|至|到|=)?\s*[-+]?\d+(?:\.\d+)?(?:\s*[~～—–至到]\s*[-+]?\d+(?:\.\d+)?)?\s*(?:Pa|kPa|MPa|GPa)")),
    ("转速", re.compile(r"(?:转速)?\s*(?:为|至|到|=)?\s*\d+(?:\.\d+)?\s*(?:rpm|r/min|转/分)")),
    ("气氛", re.compile(r"(?:在)?\s*(?:氮气|氩气|惰性气体|真空|空气)\s*(?:气氛|条件)?(?:下)?")),
    ("催化剂", re.compile(r"以\s*[^，,。；;]{1,50}?\s*为催化剂")),
    ("溶剂", re.compile(r"以\s*[^，,。；;]{1,50}?\s*为溶剂")),
    ("原料", re.compile(r"以\s*[^，,。；;]{1,50}?\s*为原料")),
    ("配比", re.compile(r"(?:按照|按)\s*[^，,。；;]{1,50}?(?:比|比例)")),
    ("pH", re.compile(r"(?:pH|\\mathrm\s*\{\s*p\s*H\s*\})\s*(?:值)?\s*(?:为|至|到|=)?\s*[-+]?\d+(?:\.\d+)?", re.I)),
    ("方法", re.compile(r"(?:采用|通过)?\s*[^，,。；;]{1,30}?(?:法|方法)(?=除去|进行|实现|制备|处理|干燥|包覆|测定|测试|$)")),
    ("介质", re.compile(r"(?:在|于)\s*[^，,。；;]{0,30}?(?:溶液|介质|溶剂|水相|油相)中")),
)
_RESULT_RE = re.compile(r"(?:得到|获得|制得|制备出|形成|生成|产得|收集|得到产物|得到成品)(?P<result>[^。；;]{0,100})")
_CN = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_CIRCLED = {"①": 1, "②": 2, "③": 3, "④": 4, "⑤": 5, "⑥": 6, "⑦": 7, "⑧": 8, "⑨": 9, "⑩": 10}
_NON_STEP_PHRASE_RE = re.compile(r"^(?:反应式|方程式|结构式|示意图|测试结果|试验结果|性能结果|结果表明|由此可见|如下)")
_NON_MATERIAL_PROCESS_HEADING_RE = re.compile(r"(?:系统工作流程|总体工作流程|推理流程|算法流程|软件流程|计算流程|专家系统工作|程序流程)")


@dataclass
class ProcessStepAudit:
    block_id: str
    process_id: str
    step_id: str
    step_index: int | None
    action: str
    object_text: str
    condition_text: str
    result_text: str
    ordering_source: str
    status: str
    reasons: List[str] = field(default_factory=list)
    evidence: str = ""
    step_text: str = ""


def _canonical_action(text: str, *, near_start: bool = False) -> Tuple[str, str]:
    value = text or ""
    candidates: List[Tuple[int, str, str]] = []
    for hit in _ACTION_RE.finditer(value):
        raw = hit.group(0)
        if near_start and hit.start() > 120:
            continue
        tail = value[hit.end(): hit.end() + 10]
        before = value[: hit.start()]
        if _ACTION_NOUN_SUFFIX_RE.match(tail):
            continue
        if raw in {"混合", "聚合"} and re.match(r"(?:物|时间|性能|程度|效率|效果|比例|状态|质量|均匀性)", tail):
            # Material/property nouns such as “混合物、聚合物、混合时间”
            # are not operation verbs.
            continue
        if raw in {"塑化", "乳化"} and re.match(r"(?:物|棉|剂|产品|产物|程度|反应|机理|性能)", tail):
            continue
        if raw in {"提取", "分散"} and re.match(r"(?:物|率|性|性能|程度|效果)", tail):
            continue
        if raw == "提取" and re.search(r"(?:是|为)[^，,。；;]{0,30}提取[^，,。；;]{0,40}(?:副产品|产物|产品|方法|工艺)", value):
            continue
        if raw in {"脱胶", "脱酸", "脱色", "脱臭", "漂白", "除杂质", "除杂"} and re.match(r"(?:所得|所获|所形成|产品|产物|结果)", tail):
            continue
        if raw == "氧化":
            prefix = value[max(0, hit.start() - 2): hit.start()]
            if prefix.endswith(("氢", "过")) or re.match(r"(?:钙|镁|铝|钠|钾|锌|铁|铜|铅|银|钼|锰|钛|铬|硅|硼|钴|镍|钨|锡|锑|铋|物|剂)", tail):
                continue
        if raw == "反应":
            # “反应后/反应结束后” is a temporal bridge into the next
            # operation, not a standalone process step. Skip it so a later
            # concrete action in the same exact source span can be selected.
            if re.match(r"(?:后|结束后|完成后|完毕后|结束|完成|完毕|产生|难以|要求|压力|温度|速率|热|机理|过程|式|器)", tail):
                continue
            operation_context = bool(
                hit.start() <= 2
                or re.search(r"(?:将|把|使|令|由|采用|通过|直接|继续|进行|发生|在[^，,。；;]{0,40}下)[^，,。；;]{0,100}$", before)
            )
            if not operation_context:
                continue
        canonical = raw
        for name, aliases in _ACTION_ALIASES.items():
            if raw in aliases:
                canonical = name
                break
        candidates.append((hit.start(), canonical, raw))
    if not candidates:
        return "", ""

    # “在搅拌下加入...” uses stirring as a local operating condition; the
    # material addition is the step action.
    if re.search(r"在搅拌下", value):
        for _, canonical, raw in candidates:
            if canonical == "加入":
                return canonical, raw

    generic = {"加入", "装配"}
    first = candidates[0]
    if first[1] in generic:
        for candidate in candidates[1:]:
            if candidate[1] not in generic:
                return candidate[1], candidate[2]
    return first[1], first[2]


def _looks_toc_block(text: str) -> bool:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    toc_hits = sum(bool(_TOC_LINE_RE.match(line)) for line in lines)
    section_hits = sum(bool(re.match(r"^\d+(?:[.．]\d+){1,5}", line)) for line in lines)
    page_hits = sum(bool(re.search(r"(?:…+|\.{2,}|\s)\s*\d+\s*$", line)) for line in lines)
    return (toc_hits >= 2) or (len(lines) >= 5 and section_hits >= 3 and page_hits >= 3)


def _index_from_label(label: str, fallback: int) -> int:
    digits = re.search(r"\d+", label or "")
    if digits:
        return int(digits.group(0))
    for token, value in _CIRCLED.items():
        if token in (label or ""):
            return value
    compact = re.sub(r"[^一二三四五六七八九十]", "", label or "")
    if compact in _CN:
        return _CN[compact]
    return fallback


def _explicit_steps(text: str, heading: str) -> List[Tuple[int, str, str, str]]:
    if _looks_toc_block(text):
        return []
    matches = list(_EXPLICIT_STEP_RE.finditer(text or ""))
    if not matches:
        return []
    strong_heading = bool(_STRONG_PROCESS_HEADING_RE.search(heading or ""))
    explicit_worded = any("步骤" in (match.group("label") or "") or "步" in (match.group("label") or "") for match in matches)
    # Bare numbered lists are frequently alternative methods, examples,
    # properties or applications. They are a step sequence only under headings
    # that explicitly say steps/flow/procedure. A lone item under “制备方法” is
    # handled later by action-span segmentation rather than becoming one giant step.
    sequence_heading = bool(re.search(r"(?:步骤|流程|程序)", heading or ""))
    if not explicit_worded and not sequence_heading:
        return []
    result: List[Tuple[int, str, str, str]] = []
    for pos, match in enumerate(matches):
        start = match.end()
        end = matches[pos + 1].start() if pos + 1 < len(matches) else len(text)
        clause = text[start:end].strip(" \n。；;：:")
        action, _ = _canonical_action(clause, near_start=True)
        if clause and action:
            result.append((_index_from_label(match.group("label"), pos + 1), match.group("label"), clause, "explicit_step_marker"))
    if len(matches) >= 2 and len(result) < 2 and not strong_heading:
        return []
    return result


def _truncate_process_tail(text: str) -> str:
    # Connector-defined steps should not absorb later explanatory, performance,
    # figure or discussion sentences. Keep the first source sentence only.
    value = (text or "").strip()
    for separator in ("。", "；", ";"):
        if separator in value:
            value = value.split(separator, 1)[0].strip()
    return value


def _split_compound_action_clause(clause: str) -> List[str]:
    value = (clause or "").strip()
    if not value:
        return []
    valid: List[Tuple[re.Match[str], str]] = []
    for hit in _ACTION_RE.finditer(value):
        suffix = value[hit.start():]
        canonical, raw = _canonical_action(suffix, near_start=True)
        if not canonical or raw != hit.group(0):
            continue
        if canonical == "搅拌" and re.search(r"在搅拌下", value):
            continue
        valid.append((hit, canonical))
    if len(valid) < 2:
        return [value]
    for (left_hit, left_action), (right_hit, right_action) in zip(valid, valid[1:]):
        bridge = value[left_hit.end(): right_hit.start()]
        explicit_sequence = bool(re.search(r"(?:后|并|并再|再|随后|然后|接着)\s*$", bridge))
        safe_adjacent = not bridge.strip() and (left_action, right_action) in _SAFE_ADJACENT_ACTION_PAIRS
        if not (explicit_sequence or safe_adjacent):
            continue
        left = value[: right_hit.start()].strip(" ，,、")
        right = value[right_hit.start():].strip(" ，,、")
        output: List[str] = []
        if left:
            output.append(left)
        if right:
            output.extend(_split_compound_action_clause(right))
        return output or [value]
    return [value]


def _split_serial_action_segments(text: str) -> List[str]:
    value = _truncate_process_tail(text)
    if not value:
        return []
    matches = list(re.finditer(r"[^，,、]+", value))
    if len(matches) <= 1:
        action, _ = _canonical_action(value, near_start=True)
        return _split_compound_action_clause(value) if action else []

    spans: List[Tuple[int, int]] = []
    pending_start: int | None = None
    for match in matches:
        segment = match.group(0).strip()
        if not segment:
            continue
        segment_start = match.start() + (len(match.group(0)) - len(match.group(0).lstrip()))
        segment_end = match.end() - (len(match.group(0)) - len(match.group(0).rstrip()))
        if re.match(r"^(?:得到|获得|制得|制备出|形成|生成|产得)", segment) and spans:
            previous_start, _ = spans[-1]
            spans[-1] = (previous_start, segment_end)
            continue
        action, _ = _canonical_action(segment, near_start=True)
        if action:
            start_at = pending_start if pending_start is not None else segment_start
            spans.append((start_at, segment_end))
            pending_start = None
            continue
        condition_probe = re.sub(
            r"^(?:\$?\\textcircled\{\d+\}\$?|[①②③④⑤⑥⑦⑧⑨⑩]|[（(]?\d+[）)、.])\s*",
            "",
            segment,
        ).strip()
        temporal_prefix = len(condition_probe) <= 40 and bool(re.search(r"(?:反应后|结束后|完成后|完毕后|冷却后|过滤后|洗涤后|干燥后|待.+后|随后|然后|接着|再)$", condition_probe))
        condition_prefix = bool(_PROCESS_PREFIX_CONDITION_RE.search(condition_probe))
        if pending_start is None and (temporal_prefix or condition_prefix):
            pending_start = segment_start
    if pending_start is not None and spans:
        tail = value[pending_start:].strip()
        if re.search(r"(?:得到|获得|制得|制备出|形成|生成|产得|从而|以便)", tail):
            previous_start, _ = spans[-1]
            spans[-1] = (previous_start, len(value.rstrip()))
    output: List[str] = []
    for span_start, span_end in spans:
        clause = value[span_start:span_end].strip(" ，,、")
        if clause:
            output.extend(_split_compound_action_clause(clause))
    return output


def _connector_steps(text: str) -> List[Tuple[int, str, str, str]]:
    first_positions = [pos for token in ("首先", "先") if (pos := text.find(token)) >= 0]
    if not first_positions or not any(token in text for token in ("然后", "再", "随后", "接着", "最后")):
        return []
    body = text[min(first_positions):]
    parts = [_LEADING_CONNECTOR_RE.sub("", item.strip(" \n。；;")) for item in _CONNECTOR_RE.split(body)]
    valid: List[str] = []
    for item in parts:
        valid.extend(_split_serial_action_segments(item))
    if len(valid) < 2:
        return []
    return [(index, f"顺序{index}", clause, "ordered_connector_sequence") for index, clause in enumerate(valid, start=1)]


def _llm_step_candidates(text: str) -> List[Dict[str, object]]:
    if _looks_toc_block(text):
        return []
    candidates: List[Dict[str, object]] = []
    seen: set[Tuple[int, int]] = set()
    source_index = 0
    for sentence_match in re.finditer(r"[^。；;\n]+(?:[。；;]|$)", text or ""):
        sentence = sentence_match.group(0).strip(" \n。；;")
        if not sentence:
            continue
        segments = _split_serial_action_segments(sentence)
        if not segments:
            action, _ = _canonical_action(sentence, near_start=True)
            segments = [sentence] if action else []
        search_from = sentence_match.start()
        for segment in segments:
            cleaned_segment = re.sub(r"^(?:[（(]?\d+[）)、.]|\$?\\textcircled\{\d+\}\$?)\s*", "", segment).strip()
            if _NON_STEP_PHRASE_RE.match(cleaned_segment):
                continue
            local = (text or "").find(segment, search_from, sentence_match.end())
            if local < 0:
                local = (text or "").find(segment)
            if local < 0:
                continue
            end = local + len(segment)
            key = (local, end)
            if key in seen:
                continue
            action, raw_action = _canonical_action(segment, near_start=True)
            if not action:
                continue
            seen.add(key)
            source_index += 1
            candidates.append(
                {
                    "candidate_id": f"PSC{source_index:03d}",
                    "source_index": source_index,
                    "start": local,
                    "end": end,
                    "text": segment,
                    "action": action,
                    "raw_action": raw_action,
                    "object_text": _object_text(segment, raw_action),
                    "condition_text": _condition_text(segment),
                    "result_text": _result_text(segment),
                }
            )
            search_from = end
    return candidates


def _deterministic_action_sequence(text: str, heading: str) -> List[Tuple[int, str, str, str]]:
    if not _STRONG_PROCESS_HEADING_RE.search(heading or "") or _looks_toc_block(text):
        return []
    source_text = text or ""
    detail_match = re.search(r"(?:主要制备过程|具体制备过程|具体操作步骤|操作步骤)\s*[：:]", source_text)
    if detail_match:
        source_text = source_text[detail_match.end():].strip()
    bare_markers = list(_EXPLICIT_STEP_RE.finditer(source_text))
    if len(bare_markers) > 1 and not re.search(r"(?:步骤|流程|程序)", heading or ""):
        # Multiple numbered items under a “method” heading are usually
        # alternative routes, not one ordered chain.
        return []
    candidates = _llm_step_candidates(source_text)
    if len(candidates) < 2:
        return []
    return [
        (index, f"动作{index}", str(item["text"]), "deterministic_action_sequence")
        for index, item in enumerate(candidates, start=1)
    ]


def _llm_steps(
    block: TextBlock,
    anchor: TextSubjectAnchor,
    adjudicator: ProcessStepLLMAdjudicator,
) -> List[Tuple[int, str, str, str]]:
    candidates = _llm_step_candidates(block.text)
    if len(candidates) < 2 or not adjudicator.available:
        return []
    selection = adjudicator.adjudicate(
        block_id=block.block_id,
        heading_path=block.heading_path,
        process_subject=anchor.subject,
        source_text=block.text,
        candidates=candidates,
    )
    if selection is None or selection.status != "selected" or selection.confidence < 0.78:
        return []
    by_id = {str(item["candidate_id"]): item for item in candidates}
    output: List[Tuple[int, str, str, str]] = []
    for index, candidate_id in enumerate(selection.selected_candidate_ids, start=1):
        item = by_id.get(candidate_id)
        if not item:
            continue
        output.append((index, f"LLM候选{index}", str(item["text"]), "closed_set_process_llm"))
    return output if len(output) >= 2 else []


def _condition_text(clause: str) -> str:
    values: List[str] = []
    if re.search(r"在搅拌下", clause or ""):
        values.append("操作状态=在搅拌下")
    medium_match = re.search(r"(?:溶于|分散于|悬浮于)\s*([^，,。；;]{1,40}?中)", clause or "")
    if medium_match:
        values.append(f"介质={medium_match.group(1).strip()}")
    for name, pattern in _CONDITION_PATTERNS:
        for match in pattern.finditer(clause or ""):
            raw = match.group(0).strip()
            if raw:
                values.append(f"{name}={raw}")
    return "；".join(dict.fromkeys(values))


def _result_text(clause: str) -> str:
    match = _RESULT_RE.search(clause or "")
    return (match.group(0).strip() if match else "")


def _object_text(clause: str, raw_action: str) -> str:
    before, sep, after = (clause or "").partition(raw_action)
    if not sep:
        return ""
    match = re.search(r"(?:把|将)([^，,。；;]{1,100})$", before)
    if match:
        return re.sub(r"(?:经|进行)$", "", match.group(1).strip()).strip()
    canonical, _ = _canonical_action(clause, near_start=True)
    if canonical == "提取":
        source_match = re.search(r"以\s*([^，,。；;]{1,50}?)\s*为原料", clause or "")
        if source_match:
            return source_match.group(1).strip()
    reaction_actions = {"交联", "反应", "混合", "酯化", "环化", "缩合", "还原", "氧化", "水解", "聚合"}
    if canonical in reaction_actions and before.strip():
        candidate = before.strip()
        candidate = re.sub(r"^(?:\$?\\textcircled\{\d+\}\$?|[①②③④⑤⑥⑦⑧⑨⑩]|[（(]?\d+[）)、.])\s*", "", candidate)
        by_match = re.search(r"(?:由|使)([^，,。；;]{1,120}?)(?:进行)?$", candidate)
        if by_match:
            candidate = by_match.group(1).strip()
        else:
            candidate = re.sub(r"^(?:首先|先|然后|再|随后|接着|其次|最后)?\s*(?:通过|采用|用|以|由)?", "", candidate)
        candidate = re.sub(r"(?:在|于)\s*[^，,。；;]{1,60}(?:中|下)(?:进行)?$", "", candidate).strip()
        candidate = re.sub(r"(?:进行)$", "", candidate).strip()
        if candidate:
            return candidate[-120:]
    if canonical == "溶解" and raw_action == "溶于":
        return ""
    if canonical in {"加热", "冷却", "保温", "冷藏", "干燥", "焙烧", "分解"} and re.match(r"^\s*(?:至|到|于|在)?\s*(?:\$|[-+]?\d)", after):
        return ""
    if re.match(r"^\s*(?:等(?:前处理|工艺|处理)|所得|后|完成后)", after):
        return ""
    candidate = re.split(r"[，,。；;]|(?:在|于|至|到)\s*[-+]?\d", after, maxsplit=1)[0].strip()
    candidate = re.sub(r"^(?:到|至|入|进)", "", candidate).strip()
    # Stop before a second concrete operation in the same clause.
    for hit in _ACTION_RE.finditer(candidate):
        tail = candidate[hit.end(): hit.end() + 10]
        if not _ACTION_NOUN_SUFFIX_RE.match(tail):
            candidate = candidate[: hit.start()].strip()
            break
    candidate = re.sub(r"(?:后|后再|后进行)$", "", candidate).strip()
    if candidate in {"浓缩", "沉淀", "结晶", "脱水", "工序"}:
        return ""
    return candidate[:120]


def _process_type(block: TextBlock) -> str:
    title = block.heading_title or ""
    for token in ("制备", "合成", "加工", "装配", "测试", "试验", "处理"):
        if token in title:
            return token + "流程"
    return "工艺流程"


def _process_name(block: TextBlock, anchor: TextSubjectAnchor) -> str:
    title = re.sub(r"^\s*\d+(?:[.．]\d+)*\s*", "", block.heading_title or "").strip()
    if title and _PROCESS_HEADING_RE.search(title):
        return title
    return f"{anchor.subject}{_process_type(block)}"


_RELIABLE_PROCESS_ANCHOR_SOURCES = {
    "identity_field",
    "heading_anchor",
    "explicit_process_subject_span",
    "explicit_process_final_product_span",
    "explicit_process_local_entity_span",
    "same_heading_identity_inheritance",
    "bounded_heading_inheritance",
    "closed_set_subject_llm",
}


def _is_reliable_process_anchor(block: TextBlock, anchor: TextSubjectAnchor) -> bool:
    if not is_plausible_subject_name(anchor.subject):
        return False
    if anchor.source in _RELIABLE_PROCESS_ANCHOR_SOURCES:
        return True
    if anchor.source == "unique_confirmed_registry_mention":
        # A unique name in the body may be a reagent or intermediate rather
        # than the process product. It is safe without LLM only when the same
        # registered subject is also explicitly scoped by the heading path.
        heading_context = " > ".join(block.heading_path)
        return bool(anchor.subject and anchor.subject in heading_context)
    return False


def extract_process_step_facts(
    blocks: Sequence[TextBlock],
    anchors_by_block: Mapping[str, TextSubjectAnchor],
) -> Tuple[List[TextFactRecord], List[ProcessStepAudit], set[str]]:
    records: List[TextFactRecord] = []
    audits: List[ProcessStepAudit] = []
    blocks_with_steps: set[str] = set()
    model_root = Path(__file__).resolve().parents[2]
    adjudicator = ProcessStepLLMAdjudicator(
        enabled=os.getenv("KGCHOUQU_PROCESS_LLM_ENABLED") == "1",
        cache_dir=model_root / "cache" / "process_step_phase91",
    )

    for block in blocks:
        anchor = anchors_by_block.get(block.block_id)
        if anchor is None or anchor.status != "confirmed":
            continue
        if not _is_reliable_process_anchor(block, anchor):
            # Precision-first fail-closed behavior: do not attach a process
            # chain to a material that is only mentioned as a possible reagent.
            continue
        heading_context = " > ".join(block.heading_path)
        if _NON_MATERIAL_PROCESS_HEADING_RE.search(heading_context):
            # Computational/expert-system workflow is not a material operation
            # chain and must not enter the material-process projection.
            continue
        is_process = block.role == "process" or bool(_PROCESS_HEADING_RE.search(block.heading_title or ""))
        if not is_process:
            continue
        step_specs = _explicit_steps(block.text, block.heading_title) or _connector_steps(block.text)
        if not step_specs:
            step_specs = _deterministic_action_sequence(block.text, block.heading_title)
        if not step_specs:
            step_specs = _llm_steps(block, anchor, adjudicator)
        if not step_specs:
            continue
        process_name = _process_name(block, anchor)
        process_type = _process_type(block)
        process_id = "proc:" + hashlib.sha1(
            f"{anchor.subject}|{process_name}|{block.block_id}|{block.text}".encode("utf-8")
        ).hexdigest()[:18]
        temporary: List[TextFactRecord] = []
        audit_buffer: List[ProcessStepAudit] = []
        for ordinal, (step_index, step_label, clause, ordering_source) in enumerate(step_specs, start=1):
            action, raw_action = _canonical_action(clause, near_start=True)
            object_text = _object_text(clause, raw_action)
            condition_text = _condition_text(clause)
            result_text = _result_text(clause)
            step_id = "step:" + hashlib.sha1(
                f"{process_id}|{step_index}|{clause}".encode("utf-8")
            ).hexdigest()[:18]
            source_confidence = (
                0.92 if ordering_source == "explicit_step_marker"
                else (0.88 if ordering_source == "closed_set_process_llm" else (0.87 if ordering_source == "deterministic_action_sequence" else 0.86))
            )
            confidence = min(0.95, anchor.confidence, source_confidence)
            record = TextFactRecord(
                record_id="txtproc:" + hashlib.sha1(f"{step_id}|{clause}".encode("utf-8")).hexdigest()[:20],
                block_id=block.block_id,
                subject=anchor.subject,
                subject_type=anchor.subject_type,
                property_name="制备工艺步骤" if process_type in {"制备流程", "合成流程", "加工流程"} else "操作步骤",
                value_text=clause,
                normalized_value_text=clause,
                relation_kind="process_step",
                source_type="text_process_step",
                heading_path=list(block.heading_path),
                line_start=block.line_start,
                line_end=block.line_end,
                evidence=block.text,
                confidence=confidence,
                record_status="ready" if action else "candidate",
                unresolved_reasons=[] if action else ["missing_specific_step_action"],
                process_id=process_id,
                process_name=process_name,
                process_type=process_type,
                step_id=step_id,
                step_index=step_index or ordinal,
                step_label=clause,
                step_action=action,
                step_object=object_text,
                step_condition_text=condition_text,
                step_result_text=result_text,
                anchor_source=anchor.source,
            )
            temporary.append(record)
            audit_buffer.append(
                ProcessStepAudit(
                    block.block_id, process_id, step_id, record.step_index, action, object_text,
                    condition_text, result_text, ordering_source, record.record_status,
                    list(record.unresolved_reasons), block.text, clause,
                )
            )
        for index, record in enumerate(temporary):
            record.previous_step_id = temporary[index - 1].step_id if index > 0 else ""
            record.next_step_id = temporary[index + 1].step_id if index + 1 < len(temporary) else ""
        records.extend(temporary)
        audits.extend(audit_buffer)
        blocks_with_steps.add(block.block_id)
    return records, audits, blocks_with_steps


__all__ = ["ProcessStepAudit", "extract_process_step_facts", "_llm_step_candidates"]
