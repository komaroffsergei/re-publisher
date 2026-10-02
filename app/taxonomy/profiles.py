"""Обычная таксономия и отдельный двухметочный профиль юмора."""
PROFILES = ("taxonomy", "humor_ocr")
HUMOR_LABELS = [
    {"id": "is_joke", "name": "Шутка", "parent": None, "kind": "feature"},
    {"id": "input_has_context", "name": "Хватает контекста", "parent": None, "kind": "feature"},
]


def profile_of(value):
    return getattr(value, "profile", None) or "taxonomy"


def job_key(model_key, profile="taxonomy"):
    return model_key if profile == "taxonomy" else f"{profile}:{model_key}"


def profile_labels(profile):
    if profile == "humor_ocr":
        return HUMOR_LABELS
    if profile != "taxonomy":
        raise ValueError("Unknown classification profile")
    from app.content.selection_rules import taxonomy_catalog
    return taxonomy_catalog()["labels"]
