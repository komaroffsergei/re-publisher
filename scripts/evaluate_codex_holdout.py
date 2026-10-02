"""Evaluate frozen local classifiers on the reserved Codex-labeled MAX set.

No text or label is sent to a remote inference API. Outputs stay outside Git.
Thresholds are read from development validation reports and are never tuned on
this blind test. Metrics express agreement with Codex, not human ground truth.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import time
from pathlib import Path

import joblib
import numpy as np
import torch
from safetensors.torch import load_file
from scipy.sparse import hstack
from sklearn.metrics import mean_absolute_error, precision_recall_fscore_support
from transformers import AutoModel, AutoTokenizer

from train_codex_baseline import labels_from_taxonomy, read_jsonl
from train_codex_minilm import CHECKPOINT, REVISION, Classifier


def working_set_bytes():
    """Current Windows working set; RSS-equivalent for this local CPU run."""
    if not hasattr(ctypes, "windll"):
        return None
    class Counters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong), ("page_fault_count", ctypes.c_ulong),
            ("peak_working_set", ctypes.c_size_t), ("working_set", ctypes.c_size_t),
            ("quota_peak_paged", ctypes.c_size_t), ("quota_paged", ctypes.c_size_t),
            ("quota_peak_nonpaged", ctypes.c_size_t), ("quota_nonpaged", ctypes.c_size_t),
            ("pagefile", ctypes.c_size_t), ("peak_pagefile", ctypes.c_size_t),
        ]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    kernel = ctypes.windll.kernel32
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    psapi = ctypes.windll.psapi
    psapi.GetProcessMemoryInfo.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong)
    psapi.GetProcessMemoryInfo.restype = ctypes.c_int
    current = kernel.GetCurrentProcess()
    if not psapi.GetProcessMemoryInfo(current, ctypes.byref(counters), counters.cb):
        return None
    return int(counters.working_set)


def ece(truth, scores, bins=10):
    value = 0.0
    groups = []
    for low, high in zip(np.linspace(0, 1, bins + 1)[:-1], np.linspace(0, 1, bins + 1)[1:]):
        chosen = (scores >= low) & (scores < high if high < 1 else scores <= high)
        if chosen.any():
            mean_score = float(scores[chosen].mean())
            observed = float(truth[chosen].mean())
            value += float(chosen.mean()) * abs(mean_score - observed)
            groups.append({"low": float(low), "high": float(high), "n": int(chosen.sum()),
                           "mean_score": mean_score, "observed": observed})
    return value, groups


def summarize(rows, names, broad, parents, scores, thresholds, complexity):
    labels = [label for _, label in rows]
    true = np.array([[int(name in label["yes"]) for name in names] for label in labels])
    clear = np.array([[int(name not in label["unclear"]) for name in names] for label in labels], dtype=bool)
    pred = scores >= np.array([thresholds[name] for name in names])
    summary = {}
    errors = []
    for col, name in enumerate(names):
        mask = clear[:, col]
        actual = true[mask, col]
        guessed = pred[mask, col]
        probability = scores[mask, col]
        precision, recall, f1, _ = precision_recall_fscore_support(
            actual, guessed, average="binary", zero_division=0)
        calibration, bins = ece(actual, probability)
        summary[name] = {
            "positive": int(actual.sum()), "negative": int(len(actual) - actual.sum()),
            "precision": float(precision), "recall": float(recall), "f1": float(f1),
            "threshold": float(thresholds[name]),
            "brier": float(np.mean((probability - actual) ** 2)),
            "ece_10": float(calibration), "calibration_bins": bins,
        }
        for local_idx in np.where(mask & (pred[:, col] != true[:, col]))[0]:
            errors.append({"id": int(rows[local_idx][0]["id"]), "label": name,
                           "true": int(true[local_idx, col]), "score": float(scores[local_idx, col]),
                           "threshold": float(thresholds[name])})
    broad_indices = [names.index(name) for name in broad]
    top3 = np.argsort(-scores[:, broad_indices], axis=1)[:, :3]
    top3_recall = []
    all_true_in_top3 = []
    for index in range(len(rows)):
        positives = {position for position, col in enumerate(broad_indices) if true[index, col]}
        if positives:
            chosen = set(top3[index].tolist())
            top3_recall.append(len(positives & chosen) / len(positives))
            all_true_in_top3.append(positives <= chosen)
    contradictions = 0
    for child, parent in parents.items():
        contradictions += int(np.sum(pred[:, names.index(child)] & ~pred[:, names.index(parent)]))
    contradictions += int(np.sum(pred[:, names.index("is_event_invitation")] & ~pred[:, names.index("is_event_related")]))
    comp_true = np.array([label["technical_complexity"] if isinstance(label["technical_complexity"], int) else -1
                          for label in labels])
    comp_mask = comp_true >= 0
    comp_pred = np.rint(np.clip(complexity[comp_mask], 0, 5))
    return {
        "count": len(rows), "label_metrics": summary,
        "micro_f1": float(precision_recall_fscore_support(true[clear], pred[clear], average="binary", zero_division=0)[2]),
        "macro_f1": float(np.mean([item["f1"] for item in summary.values()])),
        "broad_macro_f1": float(np.mean([summary[name]["f1"] for name in broad])),
        "top3_mean_recall": float(np.mean(top3_recall)),
        "top3_all_true_coverage": float(np.mean(all_true_in_top3)),
        "parent_child_and_event_contradictions": contradictions,
        "technical_complexity": {
            "n": int(comp_mask.sum()), "mae": float(mean_absolute_error(comp_true[comp_mask], comp_pred)),
            "exact": float(np.mean(comp_true[comp_mask] == comp_pred)),
            "within_one": float(np.mean(abs(comp_true[comp_mask] - comp_pred) <= 1)),
        },
        "errors": errors,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("worklist", type=Path)
    parser.add_argument("labels", type=Path)
    parser.add_argument("taxonomy", type=Path)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--expected-count", type=int, default=450)
    args = parser.parse_args()
    if args.output.resolve().is_relative_to(Path(__file__).resolve().parents[1]):
        raise ValueError("private result must remain outside Git")
    taxonomy = json.loads(args.taxonomy.read_text(encoding="utf-8"))
    names = labels_from_taxonomy(taxonomy)
    broad = [category["id"] for category in taxonomy["categories"]]
    parents = {child["id"]: category["id"] for category in taxonomy["categories"]
               for child in category["subcategories"]}
    by_id = {int(row["id"]): row for row in read_jsonl(args.worklist)}
    labels = read_jsonl(args.labels)
    rows = []
    for label in labels:
        row = by_id[int(label["id"])]
        if row["partition_hint"] != "blind_test_candidate":
            continue
        if row["text_sha256"] != label["text_sha256"] or label["taxonomy_version"] != taxonomy["version"]:
            raise ValueError(f"hash/version mismatch at {label['id']}")
        if not label["needs_review"]:
            rows.append((row, label))
    if len([1 for label in labels if by_id[int(label["id"])]["partition_hint"] == "blind_test_candidate"]) != args.expected_count:
        raise ValueError(f"all {args.expected_count} blind posts must be labeled")
    texts = [row["text"] for row, _ in rows]
    args.output.mkdir(parents=True, exist_ok=True)

    baseline = joblib.load(args.baseline / "baseline.joblib")
    baseline_val = json.loads((args.baseline / "validation.json").read_text(encoding="utf-8"))
    sparse = hstack([baseline["word"].transform(texts), baseline["char"].transform(texts)]).tocsr()
    baseline_scores = np.stack([baseline["models"][name].predict_proba(sparse)[:, 1] for name in names], axis=1)
    baseline_complexity = baseline["complexity"].predict(sparse)
    baseline_thresholds = {name: baseline_val["label_metrics"][name]["threshold"] for name in names}
    baseline_report = summarize(rows, names, broad, parents, baseline_scores, baseline_thresholds, baseline_complexity)
    before_model_memory = working_set_bytes()

    torch.set_num_threads(min(4, torch.get_num_threads()))
    tokenizer = AutoTokenizer.from_pretrained(args.candidate / "tokenizer", local_files_only=True)
    encoder = AutoModel.from_pretrained(CHECKPOINT, revision=REVISION)
    candidate = Classifier(encoder, len(names))
    candidate.load_state_dict(load_file(str(args.candidate / "epoch-2.safetensors")))
    candidate.eval()
    loaded_model_memory = working_set_bytes()
    max_length = int(json.loads((args.candidate / "training.json").read_text(encoding="utf-8"))["max_length"])
    candidate_val = json.loads((args.candidate / "validation-epoch-2.json").read_text(encoding="utf-8"))
    candidate_thresholds = {name: candidate_val[name]["threshold"] for name in names}
    candidate_scores, candidate_complexity = [], []
    elapsed = []
    with torch.no_grad():
        for text in texts:
            inputs = tokenizer(text, truncation=True, max_length=max_length, return_tensors="pt")
            start = time.perf_counter()
            logits, comp = candidate(inputs)
            elapsed.append(time.perf_counter() - start)
            candidate_scores.append(torch.sigmoid(logits).numpy()[0])
            candidate_complexity.append(float(comp.numpy()[0]))
    candidate_report = summarize(rows, names, broad, parents,
                                 np.array(candidate_scores), candidate_thresholds, np.array(candidate_complexity))
    short_elapsed = [seconds for text, seconds in zip(texts, elapsed) if len(text) <= 200]
    long_elapsed = [seconds for text, seconds in zip(texts, elapsed) if len(text) >= 1000]
    timing = {"p50_seconds": float(np.percentile(elapsed, 50)), "p95_seconds": float(np.percentile(elapsed, 95)),
              "max_seconds": float(max(elapsed)), "threads": torch.get_num_threads(),
              "short_chars_le_200": {"n": len(short_elapsed),
                                     "p50_seconds": float(np.percentile(short_elapsed, 50)),
                                     "p95_seconds": float(np.percentile(short_elapsed, 95))},
              "long_chars_ge_1000": {"n": len(long_elapsed),
                                      "p50_seconds": float(np.percentile(long_elapsed, 50)),
                                      "p95_seconds": float(np.percentile(long_elapsed, 95))},
              "working_set_before_model_bytes": before_model_memory,
              "working_set_after_load_bytes": loaded_model_memory,
              "working_set_after_inference_bytes": working_set_bytes()}
    report = {
        "taxonomy_version": taxonomy["version"], "test_labels_sha256": hashlib.sha256(args.labels.read_bytes()).hexdigest(),
        "test_total": args.expected_count, "test_accepted": len(rows), "test_needs_review": args.expected_count - len(rows),
        "baseline": baseline_report, "minilm_epoch_2": candidate_report,
        "minilm_cpu_inference": timing,
        "caveat": "Measures agreement with Codex labels, not independent human accuracy."
    }
    (args.output / "blind-test-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"test_total": args.expected_count, "accepted": len(rows),
                      "baseline_macro_f1": baseline_report["macro_f1"],
                      "candidate_macro_f1": candidate_report["macro_f1"],
                      "candidate_p95_seconds": timing["p95_seconds"],
                      "output": str(args.output)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
