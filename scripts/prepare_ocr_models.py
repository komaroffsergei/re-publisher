"""Скачать официальные веса для offline OCR; никакие посты не отправляются."""
import argparse
import hashlib
import json
from pathlib import Path

import requests
import yaml
import rapidocr


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    metadata = yaml.safe_load((Path(rapidocr.__file__).parent / "default_models.yaml").read_text(encoding="utf-8"))["onnxruntime"]
    selected = {"det": ("PP-OCRv5", "det", "ch_PP-OCRv5_det_mobile"),
                "cls": ("PP-OCRv4", "cls", "ch_ppocr_mobile_v2.0_cls_mobile"),
                "ru": ("PP-OCRv5", "rec", "cyrillic_PP-OCRv5_rec_mobile"),
                "en": ("PP-OCRv5", "rec", "en_PP-OCRv5_rec_mobile")}
    models = {}
    with requests.Session() as client:
        for key, (version, task, name) in selected.items():
            info = metadata[version][task][name]
            target = args.directory / (name + ".onnx")
            expected = info["SHA256"]
            if not target.exists() or hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                temporary = target.with_suffix(".part")
                try:
                    with client.get(info["model_dir"], timeout=120, stream=True) as response:
                        response.raise_for_status()
                        with temporary.open("wb") as output:
                            for chunk in response.iter_content(1024 * 1024):
                                output.write(chunk)
                    if hashlib.sha256(temporary.read_bytes()).hexdigest() != expected:
                        raise ValueError("Official OCR model checksum mismatch")
                    temporary.replace(target)
                finally:
                    temporary.unlink(missing_ok=True)
            models[key] = {"file": target.name, "sha256": expected, "source": info["model_dir"]}
            print(json.dumps({"downloaded": key, "bytes": target.stat().st_size}), flush=True)
    (args.directory / "ocr-manifest.json").write_text(json.dumps(
        {"rapidocr": "3.9.2", "onnxruntime": "1.30.0", "contract": "caption_ocr_v1", "models": models},
        ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
