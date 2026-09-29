"""Explore a private MAX snapshot without creating classification labels.

TF-IDF groups only help select texts for Codex to read. Cluster IDs and terms
must never be used as labels or training targets.
"""

from __future__ import annotations

import argparse
import collections
import gzip
import json
import random
import re
from pathlib import Path


def normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text.casefold()).strip()


def load_rows(snapshot: Path) -> list[dict]:
    with gzip.open(snapshot, "rt", encoding="utf-8") as source:
        return [json.loads(line) for line in source if line.strip()]


def unique_texts(rows: list[dict]) -> tuple[list[dict], dict[int, list[int]]]:
    groups: dict[str, list[dict]] = collections.defaultdict(list)
    for row in rows:
        key = normalized(row.get("text") or "")
        if key:
            groups[key].append(row)
    unique = [group[0] for group in groups.values()]
    duplicates = {group[0]["id"]: [row["id"] for row in group] for group in groups.values()}
    return unique, duplicates


def cluster(snapshot: Path, output: Path, clusters: int, seed: int) -> None:
    from sklearn.cluster import MiniBatchKMeans
    from sklearn.decomposition import TruncatedSVD
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.preprocessing import Normalizer

    rows, duplicates = unique_texts(load_rows(snapshot))
    texts = [row["text"] for row in rows]
    matrix = TfidfVectorizer(
        lowercase=True, strip_accents="unicode", ngram_range=(1, 2),
        min_df=3, max_df=0.9, max_features=30_000, sublinear_tf=True,
    ).fit_transform(texts)
    reduced = Normalizer(copy=False).fit_transform(
        TruncatedSVD(n_components=100, random_state=seed).fit_transform(matrix)
    )
    model = MiniBatchKMeans(n_clusters=clusters, random_state=seed, batch_size=512, n_init=5)
    labels = model.fit_predict(reduced)
    result = {
        "snapshot": snapshot.name,
        "method": "TF-IDF + SVD + MiniBatchKMeans; sampling only, not labels",
        "seed": seed,
        "clusters": clusters,
        "posts": len(rows),
        "duplicate_ids": duplicates,
        "assignments": {str(row["id"]): int(label) for row, label in zip(rows, labels)},
    }
    output.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    grouped: dict[int, list[dict]] = collections.defaultdict(list)
    for row, label in zip(rows, labels):
        grouped[int(label)].append(row)
    rng = random.Random(seed)
    for label, group in sorted(grouped.items(), key=lambda item: -len(item[1])):
        print(f"CLUSTER {label} unique_texts={len(group)}")
        for row in rng.sample(group, min(3, len(group))):
            print(json.dumps({"id": row["id"], "text": (row["text"] or "")[:650]}, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("output", type=Path, help="File outside Git, next to the private snapshot")
    parser.add_argument("--clusters", type=int, default=20)
    parser.add_argument("--seed", type=int, default=82731)
    args = parser.parse_args()
    if args.output.resolve().is_relative_to(Path.cwd().resolve()):
        raise SystemExit("cluster manifest must be outside the Git checkout")
    cluster(args.snapshot, args.output, args.clusters, args.seed)


if __name__ == "__main__":
    main()
