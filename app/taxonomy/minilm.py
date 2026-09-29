"""Offline inference from the Codex-trained MiniLM checkpoint."""

from __future__ import annotations

import json
from pathlib import Path

import torch
from safetensors.torch import load_file
from torch import nn
from transformers import AutoConfig, AutoModel, AutoTokenizer

from app.taxonomy.inference import format_result


class Classifier(nn.Module):
    """Same pooling and heads as the frozen training script."""

    def __init__(self, encoder: nn.Module, count: int):
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


class MiniLmTaxonomyModel:
    def __init__(self, model_dir: str | Path):
        model_dir = Path(model_dir)
        candidate = model_dir / "minilm-v2"
        self.taxonomy = json.loads((model_dir / "taxonomy.json").read_text(encoding="utf-8"))
        training = json.loads((candidate / "training.json").read_text(encoding="utf-8"))
        expected = [category["id"] for category in self.taxonomy["categories"]]
        expected += [child["id"] for category in self.taxonomy["categories"] for child in category["subcategories"]]
        expected += self.taxonomy["binary_features"]
        if training["taxonomy_version"] != self.taxonomy["version"] or set(training["names"]) != set(expected):
            raise ValueError("MiniLM taxonomy labels differ from the checkpoint")
        torch.set_num_threads(1)
        self.names = training["names"]
        self.tokenizer = AutoTokenizer.from_pretrained(candidate / "tokenizer", local_files_only=True)
        config = AutoConfig.from_pretrained(candidate, local_files_only=True)
        self.model = Classifier(AutoModel.from_config(config), len(expected))
        self.model.load_state_dict(load_file(str(candidate / "epoch-2.safetensors")))
        self.model.eval()

    def classify(self, text: str) -> dict:
        with torch.inference_mode():
            encoded = self.tokenizer(text, truncation=True, max_length=256, return_tensors="pt")
            logits, complexity = self.model(encoded)
            scores = dict(zip(self.names, torch.sigmoid(logits)[0].tolist()))
            return format_result(self.taxonomy, scores, float(complexity[0]))
