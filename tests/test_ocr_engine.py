"""Контракт учебного корпуса и сайта проверяется без скачивания весов."""
from app.ocr.engine import compose_input, input_digest, same_box, reading_order


def item(text="Я и мой баг", sha="first", status="complete"):
    return {"media_sha256": sha, "engine_version": "ocr-v1", "status": status,
            "blocks": [{"text": text, "score": .9, "box": [[0, 0], [30, 0], [30, 10], [0, 10]]}]}


def test_empty_caption_with_readable_media_is_not_empty():
    assert "Я и мой баг" in compose_input(None, [item()])
    assert compose_input(None, [{"blocks": []}]) == ""
    assert compose_input(" ", []) == ""


def test_input_digest_separates_textless_pictures_and_detects_ocr_changes():
    assert input_digest(None, [item(sha="first")]) != input_digest(None, [item(sha="second")])
    assert input_digest("caption", [item()]) != input_digest("edited", [item()])
    assert input_digest("caption", [item()]) != input_digest("caption", [item("other")])
    assert input_digest("caption", [item()]) != input_digest("caption", [item(status="needs_review")])


def test_original_caption_is_not_changed_and_album_order_is_explicit():
    original = "  Исходный текст\nбез OCR  "
    result = compose_input(original, [item("Первое"), item("Второе")])
    assert result.index("Первое") < result.index("Второе")
    assert "[Медиа 1]" in result and "[Медиа 2]" in result
    assert original == "  Исходный текст\nбез OCR  "


def test_bilingual_reads_merge_same_region_not_different_lines():
    box = [[0, 0], [30, 0], [30, 10], [0, 10]]
    assert same_box(box, [[1, 0], [31, 0], [31, 10], [1, 10]])
    assert not same_box(box, [[0, 15], [30, 15], [30, 25], [0, 25]])


def test_tilted_words_keep_left_to_right_reading_order():
    blocks = [
        {"text": "справа", "box": [[100, 0], [140, 0], [140, 20], [100, 20]]},
        {"text": "слева", "box": [[0, 2], [40, 2], [40, 22], [0, 22]]},
        {"text": "потом", "box": [[0, 40], [40, 40], [40, 60], [0, 60]]}]
    assert [b["text"] for b in reading_order(blocks)] == ["слева", "справа", "потом"]
