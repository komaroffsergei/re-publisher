"""Aggregate broad-category test confusions without exporting MAX text or IDs."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path


def read_jsonl(path: Path):
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("labels", type=Path)
    parser.add_argument("worklist", type=Path)
    parser.add_argument("taxonomy", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.output.resolve().is_relative_to(Path(__file__).resolve().parents[1]):
        raise ValueError("reports belong outside Git")
    broad = {item["id"] for item in json.loads(args.taxonomy.read_text(encoding="utf-8"))["categories"]}
    labels = {row["id"]: row for row in read_jsonl(args.labels)}
    test = [row for row in read_jsonl(args.worklist)
            if row["partition_hint"] == "blind_test_candidate" and not labels[row["id"]]["needs_review"]]
    report = json.loads(args.report.read_text(encoding="utf-8"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["model", "true_category", "extra_predicted_category", "posts"])
        for key in ("baseline", "minilm_epoch_2"):
            errors_by_id = {}
            for error in report[key]["errors"]:
                if error["label"] in broad:
                    errors_by_id.setdefault(error["id"], set()).add(error["label"])
            pairs = Counter()
            for row in test:
                actual = set(labels[row["id"]]["yes"]) & broad
                predicted = actual ^ errors_by_id.get(row["id"], set())
                for missing in actual - predicted:
                    for extra in predicted - actual:
                        pairs[missing, extra] += 1
            for (missing, extra), count in pairs.most_common():
                writer.writerow([key, missing, extra, count])
    print(args.output)


if __name__ == "__main__":
    main()
