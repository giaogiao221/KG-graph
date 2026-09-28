from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class SourceLocation:
    source_path: str
    block_id: str = ""
    table_id: str = ""
    row_index: Optional[int] = None
    column_index: Optional[int] = None
    line_start: Optional[int] = None
    line_end: Optional[int] = None


@dataclass
class TableBlock:
    table_id: str
    source_type: str
    raw_text: str
    line_start: int
    line_end: int
    heading: str = ""
    preceding_text: str = ""
    following_text: str = ""
    heading_path: List[str] = field(default_factory=list)


@dataclass
class TableCell:
    table_id: str
    row_index: int
    column_index: int
    raw_text: str
    normalized_text: str = ""
    rowspan: int = 1
    colspan: int = 1
    is_header: bool = False
    origin_row: Optional[int] = None
    origin_column: Optional[int] = None
    is_span_copy: bool = False
    source: Optional[SourceLocation] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TableGrid:
    table_id: str
    source_type: str
    cells: List[List[TableCell]]
    raw_row_count: int
    raw_column_count: int
    row_count: int
    column_count: int
    has_rowspan: bool = False
    has_colspan: bool = False
    warnings: List[str] = field(default_factory=list)

    def iter_cells(self):
        for row in self.cells:
            for cell in row:
                yield cell


@dataclass
class HeaderPath:
    axis: str
    index: int
    labels: List[str] = field(default_factory=list)
    unit: str = ""
    semantic_role: str = ""
    confidence: float = 0.0


@dataclass
class HeaderTree:
    table_id: str
    header_rows: List[int] = field(default_factory=list)
    row_header_columns: List[int] = field(default_factory=list)
    column_paths: List[HeaderPath] = field(default_factory=list)
    row_paths: List[HeaderPath] = field(default_factory=list)
    confidence: float = 0.0
    warnings: List[str] = field(default_factory=list)


@dataclass
class AxisRoleDecision:
    table_id: str
    axis: str
    index: int
    label_path: List[str] = field(default_factory=list)
    role: str = "unknown"
    unit: str = ""
    numeric_ratio: float = 0.0
    unique_ratio: float = 0.0
    confidence: float = 0.0
    reasons: List[str] = field(default_factory=list)


@dataclass
class TopologyDecision:
    table_id: str
    topology: str
    orientation: str = "row_subject"
    confidence: float = 0.0
    reasons: List[str] = field(default_factory=list)
    unresolved_reasons: List[str] = field(default_factory=list)


@dataclass
class CellRoleDecision:
    table_id: str
    row_index: int
    column_index: int
    role: str
    confidence: float
    reasons: List[str] = field(default_factory=list)


@dataclass
class ParsedValue:
    raw_text: str
    normalized_text: str = ""
    value_num: Optional[float] = None
    lower_bound: Optional[float] = None
    upper_bound: Optional[float] = None
    comparator: str = ""
    unit: str = ""
    is_range: bool = False
    is_ratio: bool = False
    parse_confidence: float = 0.0


@dataclass
class ConditionAtom:
    name: str
    condition_type: str
    value_text: str
    unit: str = ""
    value_num: Optional[float] = None
    lower_bound: Optional[float] = None
    upper_bound: Optional[float] = None
    comparator: str = ""
    normalized_name: str = ""
    scope: str = "table"
    priority: int = 0
    confidence: float = 0.0
    condition_id: str = ""
    source_kind: str = ""
    source_text: str = ""
    target_row: Optional[int] = None
    target_column: Optional[int] = None
    binding_target: str = ""
    is_conflict: bool = False
    reasons: List[str] = field(default_factory=list)
    source: Optional[SourceLocation] = None


@dataclass
class ConditionCandidate:
    condition_id: str
    table_id: str
    name: str
    normalized_name: str
    condition_type: str
    value_text: str
    unit: str = ""
    value_num: Optional[float] = None
    lower_bound: Optional[float] = None
    upper_bound: Optional[float] = None
    comparator: str = ""
    scope: str = "table"
    source_kind: str = ""
    source_text: str = ""
    target_row: Optional[int] = None
    target_column: Optional[int] = None
    priority: int = 0
    confidence: float = 0.0
    reasons: List[str] = field(default_factory=list)
    source: Optional[SourceLocation] = None

    def to_atom(self, binding_target: str = "") -> ConditionAtom:
        return ConditionAtom(
            name=self.name,
            normalized_name=self.normalized_name,
            condition_type=self.condition_type,
            value_text=self.value_text,
            unit=self.unit,
            value_num=self.value_num,
            lower_bound=self.lower_bound,
            upper_bound=self.upper_bound,
            comparator=self.comparator,
            scope=self.scope,
            priority=self.priority,
            confidence=self.confidence,
            condition_id=self.condition_id,
            source_kind=self.source_kind,
            source_text=self.source_text,
            target_row=self.target_row,
            target_column=self.target_column,
            binding_target=binding_target,
            reasons=list(self.reasons),
            source=self.source,
        )


@dataclass
class ConditionBinding:
    record_id: str
    condition_id: str
    table_id: str
    row_index: Optional[int]
    column_index: Optional[int]
    binding_target: str
    applied: bool
    overridden: bool = False
    conflict: bool = False
    reason: str = ""


@dataclass
class TableSemanticPlan:
    table_id: str
    topology: str
    orientation: str = "row_subject"
    subject_axes: List[Dict[str, Any]] = field(default_factory=list)
    property_axes: List[Dict[str, Any]] = field(default_factory=list)
    condition_axes: List[Dict[str, Any]] = field(default_factory=list)
    value_axes: List[Dict[str, Any]] = field(default_factory=list)
    method_axes: List[Dict[str, Any]] = field(default_factory=list)
    composition_axes: List[Dict[str, Any]] = field(default_factory=list)
    identifier_axes: List[Dict[str, Any]] = field(default_factory=list)
    group_axes: List[Dict[str, Any]] = field(default_factory=list)
    note_axes: List[Dict[str, Any]] = field(default_factory=list)
    global_conditions: List[ConditionAtom] = field(default_factory=list)
    confidence: float = 0.0
    unresolved_reasons: List[str] = field(default_factory=list)
    llm_used: bool = False
    llm_status: str = ""
    llm_confidence: float = 0.0
    llm_reason: str = ""


@dataclass
class ConditionalFactRecord:
    subject: str
    subject_type: str
    property_name: str
    value_text: str
    unit: str = ""
    value_num: Optional[float] = None
    lower_bound: Optional[float] = None
    upper_bound: Optional[float] = None
    comparator: str = ""
    value_role: str = ""
    method: str = ""
    instrument: str = ""
    sample_id: str = ""
    conditions: List[ConditionAtom] = field(default_factory=list)
    evidence: str = ""
    confidence: float = 0.0
    source: Optional[SourceLocation] = None
    record_id: str = ""
    table_id: str = ""
    row_index: Optional[int] = None
    column_index: Optional[int] = None
    property_role: str = ""
    normalized_value_text: str = ""
    subject_source: str = ""
    row_header_path: List[str] = field(default_factory=list)
    column_header_path: List[str] = field(default_factory=list)
    record_status: str = "candidate"
    unresolved_reasons: List[str] = field(default_factory=list)
