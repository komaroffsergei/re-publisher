"""Один контракт OCR для подготовки корпуса и production inference.

Исходный пост не переписывается. Надписи и оценки чтения сохраняются отдельно;
оценка OCR говорит о распознавании букв, а не о вероятности юмора.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

CONTRACT = "caption_ocr_v1"
MAX_PIXELS = 16_000_000
MIN_SCORE = 0.5
PREPROCESSING_VERSION = "bilingual-lines-v2"


def engine_version(directory: str | Path):
    return "rapidocr-3.9.2@" + file_digest(Path(directory) / "ocr-manifest.json")[:16] + ":" + PREPROCESSING_VERSION


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for part in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(part)
    return digest.hexdigest()


def normalized(value: str) -> str:
    return " ".join(value.split())


def compose_input(caption: str | None, items: list[dict]) -> str:
    """Стабильный порядок строк; пустая подпись не объединяет разные картинки."""
    parts = [f"[Подпись]\n{(caption or '').strip()}"]
    for number, item in enumerate(items, 1):
        text = "\n".join(block["text"] for block in item.get("blocks", []))
        if text:
            parts.append(f"[Медиа {number}]\n{text}")
    return "\n\n".join(parts) if (caption or "").strip() or any(i.get("blocks") for i in items) else ""


def input_digest(caption: str | None, items: list[dict]) -> str:
    data = {"contract": CONTRACT, "caption": (caption or "").strip(), "media": [
        {key: item.get(key) for key in ("media_sha256", "engine_version", "status", "blocks")}
        for item in items]}
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def same_box(left, right) -> bool:
    a, b = np.asarray(left), np.asarray(right)
    amin, amax = a.min(axis=0), a.max(axis=0)
    bmin, bmax = b.min(axis=0), b.max(axis=0)
    intersection = float(np.prod(np.maximum(0, np.minimum(amax, bmax) - np.maximum(amin, bmin))))
    area_a, area_b = float(np.prod(amax - amin)), float(np.prod(bmax - bmin))
    return intersection / max(1, area_a + area_b - intersection) > .75


def reading_order(blocks: list[dict]) -> list[dict]:
    """Небольшой наклон строки не переставляет слова справа налево."""
    lines: list[list[dict]] = []
    for block in sorted(blocks, key=lambda b: min(p[1] for p in b["box"])):
        ys = [p[1] for p in block["box"]]
        center, height = (min(ys) + max(ys)) / 2, max(ys) - min(ys)
        line = next((line for line in lines if abs(center - np.median([
            (min(p[1] for p in b["box"]) + max(p[1] for p in b["box"])) / 2 for b in line
        ])) <= .45 * min(height, np.median([
            max(p[1] for p in b["box"]) - min(p[1] for p in b["box"]) for b in line
        ]))), None)
        if line is None:
            lines.append([block])
        else:
            line.append(block)
    return [b for line in lines for b in sorted(line, key=lambda b: min(p[0] for p in b["box"]))]


class OcrEngine:
    def __init__(self, directory: str | Path):
        # Импортируется только в OCR-процессе; web не загружает ONNX-модели.
        from rapidocr import EngineType, LangDet, LangRec, ModelType, OCRVersion, RapidOCR

        directory = Path(directory).resolve()
        manifest_path = directory / "ocr-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.version = engine_version(directory)
        paths = {}
        for key, model in manifest["models"].items():
            path = (directory / model["file"]).resolve()
            if not path.is_relative_to(directory) or file_digest(path) != model["sha256"]:
                raise ValueError("OCR model checksum differs from manifest")
            paths[key] = str(path)
        shared = {
            "Global.text_score": 0.0, "Global.max_side_len": 1600,
            "Global.log_level": "error", "Global.model_root_dir": str(directory),
            "EngineConfig.onnxruntime.intra_op_num_threads": 1,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
            "EngineConfig.onnxruntime.use_cuda": False,
            "Det.engine_type": EngineType.ONNXRUNTIME, "Det.model_path": paths["det"],
            "Det.lang_type": LangDet.CH, "Det.model_type": ModelType.MOBILE,
            "Det.ocr_version": OCRVersion.PPOCRV5,
            "Cls.engine_type": EngineType.ONNXRUNTIME, "Cls.model_path": paths["cls"],
            "Rec.engine_type": EngineType.ONNXRUNTIME, "Rec.model_type": ModelType.MOBILE,
            "Rec.ocr_version": OCRVersion.PPOCRV5,
        }
        self.readers = [RapidOCR(params={**shared, "Rec.model_path": paths[key], "Rec.lang_type": language})
                        for key, language in (("ru", LangRec.CYRILLIC), ("en", LangRec.EN))]

    def read(self, path: str | Path) -> dict:
        path = Path(path)
        started = time.perf_counter()
        with Image.open(path) as image:
            if image.width * image.height > MAX_PIXELS:
                raise ValueError("Image exceeds OCR pixel budget")
            image.seek(0)
            image = ImageOps.exif_transpose(image).convert("RGB")
            # RapidOCR принимает OpenCV-массив BGR; Pillow выдаёт RGB.
            pixels = np.asarray(image)[:, :, ::-1].copy()
        blocks = []
        for reader in self.readers:
            result = reader(pixels)
            if result.txts is None:
                continue
            for text, score, box in zip(result.txts, result.scores, result.boxes, strict=True):
                text = normalized(str(text))
                if not text:
                    continue
                candidate = {"text": text, "score": round(float(score), 5),
                             "box": np.asarray(box).round(2).tolist()}
                existing = next((item for item in blocks if same_box(item["box"], candidate["box"])), None)
                if existing is None:
                    blocks.append(candidate)
                # Английский recognizer иногда уверенно читает «не» как He.
                # Кириллицу оставляем от соответствующего recognizer; это
                # выбор чтения букв, а не тематическая разметка поста.
                elif not any("А" <= c <= "я" or c in "Ёё" for c in existing["text"]) and candidate["score"] > existing["score"]:
                    existing.update(candidate)
        blocks = reading_order(blocks)
        weak = [item for item in blocks if item["score"] < MIN_SCORE and len(item["text"]) >= 3]
        return {"status": "needs_review" if weak else "complete" if blocks else "no_text",
                "blocks": blocks, "text": "\n".join(item["text"] for item in blocks),
                "media_sha256": file_digest(path), "engine_version": self.version,
                "elapsed_ms": max(1, round((time.perf_counter() - started) * 1000))}
