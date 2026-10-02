from types import SimpleNamespace
import pytest
from pydantic import ValidationError
from telethon.tl.types import PhotoSize, PhotoSizeProgressive, VideoSize
from app.content.selection_rules import taxonomy_catalog, validate_expression
from app.content.selection_filters import assessment, needs_backfill
from app.taxonomy.inference import format_result
from app.taxonomy.jobs import text_sha256
from app.taxonomy.profiles import job_key
from app.ocr.media import static_thumbnail, safe_path
from app.web.selection_routes import FilterInput


def condition(label="is_joke"):
    return {"op": "condition", "label_id": label, "compare": "gte", "threshold": 92}


def test_humor_only_exposes_two_actual_scores():
    config = {"version": "humor_ocr_v1", "profile": "humor_ocr", "categories": [],
        "binary_features": ["is_joke", "input_has_context"], "feature_names": {"input_has_context": "Хватает контекста"}}
    result = format_result(config, {"is_joke": .97, "input_has_context": .99}, None)
    assert set(result["scores"]) == {"is_joke", "input_has_context"}
    assert len(result["features"]) == 2
    assert result["top_3"] == [] and result["technical_complexity"] is None
    assert result["review_status"] == "scored"
    assert job_key("tfidf") != job_key("tfidf", "humor_ocr")


def test_filter_profiles_validate_their_own_labels_and_preserve_dictionary_mark():
    data = {"name": "Юмор", "mark_id": 44, "expression": condition("input_has_context"), "profile": "humor_ocr"}
    assert FilterInput.model_validate(data).mark_id == 44
    data["expression"] = condition("models")
    with pytest.raises(ValidationError): FilterInput.model_validate(data)
    assert len(taxonomy_catalog("humor_ocr")["labels"]) == 2
    with pytest.raises(ValueError): validate_expression(condition("missing"), "humor_ocr")


def test_empty_caption_with_ocr_can_match_but_taxonomy_cannot_reuse_its_result():
    post = SimpleNamespace(text="", is_deleted=False)
    version = SimpleNamespace(profile="humor_ocr", expression=condition())
    job = SimpleNamespace(profile="humor_ocr", current_run_id=12, text_sha256=text_sha256(""),
        input_sha256="ocr-input", status="complete", result={"taxonomy_version": "humor_ocr_v1", "scores": {"is_joke": .98}})
    assert assessment(version, job, post)["outcome"] == "matched"
    version.profile = "taxonomy"
    assert assessment(version, job, post)["outcome"] == "unknown"
    job.status = "ocr"
    assert not needs_backfill(job, post, "humor_ocr")
    job.status = "needs_review"
    assert not needs_backfill(job, post, "humor_ocr")


def test_largest_static_preview_wins_even_when_video_thumb_is_last():
    small = PhotoSize(type="s", w=100, h=100, size=1000)
    large = PhotoSizeProgressive(type="x", w=600, h=400, sizes=[100, 200])
    moving = VideoSize(type="v", w=1000, h=1000, size=999999)
    message = SimpleNamespace(document=SimpleNamespace(thumbs=[small, large, moving]))
    assert static_thumbnail(message) is large
    message.document.thumbs = [moving]
    assert static_thumbnail(message) is None


def test_ocr_cannot_read_file_outside_its_media_mount(tmp_path):
    root = tmp_path / "media"; root.mkdir()
    outside = tmp_path / "private.txt"; outside.write_text("not media")
    inside = root / "image.png"; inside.write_bytes(b"fixture")
    assert safe_path(outside, root) is None
    assert safe_path(inside, root) == inside.resolve()


def test_input_contract_accepts_512_tokens_and_never_silently_truncates(tmp_path):
    from tokenizers import Tokenizer, models, pre_tokenizers
    from app.taxonomy.input_contract import InputGuard, InputNeedsReview
    tokenizer = Tokenizer(models.WordLevel({"[UNK]": 0, "текст": 1}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer.enable_truncation(512)
    tokenizer.save(str(tmp_path / "input-tokenizer.json"))
    guard = InputGuard(tmp_path)
    guard.check(" ".join(["текст"] * 512))
    with pytest.raises(InputNeedsReview):
        guard.check(" ".join(["текст"] * 513))
