"""Cluster local E5 vectors to select texts for reading, never to label them."""

from __future__ import annotations

import argparse
import json
import random
from collections import Counter
from pathlib import Path

from taxonomy_label_batches import read_jsonl


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('worklist', type=Path)
    parser.add_argument('embeddings', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--clusters', type=int, default=24)
    parser.add_argument('--seed', type=int, default=82731)
    parser.add_argument('--preview-chars', type=int, default=500)
    args = parser.parse_args()
    if args.output.resolve().is_relative_to(Path.cwd().resolve()):
        raise ValueError('Output must be outside the Git checkout')
    import numpy as np
    from sklearn.cluster import MiniBatchKMeans

    matrix = np.load(args.embeddings)
    ids = matrix['ids']
    vectors = matrix['vectors']
    if len(ids) != len(vectors):
        raise ValueError('ID/vector length mismatch')
    rows = {int(row['id']): row for row in read_jsonl(args.worklist)}
    model = MiniBatchKMeans(n_clusters=args.clusters, random_state=args.seed, batch_size=512, n_init=5)
    assignments = model.fit_predict(vectors)
    result = {
        'worklist': args.worklist.name,
        'embeddings': args.embeddings.name,
        'method': 'MiniBatchKMeans on local multilingual E5 vectors; sampling only, not labels',
        'seed': args.seed,
        'clusters': args.clusters,
        'assignments': {str(int(ident)): int(cluster) for ident, cluster in zip(ids, assignments)},
        'counts': dict(Counter(int(cluster) for cluster in assignments)),
    }
    args.output.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    rng = random.Random(args.seed)
    for cluster, count in sorted(Counter(assignments).items(), key=lambda item: -item[1]):
        indexes = np.flatnonzero(assignments == cluster)
        center = model.cluster_centers_[cluster]
        similarities = vectors[indexes] @ center
        representative = int(indexes[int(np.argmax(similarities))])
        random_index = int(rng.choice(list(indexes)))
        print(json.dumps({'cluster': int(cluster), 'size': int(count), 'samples': [
            {'id': int(ids[index]), 'text': rows[int(ids[index])]['text'][:args.preview_chars]}
            for index in dict.fromkeys([representative, random_index])
        ]}, ensure_ascii=False))


if __name__ == '__main__':
    main()
