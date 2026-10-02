"""Fine-tune a local multilingual MiniLM encoder on Codex-reviewed MAX posts.

Only development labels are accepted. Texts, labels, and model outputs must
stay in a protected directory outside the repository. This script never labels
posts and does not contact an inference API with post content.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from pathlib import Path

import numpy as np
import torch
from safetensors.torch import save_file
from sklearn.metrics import f1_score, mean_absolute_error
from torch import nn
from torch.utils.data import DataLoader, Dataset
from transformers import AutoModel, AutoTokenizer

from train_codex_baseline import labels_from_taxonomy, read_jsonl

CHECKPOINT = "MoritzLaurer/multilingual-MiniLMv2-L6-mnli-xnli"
REVISION = "0a71e92a985b6e1ad1828cf67ce9c459639c1dca"


class Posts(Dataset):
    def __init__(self, rows, tokenizer, names, max_length):
        self.rows = rows
        self.tokenizer = tokenizer
        self.names = names
        self.max_length = max_length

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, index):
        row, label = self.rows[index]
        encoded = self.tokenizer(
            row["text"], truncation=True, max_length=self.max_length,
        )
        targets = [float(name in label["yes"]) for name in self.names]
        mask = [float(name not in label["unclear"]) for name in self.names]
        complexity = label["technical_complexity"]
        return encoded, targets, mask, float(complexity) if isinstance(complexity, int) else -1.0


def collate(batch, tokenizer):
    encoded, targets, masks, complexity = zip(*batch)
    inputs = tokenizer.pad(encoded, return_tensors="pt")
    return inputs, torch.tensor(targets), torch.tensor(masks), torch.tensor(complexity)


class Classifier(nn.Module):
    def __init__(self, encoder, count):
        super().__init__()
        self.encoder = encoder
        self.dropout = nn.Dropout(0.1)
        self.labels = nn.Linear(encoder.config.hidden_size, count)
        self.complexity = nn.Linear(encoder.config.hidden_size, 1)

    def forward(self, inputs):
        hidden = self.encoder(**inputs).last_hidden_state
        attention = inputs["attention_mask"].unsqueeze(-1)
        pooled = (hidden * attention).sum(dim=1) / attention.sum(dim=1).clamp(min=1)
        pooled = self.dropout(pooled)
        return self.labels(pooled), self.complexity(pooled).squeeze(-1)


def evaluate(model, loader, names, device):
    model.eval()
    scores, truths, masks, comp_predictions, comp_truths = [], [], [], [], []
    with torch.no_grad():
        for inputs, targets, mask, complexity in loader:
            inputs = {key: value.to(device) for key, value in inputs.items()}
            logits, comp = model(inputs)
            scores.append(torch.sigmoid(logits).cpu().numpy())
            truths.append(targets.numpy())
            masks.append(mask.numpy())
            comp_predictions.extend(comp.cpu().numpy().tolist())
            comp_truths.extend(complexity.numpy().tolist())
    scores = np.concatenate(scores)
    truths = np.concatenate(truths)
    masks = np.concatenate(masks)
    result = {}
    for index, name in enumerate(names):
        valid = masks[:, index].astype(bool)
        truth = truths[valid, index]
        probability = scores[valid, index]
        thresholds = np.linspace(0.1, 0.9, 33)
        threshold = max(
            thresholds,
            key=lambda value: f1_score(truth, probability >= value, zero_division=0),
        )
        result[name] = {
            "positive": int(truth.sum()), "negative": int(len(truth) - truth.sum()),
            "f1": float(f1_score(truth, probability >= threshold, zero_division=0)),
            "threshold": float(threshold),
            "brier": float(np.mean((probability - truth) ** 2)),
        }
    comp_mask = np.array(comp_truths) >= 0
    true = np.array(comp_truths)[comp_mask]
    pred = np.rint(np.clip(np.array(comp_predictions)[comp_mask], 0, 5))
    return result, {
        "mae": float(mean_absolute_error(true, pred)),
        "exact": float(np.mean(true == pred)),
        "within_one": float(np.mean(np.abs(true - pred) <= 1)),
        "count": int(len(true)),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("worklist", type=Path)
    parser.add_argument("labels", type=Path)
    parser.add_argument("taxonomy", type=Path)
    parser.add_argument("split", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--epochs", type=int, default=2)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--max-length", type=int, default=256)
    parser.add_argument("--learning-rate", type=float, default=2e-5)
    parser.add_argument("--seed", type=int, default=20260929)
    args = parser.parse_args()
    if args.output.resolve().is_relative_to(Path(__file__).resolve().parents[1]):
        raise ValueError("output must stay outside Git checkout")
    if args.epochs < 1 or args.batch_size < 1:
        raise ValueError("epochs and batch size must be positive")
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.set_num_threads(min(4, torch.get_num_threads()))

    by_id = {int(row["id"]): row for row in read_jsonl(args.worklist)}
    labels = {int(row["id"]): row for row in read_jsonl(args.labels)}
    split = {int(row["id"]): row["partition"] for row in read_jsonl(args.split)}
    taxonomy = json.loads(args.taxonomy.read_text(encoding="utf-8"))
    names = labels_from_taxonomy(taxonomy)
    parts = {"train": [], "validation": []}
    for ident, partition in split.items():
        if partition not in parts:
            raise ValueError(f"unexpected split {partition}")
        row, label = by_id[ident], labels[ident]
        if row["partition_hint"] != "development" or label["needs_review"]:
            raise ValueError("blind or disputed label entered train/validation split")
        if row["text_sha256"] != label["text_sha256"]:
            raise ValueError(f"text mismatch {ident}")
        if taxonomy["version"] != label["taxonomy_version"]:
            raise ValueError("taxonomy mismatch")
        parts[partition].append((row, label))
    tokenizer = AutoTokenizer.from_pretrained(CHECKPOINT, revision=REVISION)
    train = Posts(parts["train"], tokenizer, names, args.max_length)
    valid = Posts(parts["validation"], tokenizer, names, args.max_length)
    train_loader = DataLoader(
        train, batch_size=args.batch_size, shuffle=True,
        collate_fn=lambda batch: collate(batch, tokenizer),
    )
    valid_loader = DataLoader(
        valid, batch_size=args.batch_size, shuffle=False,
        collate_fn=lambda batch: collate(batch, tokenizer),
    )
    encoder = AutoModel.from_pretrained(CHECKPOINT, revision=REVISION)
    model = Classifier(encoder, len(names))
    device = torch.device("cpu")
    model.to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.learning_rate)
    train_values = np.array([[float(name in label["yes"]) for name in names] for _, label in parts["train"]])
    train_masks = np.array([[float(name not in label["unclear"]) for name in names] for _, label in parts["train"]])
    positive = (train_values * train_masks).sum(axis=0)
    negative = ((1 - train_values) * train_masks).sum(axis=0)
    weights = torch.tensor(np.minimum(negative / np.maximum(positive, 1), 8), dtype=torch.float)
    criterion = nn.BCEWithLogitsLoss(reduction="none", pos_weight=weights)
    args.output.mkdir(parents=True, exist_ok=True)
    history = []
    for epoch in range(args.epochs):
        model.train()
        losses = []
        for inputs, targets, mask, complexity in train_loader:
            inputs = {key: value.to(device) for key, value in inputs.items()}
            optimizer.zero_grad(set_to_none=True)
            logits, comp = model(inputs)
            semantic = (criterion(logits, targets) * mask).sum() / mask.sum().clamp(min=1)
            comp_mask = complexity >= 0
            ordered = nn.functional.smooth_l1_loss(comp[comp_mask], complexity[comp_mask])
            loss = semantic + 0.15 * ordered
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            losses.append(float(loss.detach()))
        metrics, complexity_metrics = evaluate(model, valid_loader, names, device)
        summary = {
            "epoch": epoch + 1, "train_loss": float(np.mean(losses)),
            "macro_f1": float(np.mean([value["f1"] for value in metrics.values()])),
            "complexity": complexity_metrics,
        }
        history.append(summary)
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        save_file({key: value.detach().cpu().contiguous() for key, value in model.state_dict().items()},
                  str(args.output / f"epoch-{epoch + 1}.safetensors"))
        (args.output / f"validation-epoch-{epoch + 1}.json").write_text(
            json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tokenizer.save_pretrained(args.output / "tokenizer")
    (args.output / "training.json").write_text(json.dumps({
        "checkpoint": CHECKPOINT, "revision": REVISION, "taxonomy_version": taxonomy["version"],
        "worklist_sha256": hashlib.sha256(args.worklist.read_bytes()).hexdigest(),
        "labels_sha256": hashlib.sha256(args.labels.read_bytes()).hexdigest(),
        "split_sha256": hashlib.sha256(args.split.read_bytes()).hexdigest(),
        "names": names, "train": len(train), "validation": len(valid),
        "batch_size": args.batch_size, "max_length": args.max_length,
        "learning_rate": args.learning_rate, "seed": args.seed,
        "torch_version": torch.__version__, "history": history,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
