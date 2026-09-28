from book_engine.core.schemas import TableBlock
from book_engine.tables.grid_rebuilder import rebuild_grid
from book_engine.tables.header_tree_builder import build_header_tree


def test_multi_level_column_header_paths():
    raw = """
    <table>
      <tr><th rowspan="2">材料</th><th colspan="2">性能</th></tr>
      <tr><th>密度/(g/cm3)</th><th>爆速/(m/s)</th></tr>
      <tr><td>RDX</td><td>1.81</td><td>8750</td></tr>
    </table>
    """
    grid = rebuild_grid(
        TableBlock("T00001", "html", raw, 1, 5)
    )
    tree = build_header_tree(grid)
    assert tree.header_rows == [0, 1]
    assert tree.row_header_columns == [0]
    assert tree.column_paths[1].labels == ["性能", "密度/(g/cm3)"]
    assert tree.column_paths[1].unit == "g/cm3"
    assert tree.row_paths[0].labels == ["RDX"]
