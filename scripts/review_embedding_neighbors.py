"""Show similar private texts for human/Codex review; never assign labels."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from taxonomy_label_batches import read_jsonl


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('worklist', type=Path)
    parser.add_argument('embeddings', type=Path)
    parser.add_argument('--seed-id', type=int, required=True)
    parser.add_argument('--count', type=int, default=30)
    parser.add_argument('--preview-chars', type=int, default=500)
    args = parser.parse_args()
    import numpy as np
    matrix = np.load(args.embeddings)
    ids = matrix['ids']
    vectors = matrix['vectors']
    match = np.flatnonzero(ids == args.seed_id)
    if len(match) != 1:
        raise ValueError('Seed ID missing or duplicated in embeddings')
    rows = {int(row['id']): row for row in read_jsonl(args.worklist)}
    scores = vectors @ vectors[int(match[0])]
    for index in np.argsort(-scores)[:args.count]:
        ident = int(ids[index])
        row = rows[ident]
        print(json.dumps({
            'id': ident,
            'similarity': round(float(scores[index]), 4),
            'text': row['text'][:args.preview_chars],
        }, ensure_ascii=False))


if __name__ == '__main__':
    main()
