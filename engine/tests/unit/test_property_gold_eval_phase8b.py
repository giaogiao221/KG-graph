from pathlib import Path
import sys

MODEL_SRC = Path(__file__).resolve().parents[2] / "src"
if str(MODEL_SRC) not in sys.path:
    sys.path.insert(0, str(MODEL_SRC))

from book_engine.evaluation.property_gold import GoldSample, sample_gold_records
from book_engine.evaluation.property_metrics import evaluate_gold_rows


def test_stratified_sampler_is_reproducible():
    records = []
    for index in range(12):
        row = {
            "sample_id": f"s{index}",
            "run_name": "run",
            "来源类型": "table" if index % 2 == 0 else "text",
        }
        records.append(GoldSample(row=row, stratum=f"g{index % 3}", risk_score=float(index % 4) / 2))
    first = sample_gold_records(records, sample_size=7, seed=42)
    second = sample_gold_records(records, sample_size=7, seed=42)
    assert [item["sample_id"] for item in first] == [item["sample_id"] for item in second]
    assert len(first) == 7


def test_metrics_exact_property():
    rows = [
        {
            "annotation_status": "completed",
            "predicted_attribute_category": "物理属性",
            "predicted_attribute_name": "密度",
            "predicted_accepted": "1",
            "gold_attribute_category": "物理属性",
            "gold_attribute_name": "密度",
            "gold_accept": "1",
            "gold_subject_correct": "1",
            "gold_value_correct": "1",
            "gold_condition_correct": "1",
            "gold_method_correct": "1",
            "来源类型": "table_conditional_record",
            "predicted_status": "aligned_exact",
            "run_name": "r1",
        }
    ]
    report = evaluate_gold_rows(
        rows,
        {
            "category_accuracy": 0.95,
            "name_accuracy": 0.90,
            "full_property_accuracy": 0.88,
            "accept_accuracy": 0.95,
        },
    )
    assert report["ok"] is True
    assert report["full_property"]["accuracy"] == 1.0
