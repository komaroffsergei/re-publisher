import importlib.util
import json
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location("humor_corpus", Path(__file__).parents[1] / "scripts/humor_corpus.py")
corpus = importlib.util.module_from_spec(spec); spec.loader.exec_module(corpus)


def test_media_identity_separates_empty_captions_and_groups_reused_files():
    rows = [{"sha": str(i), "media_sha256": [media]} for i, media in enumerate(["picture1", "picture2", "picture1"])]
    corpus.partition(rows)
    assert rows[0]["group_id"] != rows[1]["group_id"]
    assert rows[0]["group_id"] == rows[2]["group_id"]
    assert rows[0]["split"] == rows[2]["split"]


def test_scripts_reject_prediction_labels_and_incomplete_quota(tmp_path):
    labels = tmp_path / "annotation.jsonl"
    labels.write_text(json.dumps({"is_joke":"да", "input_has_context":"да", "reason":"fixture", "annotator":"model"}))
    with pytest.raises(ValueError): corpus.decisions([labels])
    with pytest.raises(ValueError): corpus.freeze([], tmp_path / "dataset")
    assert not (tmp_path / "dataset").exists()


def test_unclear_labels_low_confidence_and_incomplete_albums_do_not_fill_quota():
    rows = [{"split":"train", "group_id":str(i), "ocr_eligible":eligible, "tokens":20,
        "labels":{"is_joke":joke, "input_has_context":context}} for i, (eligible,joke,context) in enumerate([
            (True,"да","да"), (True,"неясно","да"), (True,"да","неясно"), (False,"да","да"), (True,"нет","да")])]
    count = corpus.coverage(rows)["train"]
    assert count["positive"] == 1 and count["negative"] == 1
