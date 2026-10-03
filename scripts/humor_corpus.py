"""Подготовка и проверка ручной разметки. Скрипт не назначает смысловые метки.

Сырые данные, OCR и решения Codex передаются путями в защищённый каталог.
Точные файлы связывают повторы; пустые подписи не служат ключом картинки.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from app.ocr.engine import compose_input, input_digest, needs_review, QUALITY_POLICY

VALUES = {"да", "нет", "неясно"}
NAMES = ("is_joke", "input_has_context")


def read_rows(paths):
    for path in paths:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                yield json.loads(line)


def decisions(paths):
    result = {}
    for row in read_rows(paths):
        if any(row.get(name) not in VALUES for name in NAMES) or not row.get("reason", "").strip():
            raise ValueError("Нужны две ручные метки и основание")
        if row.get("annotator") != "Codex /root":
            raise ValueError("Нужна разметка текущего Codex, не предсказание модели")
        key = row["peer"], row["message"], row["input_sha256"]
        # Перепроверка сохраняется отдельным файлом, последнее решение заменяет
        # текущее в dataset, а история файлов не удаляется.
        result[key] = row
    return result


def caption_hash(caption):
    value = " ".join((caption or "").split())
    # Короткие общие подписи вроде IT Memes не идентифицируют материал.
    return hashlib.sha256(value.encode()).hexdigest() if len(value) >= 80 else None


def assemble(ocr_paths, annotation_paths, known_album_members=None, reserved_sources=(), reserved_captions=()):
    raw = {}
    for row in read_rows(ocr_paths):
        raw[row["peer"], row["message"]] = row
    groups = defaultdict(list)
    for row in raw.values():
        key = (row["peer"], row.get("grouped_id") or f"message:{row['message']}")
        groups[key].append(row)
    labels, output, excluded = decisions(annotation_paths), [], Counter()
    for group, items in groups.items():
        items.sort(key=lambda r: r["message"])
        primary = next((r for r in items if r["caption"].strip()), items[0])
        expected = set((known_album_members or {}).get(group, ()))
        present = {r["message"] for r in items}
        complete_album = not primary.get("grouped_id") or bool(expected) and expected == present
        # В новом corpus старые контрольные посты не используются даже как
        # train-копии с другой подписью. Список хранится отдельно от материалов.
        if any((r["peer"], r["message"]) in reserved_sources for r in items):
            excluded["reserved_source"] += 1
            continue
        if caption_hash(primary["caption"]) in reserved_captions:
            excluded["reserved_caption_copy"] += 1
            continue
        # Production использует длинную подпись без OCR. Не обучаем на
        # надписях, которые этот же профиль не увидит на сайте.
        results = [] if len(primary["caption"].strip()) > 500 else [r["ocr"] for r in items]
        text = compose_input(primary["caption"], results)
        fingerprint = input_digest(primary["caption"], results)
        annotation = labels.get((primary["peer"], primary["message"], fingerprint))
        if not annotation:
            excluded["not_read_or_changed_input"] += 1
            continue
        sha = hashlib.sha256(json.dumps({"caption": primary["caption"].strip(),
            "media": [r.get("media_sha256") for r in items], "text": text}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        output.append({"sha": sha, "input_sha256": fingerprint, "caption": primary["caption"], "text": text,
            "sources": [{"peer": r["peer"], "message": r["message"], "date": r["date"]} for r in items],
            "media_sha256": [r.get("media_sha256") for r in items], "ocr_version": items[0]["ocr"].get("engine_version"),
            "labels": {name: annotation[name] for name in NAMES}, "reason": annotation["reason"],
            "provenance": "codex_agent", "complete_album": complete_album,
            "ocr_eligible": complete_album and all(r["ocr"].get("status") in {"complete", "no_text"}
                                                   and r.get("media_sha256") for r in items)
                            and not any(needs_review(result) for result in results),
            "caption_length": len(primary["caption"].strip())})
    return output, dict(excluded)


def partition(rows, manual_repeat_groups=()):
    """Группировка не переносит метки. Близкие пересказы связывает Codex."""
    parent = {row["sha"]: row["sha"] for row in rows}
    def root(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key
    def link(a, b):
        a, b = root(a), root(b)
        parent[max(a, b)] = min(a, b)
    media_seen = {}
    for row in rows:
        for media in row["media_sha256"]:
            if not media:
                continue
            if media in media_seen:
                link(row["sha"], media_seen[media])
            media_seen[media] = row["sha"]
    for group in manual_repeat_groups:
        members = group["members"]
        if len(set(members)) < 2 or any(m not in parent for m in members) or not group.get("reason"):
            raise ValueError("Некорректная группа повторов")
        for member in members[1:]:
            link(members[0], member)
    for row in rows:
        group = root(row["sha"])
        bucket = int(hashlib.sha256(("humor-ocr-v1:" + group).encode()).hexdigest()[:8], 16) % 100
        row.update(group_id=group, split="train" if bucket < 80 else "validation" if bucket < 90 else "test")
    return rows


def coverage(rows):
    counts = {part: {"positive": 0, "negative": 0, "context_yes": 0, "context_no": 0} for part in ("train", "validation", "test")}
    seen = set()
    for row in rows:
        key = row["split"], row["group_id"]
        if key in seen or not row["ocr_eligible"] or row.get("tokens", 513) > 512:
            continue
        seen.add(key)
        labels, count = row["labels"], counts[row["split"]]
        if labels["input_has_context"] == "да":
            count["context_yes"] += 1
            if labels["is_joke"] == "да": count["positive"] += 1
            elif labels["is_joke"] == "нет": count["negative"] += 1
        elif labels["input_has_context"] == "нет": count["context_no"] += 1
    return counts


def freeze(rows, directory, tokenizer_sha256=None):
    counts = coverage(rows)
    quotas = {"train": (1100, 1000), "validation": (150, 150), "test": (150, 150)}
    if any(counts[p]["positive"] < pos or counts[p]["negative"] < neg for p, (pos, neg) in quotas.items()):
        raise ValueError("Квоты ручной разметки пока не выполнены")
    groups = defaultdict(set)
    for row in rows:
        groups[row["group_id"]].add(row["split"])
    if any(len(parts) != 1 for parts in groups.values()):
        raise ValueError("Повторы пересекают выборки")
    known_labels = defaultdict(set)
    for row in rows:
        if row["labels"]["input_has_context"] == "да" and row["labels"]["is_joke"] in {"да", "нет"}:
            known_labels[row["group_id"]].add(row["labels"]["is_joke"])
    if any(len(values) > 1 for values in known_labels.values()):
        raise ValueError("Противоречивую группу повторов должен перепроверить Codex")
    if not all(isinstance(row.get("tokens"), int) for row in rows):
        raise ValueError("Сначала проверьте полный вход закреплённым tokenizer")
    directory.mkdir(parents=True, exist_ok=False)
    content = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows).encode()
    (directory / "dataset.jsonl").write_bytes(content)
    manifest = {"profile": "humor_ocr", "contract": "caption_ocr_v1", "dataset_sha256": hashlib.sha256(content).hexdigest(),
                "rows": len(rows), "coverage": counts, "labels": list(NAMES), "source": "Codex personal annotation",
                "tokenizer_sha256":tokenizer_sha256, "ocr_quality_policy":QUALITY_POLICY,
                "ocr_versions":sorted({r["ocr_version"] for r in rows if r["ocr_eligible"] and r.get("ocr_version")})}
    (directory / "dataset-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("directory", type=Path)
    p.add_argument("--freeze", type=Path)
    args = p.parse_args()
    policy_path = args.directory / "corpus-policy.json"
    if not policy_path.is_file():
        raise ValueError("Сначала подготовьте границы старых holdout и состав альбомов")
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    albums = {(r["peer"], r["grouped_id"]): r["members"] for r in policy["albums"]}
    reserved = {tuple(r) for r in policy["reserved_sources"]}
    ocr_directory = args.directory / policy.get("ocr_directory", ".")
    paths = [p for p in sorted(ocr_directory.glob("ocr-*.jsonl")) if re.fullmatch(r"ocr--?\d+-\d{3}\.jsonl", p.name)]
    rows, excluded = assemble(paths, sorted(args.directory.glob("codex-annotations-*.jsonl")), albums, reserved,
                             set(policy.get("reserved_caption_sha256", [])))
    rows = partition(rows, list(read_rows([args.directory / "codex-repeat-groups.jsonl"])) if (args.directory / "codex-repeat-groups.jsonl").exists() else [])
    from tokenizers import Tokenizer
    tokenizer_path = Path(policy["tokenizer_path"])
    if hashlib.sha256(tokenizer_path.read_bytes()).hexdigest() != policy["tokenizer_sha256"]:
        raise ValueError("Tokenizer изменился")
    tokenizer = Tokenizer.from_file(str(tokenizer_path)); tokenizer.no_truncation(); tokenizer.no_padding()
    for row in rows:
        row["tokens"] = len(tokenizer.encode(row["text"]).ids)
    result = {"rows": len(rows), "coverage": coverage(rows), "excluded": excluded}
    if args.freeze: result["frozen"] = freeze(rows, args.freeze, policy["tokenizer_sha256"])
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__": main()
