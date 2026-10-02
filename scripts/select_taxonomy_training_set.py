"""Freeze a diverse private MAX text worklist for Codex annotation.

Keyword pools only find texts worth reading. They never assign labels.
The recent holdout is selected before its texts are reviewed for taxonomy work.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path


POOLS = (
    ("vacancy", 120, r"ваканси|вакансия|\bищем\b|нанимаем|hiring|#vacancy|отклик"),
    ("paper", 50, r"arxiv|\bdoi\b|препринт|\bpaper\b|научн.{0,20}стат"),
    ("event", 80, r"митап|вебинар|конференц|регистрац|стрим|форум|семинар|встреч|лекц"),
    ("joke", 70, r"мем[а-я]*\b|шут|анекдот|\bлол\b|юмор|\bкек\b|смешн"),
    ("tool", 160, r"инструмент|сервис|плагин|\bcli\b|\bapi\b|бот|функци|\bskill\b|скилл|редактор"),
    ("education_health", 60, r"пациент|медицин|врач|диагност|обучени|школ|университет|репетитор"),
    ("policy", 50, r"закон|регулиров|запрет|санкци|госдума|правительств"),
    ("security", 50, r"уязвим|эксплойт|взлом|безопасност|атак|prompt injection"),
)


def read_jsonl(path: Path):
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def length_bucket(text: str) -> str:
    length = len(text.strip())
    return "short" if length < 80 else "medium" if length < 500 else "long"


def score(seed: int, row: dict) -> bytes:
    return hashlib.sha256(f"{seed}:{row['id']}".encode()).digest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("worklist", type=Path)
    parser.add_argument("labels", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--groups", type=Path, required=True)
    parser.add_argument("--size", type=int, default=3000)
    parser.add_argument("--holdout", type=int, default=450)
    parser.add_argument("--test-after", default="2026-09-27")
    parser.add_argument("--seed", type=int, default=20260929)
    args = parser.parse_args()
    if args.size < 1 or args.holdout < 1 or args.holdout >= args.size:
        raise ValueError("invalid worklist or holdout size")
    if args.output.resolve().is_relative_to(Path.cwd().resolve()):
        raise ValueError("the text worklist must stay outside the Git checkout")

    rows = list(read_jsonl(args.worklist))
    by_id = {int(row["id"]): row for row in rows}
    if len(by_id) != len(rows):
        raise ValueError("duplicate worklist IDs")
    labeled = list(read_jsonl(args.labels))
    labeled_ids = {int(row["id"]) for row in labeled}
    if len(labeled_ids) != len(labeled) or not labeled_ids <= by_id.keys():
        raise ValueError("duplicate or unknown labeled IDs")
    if len(labeled_ids) >= args.size - args.holdout:
        raise ValueError("existing labels leave no room for new training texts")
    for row in labeled:
        if row.get("text_sha256") != by_id[int(row["id"])]["text_sha256"]:
            raise ValueError(f"labeled text hash mismatch: {row['id']}")
    groups = list(read_jsonl(args.groups))
    parent: dict[int, int] = {}

    def find(ident: int) -> int:
        parent.setdefault(ident, ident)
        if parent[ident] != ident:
            parent[ident] = find(parent[ident])
        return parent[ident]

    for group in groups:
        members = [int(ident) for ident in group["candidate_ids"] if int(ident) in by_id]
        if members:
            root = find(members[0])
            for ident in members[1:]:
                parent[find(ident)] = root
    component_by_text = {ident: find(ident) for ident in parent}

    # The largest chats are capped rather than being allowed to supply most
    # of the selected texts. Caps are selection controls, not class labels.
    counts = Counter(row["source_chats"][0] for row in rows)
    top_chats = {chat for chat, _ in counts.most_common(2)}
    total_chat_cap = {chat: (650 if chat in top_chats else 200) for chat in counts}
    total_length_cap = {"short": 600, "medium": 1350, "long": 1050}
    test_length_cap = {"short": 90, "medium": 200, "long": 160}
    if args.size != 3000 or args.holdout != 450:
        raise ValueError("this frozen selection recipe is defined for 3000/450 only")

    selected: dict[int, dict] = {}
    selected_chats: Counter = Counter()
    selected_lengths: Counter = Counter()
    test_chats: Counter = Counter()
    test_lengths: Counter = Counter()
    selected_group_partitions: dict[int, str] = {}
    rejected_cross_partition = 0

    def add(row: dict, reason: str, partition: str) -> bool:
        nonlocal rejected_cross_partition
        ident = int(row["id"])
        if ident in selected:
            return False
        component = component_by_text.get(ident)
        if component is not None and selected_group_partitions.get(component, partition) != partition:
            rejected_cross_partition += 1
            return False
        chat = row["source_chats"][0]
        bucket = length_bucket(row["text"])
        if selected_chats[chat] >= total_chat_cap[chat] or selected_lengths[bucket] >= total_length_cap[bucket]:
            return False
        if partition == "blind_test_candidate":
            if test_chats[chat] >= (150 if chat in top_chats else 60):
                return False
            if test_lengths[bucket] >= test_length_cap[bucket]:
                return False
            test_chats[chat] += 1
            test_lengths[bucket] += 1
        selected[ident] = {**row, "selection_reason": reason, "partition_hint": partition}
        if component is not None:
            selected_group_partitions[component] = partition
        selected_chats[chat] += 1
        selected_lengths[bucket] += 1
        return True

    # Previously inspected texts cannot be used as a blind test.
    for ident in sorted(labeled_ids):
        if not add(by_id[ident], "existing_codex_label", "development"):
            raise ValueError(f"selection caps exclude an existing label: {ident}")

    ordered = sorted(rows, key=lambda row: score(args.seed, row))
    recent = [row for row in ordered if row["first_posted_at"] >= args.test_after and int(row["id"]) not in labeled_ids]
    for row in recent:
        if sum(test_lengths.values()) >= args.holdout:
            break
        add(row, "recent_unreviewed_holdout", "blind_test_candidate")
    if sum(test_lengths.values()) != args.holdout:
        raise ValueError(f"could select only {sum(test_lengths.values())} holdout texts")

    candidates_found = {}
    candidates_added = {}
    for name, quota, pattern in POOLS:
        expression = re.compile(pattern, re.IGNORECASE)
        candidates = [row for row in ordered if expression.search(row["text"])]
        candidates_found[name] = len(candidates)
        added = 0
        for row in candidates:
            if added >= quota:
                break
            if add(row, f"keyword_candidate:{name}", "development"):
                added += 1
        candidates_added[name] = added

    for row in ordered:
        if len(selected) >= args.size:
            break
        add(row, "balanced_representative", "development")
    if len(selected) != args.size:
        raise ValueError(f"selection caps allowed only {len(selected)} of {args.size} texts")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="\n") as handle:
        for row in sorted(selected.values(), key=lambda item: int(item["id"])):
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    manifest = {
        "source_worklist": args.worklist.name,
        "source_worklist_sha256": hashlib.sha256(args.worklist.read_bytes()).hexdigest(),
        "size": len(selected),
        "existing_codex_labels": len(labeled_ids),
        "recent_holdout_unreviewed": sum(test_lengths.values()),
        "test_after": args.test_after,
        "seed": args.seed,
        "reviewed_group_file": args.groups.name,
        "reviewed_group_file_sha256": hashlib.sha256(args.groups.read_bytes()).hexdigest(),
        "reviewed_group_count": len(groups),
        "candidates_rejected_to_avoid_known_cross_partition_groups": rejected_cross_partition,
        "selection_reasons": dict(Counter(row["selection_reason"] for row in selected.values())),
        "length_buckets": dict(selected_lengths),
        "distinct_primary_chats": len(selected_chats),
        "top_two_chats_selected": sum(selected_chats[chat] for chat in top_chats),
        "keyword_candidates_found_not_labels": candidates_found,
        "keyword_candidates_selected_not_labels": candidates_added,
        "text_and_private_ids_remain_outside_git": True,
    }
    manifest_path = args.output.with_suffix(".manifest.json")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    main()
