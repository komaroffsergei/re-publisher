"""Train a private TF-IDF baseline from Codex-reviewed MAX text labels.

This script never assigns semantic labels. Raw posts and fitted artifacts stay
outside the repository. The blind test partition is rejected on input.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

import joblib
import numpy as np
from scipy.sparse import hstack
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.metrics import f1_score, mean_absolute_error, precision_recall_fscore_support
from sklearn.model_selection import GroupShuffleSplit


def read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def labels_from_taxonomy(taxonomy: dict) -> list[str]:
    result: list[str] = []
    for category in taxonomy["categories"]:
        result.append(category["id"])
        result.extend(child["id"] for child in category["subcategories"])
    result.extend(taxonomy["binary_features"])
    return result


def group_mapping(groups: list[dict]) -> dict[int, int]:
    parent: dict[int, int] = {}

    def find(value: int) -> int:
        parent.setdefault(value, value)
        if parent[value] != value:
            parent[value] = find(parent[value])
        return parent[value]

    for group in groups:
        members = [int(value) for value in group["candidate_ids"]]
        if members:
            root = find(members[0])
            for member in members[1:]:
                parent[find(member)] = root
    return {value: find(value) for value in parent}


def metrics(y_true: np.ndarray, probability: np.ndarray, threshold: float) -> dict:
    pred = probability >= threshold
    precision, recall, f1, _ = precision_recall_fscore_support(
        y_true, pred, average="binary", zero_division=0
    )
    return {
        "positive": int(y_true.sum()),
        "negative": int(len(y_true) - y_true.sum()),
        "precision": float(precision),
        "recall": float(recall),
        "f1": float(f1),
        "threshold": float(threshold),
        "brier": float(np.mean((probability - y_true) ** 2)),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("worklist", type=Path)
    parser.add_argument("labels", type=Path)
    parser.add_argument("taxonomy", type=Path)
    parser.add_argument("groups", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--seed", type=int, default=20260929)
    args = parser.parse_args()
    checkout = Path(__file__).resolve().parents[1]
    if args.output.resolve().is_relative_to(checkout):
        raise ValueError("model output must stay outside the Git checkout")
    worklist = read_jsonl(args.worklist)
    by_id = {int(row["id"]): row for row in worklist}
    all_labels = read_jsonl(args.labels)
    taxonomy = json.loads(args.taxonomy.read_text(encoding="utf-8"))
    label_names = labels_from_taxonomy(taxonomy)
    group_by_id = group_mapping(read_jsonl(args.groups))
    accepted: list[tuple[dict, dict]] = []
    rejected = Counter()
    for label in all_labels:
        row = by_id.get(int(label["id"]))
        if row is None:
            raise ValueError(f"unknown label id {label['id']}")
        if row["partition_hint"] != "development":
            raise ValueError("blind test label must not be used for model selection")
        if label["text_sha256"] != row["text_sha256"]:
            raise ValueError(f"text hash mismatch for {label['id']}")
        if label["taxonomy_version"] != taxonomy["version"]:
            raise ValueError(f"taxonomy version mismatch for {label['id']}")
        if label["needs_review"]:
            rejected["needs_review"] += 1
            continue
        accepted.append((row, label))
    ids = [int(row["id"]) for row, _ in accepted]
    texts = [row["text"] for row, _ in accepted]
    split_groups = [group_by_id.get(ident, ident) for ident in ids]
    train_indices, valid_indices = next(
        GroupShuffleSplit(n_splits=1, test_size=0.2, random_state=args.seed).split(
            texts, groups=split_groups
        )
    )
    train_indices = np.asarray(train_indices)
    valid_indices = np.asarray(valid_indices)
    split = {int(ids[index]): "validation" for index in valid_indices}
    split.update({int(ids[index]): "train" for index in train_indices})
    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output / "split.jsonl").open("w", encoding="utf-8") as handle:
        for ident in sorted(split):
            handle.write(json.dumps({"id": ident, "partition": split[ident]}) + "\n")

    word = TfidfVectorizer(
        ngram_range=(1, 2), min_df=2, max_features=100_000, sublinear_tf=True
    )
    char = TfidfVectorizer(
        analyzer="char", ngram_range=(3, 5), min_df=3,
        max_features=100_000, sublinear_tf=True
    )
    train_texts = [texts[index] for index in train_indices]
    valid_texts = [texts[index] for index in valid_indices]
    x_train = hstack([word.fit_transform(train_texts), char.fit_transform(train_texts)]).tocsr()
    x_valid = hstack([word.transform(valid_texts), char.transform(valid_texts)]).tocsr()
    models = {}
    report = {}
    for name in label_names:
        train_mask = np.array([name not in accepted[index][1]["unclear"] for index in train_indices])
        valid_mask = np.array([name not in accepted[index][1]["unclear"] for index in valid_indices])
        y_train = np.array(
            [int(name in accepted[index][1]["yes"]) for index in train_indices],
            dtype=np.int8,
        )
        y_valid = np.array(
            [int(name in accepted[index][1]["yes"]) for index in valid_indices],
            dtype=np.int8,
        )
        if not valid_mask.any() or len(set(y_train[train_mask])) != 2:
            report[name] = {"status": "insufficient_train_or_validation"}
            continue
        model = LogisticRegression(
            C=2.0, class_weight="balanced", max_iter=500, solver="liblinear",
            random_state=args.seed,
        )
        model.fit(x_train[train_mask], y_train[train_mask])
        probability = model.predict_proba(x_valid[valid_mask])[:, 1]
        truth = y_valid[valid_mask]
        thresholds = np.linspace(0.1, 0.9, 33)
        threshold = max(
            thresholds,
            key=lambda candidate: f1_score(truth, probability >= candidate, zero_division=0),
        )
        report[name] = metrics(truth, probability, threshold)
        report[name]["train_positive"] = int(y_train[train_mask].sum())
        report[name]["train_negative"] = int(train_mask.sum() - y_train[train_mask].sum())
        models[name] = model
    train_complexity = np.array(
        [accepted[index][1]["technical_complexity"] for index in train_indices],
        dtype=object,
    )
    valid_complexity = np.array(
        [accepted[index][1]["technical_complexity"] for index in valid_indices],
        dtype=object,
    )
    train_complexity_mask = np.array([isinstance(value, int) for value in train_complexity])
    valid_complexity_mask = np.array([isinstance(value, int) for value in valid_complexity])
    complexity = Ridge(alpha=10)
    complexity.fit(x_train[train_complexity_mask], train_complexity[train_complexity_mask].astype(float))
    complexity_pred = np.rint(
        np.clip(complexity.predict(x_valid[valid_complexity_mask]), 0, 5)
    ).astype(int)
    complexity_truth = valid_complexity[valid_complexity_mask].astype(int)
    joblib.dump(
        {"word": word, "char": char, "models": models, "complexity": complexity,
         "taxonomy_version": taxonomy["version"], "label_names": label_names},
        args.output / "baseline.joblib",
        compress=3,
    )
    summary = {
        "taxonomy_version": taxonomy["version"],
        "training_worklist_sha256": hashlib.sha256(args.worklist.read_bytes()).hexdigest(),
        "label_file_sha256": hashlib.sha256(args.labels.read_bytes()).hexdigest(),
        "seed": args.seed,
        "accepted": len(accepted),
        "rejected": dict(rejected),
        "train": len(train_indices),
        "validation": len(valid_indices),
        "label_metrics": report,
        "technical_complexity": {
            "validation_size": int(valid_complexity_mask.sum()),
            "mae": float(mean_absolute_error(complexity_truth, complexity_pred)),
            "exact": float(np.mean(complexity_pred == complexity_truth)),
            "within_one": float(np.mean(np.abs(complexity_pred - complexity_truth) <= 1)),
        },
    }
    (args.output / "validation.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "accepted": len(accepted), "train": len(train_indices),
        "validation": len(valid_indices), "rejected": dict(rejected),
        "supported_models": len(models), "weak_train_classes": {
            name: value.get("train_positive")
            for name, value in report.items()
            if value.get("train_positive", 0) < 20
        },
        "output": str(args.output),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
