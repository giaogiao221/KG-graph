from book_engine.core.schemas import (
    ConditionAtom,
    ConditionalFactRecord,
    TableCell,
    TableSemanticPlan,
)


def test_schema_objects_can_be_created():
    cell = TableCell(table_id="T1", row_index=0, column_index=0, raw_text="RDX")
    condition = ConditionAtom(
        name="pressure",
        condition_type="environment",
        value_text="5 MPa",
        unit="MPa",
        value_num=5.0,
    )
    plan = TableSemanticPlan(table_id="T1", topology="condition_by_property")
    fact = ConditionalFactRecord(
        subject="RDX",
        subject_type="material",
        property_name="burning_rate",
        value_text="8 mm/s",
        unit="mm/s",
        conditions=[condition],
    )
    assert cell.raw_text == "RDX"
    assert plan.topology == "condition_by_property"
    assert fact.conditions[0].value_num == 5.0