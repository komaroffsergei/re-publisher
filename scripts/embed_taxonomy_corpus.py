"""Embed private MAX texts locally for sampling and near-duplicate inspection.

No posts are sent to Hugging Face: only public model weights are downloaded.
Embedding vectors and cluster IDs are not classification labels.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from taxonomy_label_batches import read_jsonl


MODEL_ID = 'intfloat/multilingual-e5-small'


def embed(worklist: Path, output: Path, batch_size: int, threads: int, max_length: int):
    if output.resolve().is_relative_to(Path.cwd().resolve()):
        raise ValueError('Embeddings must be outside the Git checkout')

    import numpy as np
    import torch
    import torch.nn.functional as functional
    from huggingface_hub import model_info
    from transformers import AutoModel, AutoTokenizer

    torch.set_num_threads(threads)
    revision = model_info(MODEL_ID).sha
    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID, revision=revision)
    model = AutoModel.from_pretrained(MODEL_ID, revision=revision).eval()
    records = list(read_jsonl(worklist))
    # Length bucketing saves padding work on CPU without changing row order in
    # the stored vectors. Short comments are retained for similarity review.
    order = sorted(range(len(records)), key=lambda i: len(records[i]['text']))
    vectors = np.empty((len(records), 384), dtype=np.float32)
    with torch.inference_mode():
        for offset in range(0, len(order), batch_size):
            indexes = order[offset:offset + batch_size]
            texts = ['query: ' + records[i]['text'] for i in indexes]
            batch = tokenizer(texts, max_length=max_length, padding=True, truncation=True, return_tensors='pt')
            hidden = model(**batch).last_hidden_state
            mask = batch['attention_mask'].unsqueeze(-1)
            pooled = (hidden * mask).sum(dim=1) / mask.sum(dim=1).clamp(min=1)
            normalized = functional.normalize(pooled, p=2, dim=1).numpy()
            vectors[indexes] = normalized
            if offset % (batch_size * 20) == 0:
                print(json.dumps({'embedded': min(offset + len(indexes), len(order)), 'total': len(order)}), flush=True)
    np.savez_compressed(output, ids=np.asarray([r['id'] for r in records], dtype=np.int64), vectors=vectors)
    manifest = {
        'worklist': worklist.name,
        'vectors': output.name,
        'model_id': MODEL_ID,
        'model_revision': revision,
        'prefix': 'query: ',
        'max_length': max_length,
        'threads': threads,
        'batch_size': batch_size,
        'count': len(records),
        'purpose': 'sampling and duplicate inspection only; not labels',
    }
    output.with_suffix('.manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(manifest))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('worklist', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--batch-size', type=int, default=16)
    parser.add_argument('--threads', type=int, default=4)
    parser.add_argument('--max-length', type=int, default=256)
    args = parser.parse_args()
    if min(args.batch_size, args.threads, args.max_length) <= 0:
        parser.error('Batch size, threads, and max length must be positive')
    embed(args.worklist, args.output, args.batch_size, args.threads, args.max_length)


if __name__ == '__main__':
    main()
