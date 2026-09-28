from .adapter import build_specification_text_layer, build_specification_table_layer
from .parser import SpecificationEntry, parse_specification_entries

__all__ = [
    "SpecificationEntry",
    "parse_specification_entries",
    "build_specification_text_layer",
    "build_specification_table_layer",
]
