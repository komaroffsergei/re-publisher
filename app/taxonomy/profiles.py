"""Обычная таксономия и отдельный двухметочный профиль юмора."""
PROFILES = ("taxonomy", "humor_ocr")
HUMOR_LABELS = [
    {"id": "is_joke", "name": "Шутка", "parent": None, "kind": "feature"},
    {"id": "input_has_context", "name": "Хватает контекста", "parent": None, "kind": "feature"},
]


def profile_of(value):
    return getattr(value, "profile", None) or "taxonomy"


def source_of(value):
    return getattr(value, "input_source", None) or ("combined" if profile_of(value) == "humor_ocr" else "text")


def job_key(model_key, profile="taxonomy", input_source=None):
    input_source = input_source or ("combined" if profile == "humor_ocr" else "text")
    base = model_key if profile == "taxonomy" else f"{profile}:{model_key}"
    return base if input_source in {"text", "combined"} else f"{base}:{input_source}"


def profile_labels(profile):
    if profile == "humor_ocr":
        return HUMOR_LABELS
    if profile != "taxonomy":
        raise ValueError("Unknown classification profile")
    from app.content.selection_rules import taxonomy_catalog
    return taxonomy_catalog()["labels"]
