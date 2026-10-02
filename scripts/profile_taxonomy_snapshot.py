"""Print aggregate corpus measurements; never print raw Telegram content."""

from __future__ import annotations

import argparse
import collections
import gzip
import json
import re
import statistics
from datetime import datetime
from pathlib import Path


def profile(snapshot: Path) -> dict:
    chats = collections.Counter()
    days = collections.Counter()
    sources = collections.Counter()
    normalized = collections.Counter()
    lengths: list[int] = []
    total = empty = short = media = cyrillic = latin = 0
    with gzip.open(snapshot, 'rt', encoding='utf-8') as source:
        for line in source:
            if not line.strip():
                continue
            row = json.loads(line)
            total += 1
            chats[int(row['chat_peer_id'])] += 1
            days[str(row['date'])[:10]] += 1
            sources[row.get('source', 'primary')] += 1
            text = (row.get('text') or '').strip()
            if not text:
                empty += 1
            else:
                key = re.sub(r'\s+', ' ', text.casefold()).strip()
                normalized[key] += 1
                lengths.append(len(text))
                if len(text) < 20:
                    short += 1
                letters_ru = sum('а' <= char.lower() <= 'я' or char.lower() == 'ё' for char in text)
                letters_en = sum('a' <= char.lower() <= 'z' for char in text)
                if letters_ru > letters_en:
                    cyrillic += 1
                elif letters_en > letters_ru:
                    latin += 1
            if row.get('media_type') or row.get('has_media'):
                media += 1
    if total == 0:
        raise ValueError('Empty snapshot')
    ordered = sorted(chats.values(), reverse=True)
    exact_duplicate_extras = sum(count - 1 for count in normalized.values())
    result = {
        'snapshot': snapshot.name,
        'posts': total,
        'chats_with_posts': len(chats),
        'top_two_chat_posts': sum(ordered[:2]),
        'top_two_chat_fraction': round(sum(ordered[:2]) / total, 4),
        'empty_text': empty,
        'text_under_20_chars': short,
        'unique_nonempty_texts': len(normalized),
        'exact_duplicate_extra_posts': exact_duplicate_extras,
        'posts_with_media': media,
        'cyrillic_dominant_texts': cyrillic,
        'latin_dominant_texts': latin,
        'length_chars_nonempty': {
            'median': int(statistics.median(lengths)) if lengths else 0,
            'p90': sorted(lengths)[int((len(lengths) - 1) * .9)] if lengths else 0,
            'max': max(lengths, default=0),
        },
        'posts_by_day': dict(sorted(days.items())),
        'posts_by_source': dict(sources),
        'note': 'Character-length and script statistics are descriptive, not semantic labels.',
    }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = profile(args.snapshot)
    if args.output:
        if args.output.resolve().is_relative_to(Path.cwd().resolve()):
            raise ValueError('Profile output must be outside the Git checkout')
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
