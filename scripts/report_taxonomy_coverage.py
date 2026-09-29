"""Summarize private Codex labels without printing post text or chat identifiers.

The report is descriptive only. It never infers or assigns a semantic label.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def read_jsonl(path: Path):
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("worklist", type=Path)
    parser.add_argument("taxonomy", type=Path)
    parser.add_argument("labels", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    if args.output.resolve().is_relative_to(Path.cwd().resolve()):
        raise ValueError("private coverage report must stay outside the Git checkout")

    work = {int(row["id"]): row for row in read_jsonl(args.worklist)}
    taxonomy = json.loads(args.taxonomy.read_text(encoding="utf-8"))
    categories = taxonomy["categories"]
    semantic = [item["id"] for parent in categories for item in [parent, *parent["subcategories"]]]
    features = list(taxonomy["binary_features"])
    label_ids = semantic + features
    labels = list(read_jsonl(args.labels))
    label_row_ids = [int(row["id"]) for row in labels]
    if len(label_row_ids) != len(set(label_row_ids)):
        raise ValueError("duplicate label IDs")
    unknown_ids = set(label_row_ids) - work.keys()
    if unknown_ids:
        raise ValueError(f"{len(unknown_ids)} label IDs are absent from the worklist")
    for row in labels:
        source = work[int(row["id"])]
        if row.get("taxonomy_version") != taxonomy["version"]:
            raise ValueError(f"taxonomy version mismatch for {row['id']}")
        if row.get("text_sha256") != source["text_sha256"]:
            raise ValueError(f"text hash mismatch for {row['id']}")

    counts = {key: Counter() for key in label_ids}
    positive_chats = {key: set() for key in label_ids}
    technical = Counter()
    review = Counter()
    for row in labels:
        yes = set(row["yes"])
        unclear = set(row["unclear"])
        if row.get("unclear_semantic"):
            unclear.update(semantic)
        chats = work[int(row["id"])]["source_chats"]
        for key in label_ids:
            state = "yes" if key in yes else "unclear" if key in unclear else "no"
            counts[key][state] += 1
            if state == "yes":
                positive_chats[key].update(chats)
        technical[str(row["technical_complexity"])] += 1
        review["needs_review" if row["needs_review"] else "accepted"] += 1

    report = {
        "taxonomy_version": taxonomy["version"],
        "worklist_texts": len(work),
        "codex_labeled_texts": len(labels),
        "remaining_texts": len(work) - len(labels),
        "label_distribution_is_selected_pilot_not_population": True,
        "technical_complexity": dict(sorted(technical.items())),
        "review": dict(review),
        "labels": {
            key: {
                "yes": counts[key]["yes"],
                "no": counts[key]["no"],
                "unclear": counts[key]["unclear"],
                "positive_chat_count": len(positive_chats[key]),
            }
            for key in label_ids
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "worklist_texts": report["worklist_texts"],
        "codex_labeled_texts": report["codex_labeled_texts"],
        "remaining_texts": report["remaining_texts"],
        "classes_without_pilot_positives": [key for key, value in report["labels"].items() if not value["yes"]],
        "output": args.output.name,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
