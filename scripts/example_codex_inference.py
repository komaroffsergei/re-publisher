"""Export private research-only response examples without source post text."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import torch
from safetensors.torch import load_file
from transformers import AutoModel, AutoTokenizer

from train_codex_baseline import labels_from_taxonomy, read_jsonl
from train_codex_minilm import CHECKPOINT, REVISION, Classifier


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("worklist", type=Path)
    parser.add_argument("taxonomy", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.output.resolve().is_relative_to(Path(__file__).resolve().parents[1]):
        raise ValueError("private examples must stay outside Git")
    taxonomy = json.loads(args.taxonomy.read_text(encoding="utf-8"))
    names = labels_from_taxonomy(taxonomy)
    by_id = {row["id"]: row for row in read_jsonl(args.worklist)}
    tokenizer = AutoTokenizer.from_pretrained(args.candidate / "tokenizer", local_files_only=True)
    encoder = AutoModel.from_pretrained(CHECKPOINT, revision=REVISION, local_files_only=True)
    model = Classifier(encoder, len(names))
    model.load_state_dict(load_file(str(args.candidate / "epoch-2.safetensors")))
    model.eval()
    torch.set_num_threads(min(4, torch.get_num_threads()))
    examples = []
    with torch.no_grad():
        for ident in (2709, 2805):
            row = by_id[ident]
            encoded = tokenizer(row["text"], truncation=True, max_length=256, return_tensors="pt")
            logits, complexity = model(encoded)
            scores = dict(zip(names, torch.sigmoid(logits)[0].tolist()))
            broad = sorted(taxonomy["categories"], key=lambda item: scores[item["id"]], reverse=True)[:3]
            examples.append({
                "internal_sample_id": ident,
                "media": bool(row["has_media_any"]),
                "status": "research_only_needs_review",
                "top_3": [{"id": item["id"], "raw_score": round(scores[item["id"]], 4),
                           "subcategories": [{"id": child["id"], "raw_score": round(scores[child["id"]], 4)}
                                             for child in item["subcategories"]]}
                          for item in broad],
                "features": {name: round(scores[name], 4) for name in taxonomy["binary_features"]},
                "technical_complexity_raw": round(float(complexity[0]), 3),
                "technical_complexity_rounded": round(max(0, min(5, float(complexity[0])))),
            })
    examples.append({"internal_sample_id": "media-only-contract", "media": True,
                     "status": "no_text_for_semantic_classification", "top_3": [],
                     "features": None, "technical_complexity_raw": None})
    payload = {"taxonomy_version": taxonomy["version"], "model": "minilm-v1/epoch-2",
               "not_released": True, "not_calibrated": True,
               "note": "Raw model scores, not established probability or accuracy. Source text omitted.",
               "examples": examples}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
