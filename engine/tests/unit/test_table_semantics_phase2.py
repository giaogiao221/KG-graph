from book_engine.core.schemas import HeaderPath, HeaderTree, SourceLocation, TableBlock, TableCell, TableGrid
from book_engine.tables.semantic_planner import build_semantic_plan


def make_grid(rows, header_rows=(0,), row_header_columns=(0,)):
    cells = []
    for r, values in enumerate(rows):
        row = []
        for c, value in enumerate(values):
            row.append(
                TableCell(
                    table_id="T1",
                    row_index=r,
                    column_index=c,
                    raw_text=value,
                    normalized_text=value,
                    is_header=r in header_rows,
                    origin_row=r,
                    origin_column=c,
                    source=SourceLocation(source_path="test", table_id="T1"),
                )
            )
        cells.append(row)
    grid = TableGrid(
        table_id="T1",
        source_type="html",
        cells=cells,
        raw_row_count=len(rows),
        raw_column_count=len(rows[0]),
        row_count=len(rows),
        column_count=len(rows[0]),
    )
    column_paths = [
        HeaderPath(axis="column", index=c, labels=[rows[0][c]])
        for c in range(len(rows[0]))
    ]
    row_paths = [
        HeaderPath(axis="row", index=r, labels=[rows[r][0]])
        for r in range(1, len(rows))
    ]
    tree = HeaderTree(
        table_id="T1",
        header_rows=list(header_rows),
        row_header_columns=list(row_header_columns),
        column_paths=column_paths,
        row_paths=row_paths,
        confidence=0.9,
    )
    block = TableBlock(
        table_id="T1",
        source_type="html",
        raw_text="",
        line_start=1,
        line_end=5,
        heading="test",
    )
    return block, grid, tree


def test_entity_by_property_table():
    block, grid, tree = make_grid(
        [
            ["材料名称", "密度/(g/cm3)", "爆速/(m/s)"],
            ["RDX", "1.81", "8750"],
            ["HMX", "1.96", "9100"],
        ]
    )
    plan, axes, cells, topology = build_semantic_plan(block, grid, tree)
    assert plan.topology == "entity_by_property"
    assert plan.orientation == "row_subject"
    assert any(axis.role == "entity" and axis.index == 0 for axis in axes)
    assert sum(axis.role == "property_value" for axis in axes) >= 2
    assert any(cell.role == "measured_result" for cell in cells)


def test_condition_by_property_table():
    block, grid, tree = make_grid(
        [
            ["压力/MPa", "燃速/(mm/s)"],
            ["5", "8.1"],
            ["10", "11.7"],
        ]
    )
    plan, axes, cells, topology = build_semantic_plan(block, grid, tree)
    assert plan.topology == "condition_by_property"
    assert any(axis.role == "condition" for axis in axes)
    assert any(cell.role == "condition_value" for cell in cells)


def test_composition_and_performance_table():
    block, grid, tree = make_grid(
        [
            ["配方号", "AP/%", "Al/%", "HTPB/%", "比冲/s"],
            ["P1", "70", "15", "15", "260"],
            ["P2", "68", "18", "14", "265"],
        ]
    )
    plan, axes, cells, topology = build_semantic_plan(block, grid, tree)
    assert plan.topology == "composition_and_performance"
    assert len(plan.composition_axes) >= 2
    assert any(cell.role == "formulation_component_value" for cell in cells)


def test_model_validation_table():
    block, grid, tree = make_grid(
        [
            ["样品", "实验值", "预测值", "误差"],
            ["A", "10.0", "9.8", "2.0%"],
            ["B", "20.0", "20.5", "2.5%"],
        ]
    )
    plan, axes, cells, topology = build_semantic_plan(block, grid, tree)
    assert plan.topology == "model_validation"
    assert any(cell.role == "measured_result" for cell in cells)
    assert any(cell.role == "calculated_result" for cell in cells)
    assert any(cell.role == "comparison_metric" for cell in cells)


def test_unresolved_numeric_is_auditable():
    block, grid, tree = make_grid(
        [
            ["A", "B"],
            ["x", "10"],
            ["y", "20"],
        ]
    )
    plan, axes, cells, topology = build_semantic_plan(block, grid, tree)
    assert plan.topology in {"entity_by_property", "qualitative_taxonomy"}
    assert isinstance(plan.unresolved_reasons, list)
