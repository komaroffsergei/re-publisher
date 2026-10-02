"""Load the new Codex-trained TF-IDF baseline for single-post scoring."""

from __future__ import annotations

import json
from pathlib import Path

import joblib
import numpy as np
from scipy.sparse import hstack


from app.taxonomy.labels import FEATURE_NAMES
from app.taxonomy.artifact import artifact_version, configured_artifact

def format_result(taxonomy: dict, scores: dict[str, float], complexity: float | None) -> dict:
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
        "taxonomy_version": taxonomy.get("version"),
        "scores": {label: round(score, 4) for label, score in scores.items()},
        "top_3": top,
        "features": [
            {"id": name, "name": taxonomy.get('feature_names', {}).get(name, FEATURE_NAMES.get(name, name)), "score": round(scores[name], 4)}
            for name in taxonomy["binary_features"]
        ],
        "profile": taxonomy.get("profile", "taxonomy"),
        "technical_complexity": int(np.rint(np.clip(complexity, 0, 5))) if complexity is not None else None,
        "review_status": "scored" if taxonomy.get("profile") == "humor_ocr" else "needs_review" if not top or top[0]["score"] < 0.55 else "scored",
        "score_kind": "uncalibrated_model_score",
    }


class TaxonomyModel:
    def __init__(self, model_dir: str | Path):
        model_dir = Path(model_dir)
        weights = configured_artifact(model_dir, 'tfidf')
        self.model_version = artifact_version(model_dir, 'tfidf', weights)
        self.bundle = joblib.load(weights)
        self.taxonomy = json.loads((model_dir / "taxonomy.json").read_text(encoding="utf-8"))
        self.input_guard = None
        if self.taxonomy.get("profile") == "humor_ocr":
            from app.taxonomy.input_contract import InputGuard
            self.input_guard = InputGuard(model_dir)
        if self.bundle["taxonomy_version"] != self.taxonomy["version"]:
            raise ValueError("taxonomy version differs from model")
        categories = self.taxonomy["categories"]
        expected = [category["id"] for category in categories]
        expected += [child["id"] for category in categories for child in category["subcategories"]]
        expected += self.taxonomy["binary_features"]
        if set(expected) != set(self.bundle["label_names"]):
            raise ValueError("taxonomy labels differ from model")

    def classify(self, text: str) -> dict:
        if self.input_guard:
            self.input_guard.check(text)
        sparse = hstack([
            self.bundle["word"].transform([text]),
            self.bundle["char"].transform([text]),
        ]).tocsr()
        scores = {name: float(self.bundle["models"][name].predict_proba(sparse)[0, 1])
                  for name in self.bundle["label_names"]}
        for name, parameters in self.bundle.get('calibration', {}).items():
            probability = np.clip(scores[name], 1e-7, 1 - 1e-7)
            logit = np.log(probability / (1 - probability))
            scores[name] = float(1 / (1 + np.exp(-np.clip(parameters['a'] * logit + parameters['b'], -50, 50))))
        complexity = float(self.bundle["complexity"].predict(sparse)[0]) if "complexity" in self.bundle else None
        return format_result(self.taxonomy, scores, complexity)
