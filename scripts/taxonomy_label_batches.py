"""Prepare Codex review batches and validate Codex-written labels.

This script never predicts or assigns semantic labels. The only shorthand in
label records is an explicit `all_other_labels: "нет"` declaration by Codex.
Private worklists and label files must be kept outside the Git checkout.
"""

from __future__ import annotations

import argparse
import collections
import gzip
import hashlib
import json
import re
import sys
from pathlib import Path


BINARY_FEATURES = (
    "is_ad", "is_event_related", "is_event_invitation", "is_job_vacancy",
    "is_scientific_paper", "is_joke",
)


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.casefold()).strip()


def read_jsonl(path: Path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def worklist(snapshot: Path, output: Path) -> None:
    if output.resolve().is_relative_to(Path.cwd().resolve()):
        raise ValueError("worklist must be outside the Git checkout")
    groups: dict[str, list[dict]] = collections.defaultdict(list)
    no_text: list[dict] = []
    for row in read_jsonl(snapshot):
        text = row.get("text") or ""
        key = normalize(text)
        if key:
            groups[key].append(row)
        else:
            no_text.append({"id": row["id"], "has_media": bool(row.get("media_type"))})
    records = []
    for group in groups.values():
        first = group[0]
        text = first["text"]
        records.append({
            "id": first["id"], "text": text,
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "source_ids": [row["id"] for row in group],
            "source_chats": sorted({row["chat_peer_id"] for row in group}),
            "first_posted_at": min(row["date"] for row in group),
            "last_posted_at": max(row["date"] for row in group),
            "has_media_any": any(bool(row.get("has_media") or row.get("media_type")) for row in group),
        })
    records.sort(key=lambda row: row["id"])
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    no_text_path = output.with_suffix(".no-text.json")
    no_text_path.write_text(json.dumps(no_text, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"texts_to_review": len(records), "no_text_posts": len(no_text), "worklist": output.name}))


def next_batch(worklist_path: Path, labels_path: Path, size: int, include_holdout: bool = False) -> None:
    done = {int(row["id"]) for row in read_jsonl(labels_path)} if labels_path.exists() else set()
    printed = 0
    for row in read_jsonl(worklist_path):
        if row["id"] in done:
            continue
        if not include_holdout and row.get("partition_hint") == "blind_test_candidate":
            continue
        # Metadata is deliberately hidden from Codex while assigning labels.
        print(json.dumps({"id": row["id"], "text": row["text"]}, ensure_ascii=False))
        printed += 1
        if printed >= size:
            break
    print(json.dumps({"batch_count": printed, "already_labeled": len(done)}, ensure_ascii=False))


def taxonomy_ids(taxonomy: dict) -> tuple[set[str], dict[str, str]]:
    categories: set[str] = set()
    parents: dict[str, str] = {}
    broad = taxonomy["categories"]
    if len(broad) > 8:
        raise ValueError("taxonomy has more than eight broad categories")
    for category in broad:
        parent_id = category["id"]
        if parent_id in categories:
            raise ValueError(f"duplicate category ID {parent_id}")
        categories.add(parent_id)
        for child in category["subcategories"]:
            child_id = child["id"]
            if child_id in categories or not child_id.startswith(parent_id + "."):
                raise ValueError(f"duplicate or unrelated child ID {child_id}")
            categories.add(child_id)
            parents[child_id] = parent_id
    if len(parents) > 24:
        raise ValueError("taxonomy has more than 24 subcategories")
    missing_required = set(taxonomy.get("required_broad_categories", [])) - {row["id"] for row in broad}
    if missing_required:
        raise ValueError(f"missing required broad categories: {sorted(missing_required)}")
    return categories, parents


def validate(worklist_path: Path, taxonomy_path: Path, labels_path: Path) -> None:
    work = {int(row["id"]): row for row in read_jsonl(worklist_path)}
    taxonomy = json.loads(taxonomy_path.read_text(encoding="utf-8"))
    semantic, parents = taxonomy_ids(taxonomy)
    known = semantic | set(BINARY_FEATURES)
    seen: set[int] = set()
    errors: list[str] = []
    for row in read_jsonl(labels_path):
        ident = int(row.get("id", 0))
        if ident in seen or ident not in work:
            errors.append(f"duplicate or unknown id {ident}")
            continue
        seen.add(ident)
        if row.get("taxonomy_version") != taxonomy["version"]:
            errors.append(f"{ident}: taxonomy version mismatch")
        if row.get("text_sha256") != work[ident]["text_sha256"]:
            errors.append(f"{ident}: text hash mismatch")
        if row.get("all_other_labels") != "нет":
            errors.append(f"{ident}: explicit negative declaration missing")
        yes = set(row.get("yes", []))
        unclear = set(row.get("unclear", []))
        if row.get("unclear_semantic") is True:
            unclear.update(semantic)
        if not yes <= known or not unclear <= known or yes & unclear:
            errors.append(f"{ident}: invalid or conflicting label IDs")
        for child, parent in parents.items():
            if child in yes and parent not in yes:
                errors.append(f"{ident}: {child} requires {parent}")
        if "is_event_invitation" in yes and "is_event_related" not in yes:
            errors.append(f"{ident}: invitation requires event-related")
        if row.get("technical_complexity") not in {0, 1, 2, 3, 4, 5, "неясно"}:
            errors.append(f"{ident}: invalid technical complexity")
        if row.get("needs_review") not in {True, False}:
            errors.append(f"{ident}: needs_review must be boolean")
        if not isinstance(row.get("reason"), str) or not row["reason"].strip():
            errors.append(f"{ident}: short reasoning missing")
    print(json.dumps({"total_to_label": len(work), "labeled": len(seen), "remaining": len(work) - len(seen), "errors": errors[:50]}, ensure_ascii=False))
    if errors:
        raise SystemExit(1)


def record(worklist_path: Path, taxonomy_path: Path, staging_path: Path, labels_path: Path,
           allow_holdout: bool = False) -> None:
    if labels_path.resolve().is_relative_to(Path.cwd().resolve()):
        raise ValueError("labels must be outside the Git checkout")
    work = {int(row["id"]): row for row in read_jsonl(worklist_path)}
    existing = list(read_jsonl(labels_path)) if labels_path.exists() else []
    existing_ids = {int(row["id"]) for row in existing}
    taxonomy = json.loads(taxonomy_path.read_text(encoding="utf-8"))
    additions = []
    for row in read_jsonl(staging_path):
        ident = int(row["id"])
        if ident not in work or ident in existing_ids:
            raise ValueError(f"unknown or already recorded id {ident}")
        if not allow_holdout and work[ident].get("partition_hint") == "blind_test_candidate":
            raise ValueError(f"holdout text {ident} is reserved until taxonomy and training are frozen")
        existing_ids.add(ident)
        additions.append({
            **row,
            "taxonomy_version": taxonomy["version"],
            "text_sha256": work[ident]["text_sha256"],
            "provenance": "codex_agent",
        })
    temporary = labels_path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for row in existing + additions:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    try:
        validate(worklist_path, taxonomy_path, temporary)
        temporary.replace(labels_path)
    finally:
        temporary.unlink(missing_ok=True)
    print(json.dumps({"recorded": len(additions), "total": len(existing) + len(additions)}))


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("snapshot", type=Path)
    prepare.add_argument("output", type=Path)
    show = commands.add_parser("next")
    show.add_argument("worklist", type=Path)
    show.add_argument("labels", type=Path)
    show.add_argument("--size", type=int, default=20)
    show.add_argument("--include-holdout", action="store_true")
    check = commands.add_parser("validate")
    check.add_argument("worklist", type=Path)
    check.add_argument("taxonomy", type=Path)
    check.add_argument("labels", type=Path)
    save = commands.add_parser("record")
    save.add_argument("worklist", type=Path)
    save.add_argument("taxonomy", type=Path)
    save.add_argument("staging", type=Path)
    save.add_argument("labels", type=Path)
    save.add_argument("--allow-holdout", action="store_true")
    args = parser.parse_args()
    if args.command == "prepare":
        worklist(args.snapshot, args.output)
    elif args.command == "next":
        next_batch(args.worklist, args.labels, args.size, args.include_holdout)
    elif args.command == "validate":
        validate(args.worklist, args.taxonomy, args.labels)
    else:
        record(args.worklist, args.taxonomy, args.staging, args.labels, args.allow_holdout)


if __name__ == "__main__":
    main()
