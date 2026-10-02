"""Один постоянный ONNX-процесс. Родитель может завершить зависший OCR."""
import json
import sys
from app.config import get_settings
from app.ocr.engine import OcrEngine


def main():
    try:
        engine = OcrEngine(get_settings().ocr_model_dir)
    except Exception as exc:
        print(json.dumps({"error": "OCR initialization: " + type(exc).__name__}), flush=True)
        return
    for line in sys.stdin:
        try:
            value = json.loads(line)
            result = {"result": engine.read(value["path"])}
        except Exception as exc:
            result = {"error": type(exc).__name__}
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
