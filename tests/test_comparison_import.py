import json

import pytest

from app.import_comparison import load_cohort


def test_cohort_requires_exact_reserved_posts(tmp_path):
    path = tmp_path / "cohort.jsonl"
    rows = [
        {"chat_peer_id": -1001, "message_id": 5, "partition_hint": "blind_test_candidate"},
        {"chat_peer_id": -1001, "message_id": 6, "partition_hint": "blind_test_candidate"},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    selected = load_cohort(path, 2)
    assert set(selected[-1001]) == {5, 6}
    with pytest.raises(ValueError, match="expected 3"):
        load_cohort(path, 3)


def test_cohort_rejects_duplicate_and_training_rows(tmp_path):
    path = tmp_path / "cohort.jsonl"
    row = {"chat_peer_id": -1001, "message_id": 5, "partition_hint": "blind_test_candidate"}
    path.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_cohort(path, 2)
    row["partition_hint"] = "development"
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="blind test"):
        load_cohort(path, 1)
