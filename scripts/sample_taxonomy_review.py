"""Select a reproducible, diverse private text batch for Codex to read.

Sampling is not labeling. Chat IDs are used only to prevent one prolific chat
from dominating a batch; neither chat names nor cluster IDs enter decisions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


def read_jsonl(path: Path):
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def bucket(text: str) -> str:
    length = len(text.strip())
    if length < 80:
        return "short"
    if length < 500:
        return "medium"
    return "long"


def score(seed: int, ident: int) -> bytes:
    return hashlib.sha256(f"{seed}:{ident}".encode()).digest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("worklist", type=Path)
    parser.add_argument("labels", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--size", type=int, default=24)
    parser.add_argument("--seed", type=int, default=20260929)
    parser.add_argument("--include-holdout", action="store_true")
    parser.add_argument("--selection-reason", help="Optional retrieval pool to read; never a label")
    parser.add_argument("--unbalanced-lengths", action="store_true")
    parser.add_argument("--length-bucket", choices=("short", "medium", "long"))
    parser.add_argument("--text-regex", help="Retrieval clue to choose texts for human reading; never a label")
    args = parser.parse_args()
    if args.size < 1:
        raise ValueError("size must be positive")
    if args.output.resolve().is_relative_to(Path.cwd().resolve()):
        raise ValueError("private review batch must stay outside the Git checkout")

    labeled = {int(row["id"]) for row in read_jsonl(args.labels)}
    retrieval = re.compile(args.text_regex, re.IGNORECASE | re.DOTALL) if args.text_regex else None
    rows = [
        row for row in read_jsonl(args.worklist)
        if int(row["id"]) not in labeled
        and (args.include_holdout or row.get("partition_hint") != "blind_test_candidate")
        and (not args.selection_reason or row.get("selection_reason") == args.selection_reason)
        and (not args.length_bucket or bucket(row["text"]) == args.length_bucket)
        and (retrieval is None or retrieval.search(row["text"]))
    ]
    rows.sort(key=lambda row: score(args.seed, int(row["id"])))
    chosen = []
    chosen_ids = set()
    used_chats = set()
    if args.unbalanced_lengths:
        for distinct_chat in (True, False):
            for row in rows:
                if len(chosen) >= args.size:
                    break
                ident = int(row["id"])
                if ident in chosen_ids:
                    continue
                chats = set(row["source_chats"])
                if distinct_chat and chats & used_chats:
                    continue
                chosen.append({**row, "length_bucket": bucket(row["text"])})
                chosen_ids.add(ident)
                used_chats.update(chats)
    else:
        targets = {name: args.size // 3 for name in ("short", "medium", "long")}
        for name in ("medium", "long")[: args.size % 3]:
            targets[name] += 1

        def pick(name: str, distinct_chat: bool) -> None:
            for row in rows:
                ident = int(row["id"])
                if len([item for item in chosen if item["length_bucket"] == name]) >= targets[name]:
                    return
                if ident in chosen_ids or bucket(row["text"]) != name:
                    continue
                chats = set(row["source_chats"])
                if distinct_chat and chats & used_chats:
                    continue
                chosen.append({**row, "length_bucket": name})
                chosen_ids.add(ident)
                used_chats.update(chats)

        for length_bucket in targets:
            pick(length_bucket, distinct_chat=True)
        for length_bucket in targets:
            pick(length_bucket, distinct_chat=False)
    if len(chosen) != args.size:
        raise ValueError("not enough unlabeled texts in requested length buckets")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="\n") as handle:
        for row in chosen:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps({
        "selected": len(chosen),
        "distinct_chats": len(used_chats),
        "length_buckets": {name: sum(row["length_bucket"] == name for row in chosen) for name in ("short", "medium", "long")},
        "selection_reason": args.selection_reason,
        "retrieval_regex_used": args.text_regex is not None,
        "output": args.output.name,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
