from book_engine.core.schemas import TableBlock
from book_engine.tables.grid_rebuilder import rebuild_grid


def block(raw: str, source_type: str = "html") -> TableBlock:
    return TableBlock(
        table_id="T00001",
        source_type=source_type,
        raw_text=raw,
        line_start=1,
        line_end=1,
    )


def test_html_rowspan_and_colspan_are_expanded():
    raw = """
    <table>
      <tr><th rowspan="2">材料</th><th colspan="2">性能</th></tr>
      <tr><th>密度</th><th>爆速</th></tr>
      <tr><td>RDX</td><td>1.81</td><td>8750</td></tr>
    </table>
    """
    grid = rebuild_grid(block(raw))
    assert grid.row_count == 3
    assert grid.column_count == 3
    assert grid.cells[1][0].normalized_text == "材料"
    assert grid.cells[1][0].is_span_copy is True
    assert grid.cells[0][2].normalized_text == "性能"
    assert grid.has_rowspan is True
    assert grid.has_colspan is True


def test_markdown_table_is_rebuilt():
    raw = """| 材料 | 密度 |
|---|---|
| RDX | 1.81 |
"""
    grid = rebuild_grid(block(raw, "markdown"))
    assert grid.row_count == 2
    assert grid.column_count == 2
    assert grid.cells[0][0].is_header is True
    assert grid.cells[1][1].normalized_text == "1.81"
