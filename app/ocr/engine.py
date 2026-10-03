"""Один контракт OCR для подготовки корпуса и production inference.

Исходный пост не переписывается. Надписи и оценки чтения сохраняются отдельно;
оценка OCR говорит о распознавании букв, а не о вероятности юмора.
"""
from __future__ import annotations

import hashlib
import json
import time
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

CONTRACT = "caption_ocr_v1"
MAX_PIXELS = 16_000_000
MIN_SCORE = 0.5
PREPROCESSING_VERSION = "bilingual-regions-v3"
QUALITY_POLICY = "all_blocks_min_0.5_v1"


def needs_review(result: dict) -> bool:
    """Даже слабая строка из одного символа не считается прочитанной.

    Блоки сохраняются целиком. Частично распознанное содержимое не
    подставляется вместо полного входа; policy общая для корпуса и VPS.
    """
    return result.get("status") == "needs_review" or any(
        block["score"] < MIN_SCORE for block in result.get("blocks", [])
    )


def engine_version(directory: str | Path):
    return "rapidocr-3.9.2@" + file_digest(Path(directory) / "ocr-manifest.json")[:16] + ":" + PREPROCESSING_VERSION


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for part in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(part)
    return digest.hexdigest()


@lru_cache(maxsize=1024)
def _media_digest(path: str, size: int, mtime_ns: int, ctime_ns: int, inode: int) -> str:
    # Загрузчик заменяет файлы атомарно. Статистика файла входит в ключ;
    # проверка доски не перечитывает большое видео каждые пять секунд.
    return file_digest(Path(path))


def media_digest(path: Path) -> str:
    resolved = path.resolve()
    stat = resolved.stat()
    return _media_digest(str(resolved), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)


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
        from importlib.metadata import version
        if version("rapidocr") != "3.9.2" or version("onnxruntime") != "1.30.0":
            raise ValueError("OCR package versions differ from the pinned runtime")

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
        from rapidocr.main import RapidOCRError
        from rapidocr.utils.process_img import map_boxes_to_original
        path = Path(path)
        started = time.perf_counter()
        with Image.open(path) as image:
            if image.width * image.height > MAX_PIXELS:
                raise ValueError("Image exceeds OCR pixel budget")
            image.seek(0)
            image = ImageOps.exif_transpose(image).convert("RGB")
            pixels = np.asarray(image)[:, :, ::-1].copy()
        # Детектор и поворот строк одинаковы для RU/EN. Повторный запуск
        # детектора раньше только тратил CPU. Сохраняем все найденные регионы.
        reader = self.readers[0]
        image, record = reader.preprocess_img(pixels)
        blocks = []
        try:
            cropped, detection = reader.detect_and_crop(image, record)
        except RapidOCRError:
            cropped = []
        if cropped:
            rotated, _ = reader.cls_and_rotate(cropped)
            ru = reader.recognize_txt(rotated)
            # В прежнем объединении кириллица всегда оставалась от RU.
            # EN запускается только для оставшихся регионов; это выбор
            # распознавания букв, а не определение темы или юмора.
            selected = [i for i, text in enumerate(ru.txts)
                        if not any("А" <= c <= "я" or c in "Ёё" for c in text)]
            english = {}
            if selected:
                result = self.readers[1].recognize_txt([rotated[i] for i in selected])
                english = {i: (text, float(score)) for i, text, score
                           in zip(selected, result.txts, result.scores, strict=True)}
            boxes = map_boxes_to_original(detection.boxes.copy(), record, *pixels.shape[:2])
            for i, (text, score, box) in enumerate(zip(ru.txts, ru.scores, boxes, strict=True)):
                if i in english and english[i][1] > float(score):
                    text, score = english[i]
                text = normalized(str(text))
                if text:
                    blocks.append({"text": text, "score": round(float(score), 5),
                                   "box": np.asarray(box).round(2).tolist()})
        blocks = reading_order(blocks)
        weak = [item for item in blocks if item["score"] < MIN_SCORE and len(item["text"]) >= 3]
        return {"status": "needs_review" if weak else "complete" if blocks else "no_text",
                "blocks": blocks, "text": "\n".join(item["text"] for item in blocks),
                "media_sha256": file_digest(path), "engine_version": self.version,
                "elapsed_ms": max(1, round((time.perf_counter() - started) * 1000))}
