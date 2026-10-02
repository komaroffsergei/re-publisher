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


def test_long_caption_has_same_text_only_input_as_runtime(tmp_path):
    from app.ocr.engine import compose_input,input_digest
    caption = "а" * 501
    row = {"peer":1,"message":2,"grouped_id":None,"date":"2026-10-03", "caption":caption,
        "media_sha256":"image-one","ocr":{"status":"no_text","blocks":[],"engine_version":"fixture"}}
    raw, labels = tmp_path / "ocr.jsonl", tmp_path / "labels.jsonl"
    raw.write_text(json.dumps(row),encoding="utf-8")
    labels.write_text(json.dumps({"peer":1,"message":2,"input_sha256":input_digest(caption,[]),
        "is_joke":"нет","input_has_context":"да","reason":"fixture", "annotator":"Codex /root"}),encoding="utf-8")
    assembled, excluded = corpus.assemble([raw],[labels])
    assert not excluded
    assert assembled[0]["text"] == compose_input(caption,[])
    assert assembled[0]["ocr_eligible"]
    row["media_sha256"] = None
    raw.write_text(json.dumps(row),encoding="utf-8")
    assert not corpus.assemble([raw],[labels])[0][0]["ocr_eligible"]
