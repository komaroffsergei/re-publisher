"""Load the new Codex-trained TF-IDF baseline for single-post scoring."""

from __future__ import annotations

import json
from pathlib import Path

import joblib
import numpy as np
from scipy.sparse import hstack


MODEL_VERSION = "codex-tfidf-3000-20260929"
FEATURE_NAMES = {
    "is_ad": "Реклама",
    "is_event_related": "Про мероприятие",
    "is_event_invitation": "Приглашение",
    "is_job_vacancy": "Вакансия",
    "is_scientific_paper": "Научная статья",
    "is_joke": "Шутка",
}


def format_result(taxonomy: dict, scores: dict[str, float], complexity: float) -> dict:
    categories = sorted(taxonomy["categories"], key=lambda category: scores[category["id"]], reverse=True)[:3]
    top = [
        {
            "id": category["id"], "name": category["name"],
            "score": round(scores[category["id"]], 4),
            "subcategories": [
                {"id": child["id"], "name": child["name"], "score": round(scores[child["id"]], 4)}
                for child in category["subcategories"]
            ],
        }
        for category in categories
    ]
    return {
        "top_3": top,
        "features": [
            {"id": name, "name": FEATURE_NAMES[name], "score": round(scores[name], 4)}
            for name in taxonomy["binary_features"]
        ],
        "technical_complexity": int(np.rint(np.clip(complexity, 0, 5))),
        "review_status": "needs_review" if not top or top[0]["score"] < 0.55 else "scored",
        "score_kind": "uncalibrated_model_score",
    }


class TaxonomyModel:
    def __init__(self, model_dir: str | Path):
        model_dir = Path(model_dir)
        self.bundle = joblib.load(model_dir / "baseline.joblib")
        self.taxonomy = json.loads((model_dir / "taxonomy.json").read_text(encoding="utf-8"))
        if self.bundle["taxonomy_version"] != self.taxonomy["version"]:
            raise ValueError("taxonomy version differs from model")
        categories = self.taxonomy["categories"]
        expected = [category["id"] for category in categories]
        expected += [child["id"] for category in categories for child in category["subcategories"]]
        expected += self.taxonomy["binary_features"]
        if set(expected) != set(self.bundle["label_names"]):
            raise ValueError("taxonomy labels differ from model")

    def classify(self, text: str) -> dict:
        sparse = hstack([
            self.bundle["word"].transform([text]),
            self.bundle["char"].transform([text]),
        ]).tocsr()
        scores = {name: float(self.bundle["models"][name].predict_proba(sparse)[0, 1])
                  for name in self.bundle["label_names"]}
        complexity = float(self.bundle["complexity"].predict(sparse)[0])
        return format_result(self.taxonomy, scores, complexity)
