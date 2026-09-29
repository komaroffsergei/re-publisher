"""Combine private MAX snapshots for review without assigning any labels."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


def rows(path: Path):
    with gzip.open(path, 'rt', encoding='utf-8') as source:
        for line in source:
            if line.strip():
                yield json.loads(line)


def combine(primary: Path, supplement: Path, output_directory: Path):
    if output_directory.resolve().is_relative_to(Path.cwd().resolve()):
        raise ValueError('Output must be outside the Git checkout')
    output_directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    target = output_directory / f'max-training-{stamp}.jsonl.gz'
    temporary = output_directory / f'.{target.name}.tmp'
    seen_keys: set[tuple[int, int]] = set()
    seen_ids: set[int] = set()
    source_counts = {'primary': 0, 'supplement': 0}
    digest = hashlib.sha256()
    try:
        with gzip.open(temporary, 'wb') as output:
            for source_name, path in (('primary', primary), ('supplement', supplement)):
                for row in rows(path):
                    ident = int(row['id'])
                    key = (int(row['chat_peer_id']), int(row['message_id']))
                    if key in seen_keys:
                        if source_name == 'primary':
                            raise ValueError(f'Duplicate primary message key: {key}')
                        continue
                    if ident in seen_ids:
                        raise ValueError(f'Duplicate corpus ID: {ident}')
                    seen_keys.add(key)
                    seen_ids.add(ident)
                    row.setdefault('source', source_name)
                    encoded = json.dumps(row, ensure_ascii=False, separators=(',', ':')).encode('utf-8') + b'\n'
                    output.write(encoded)
                    digest.update(encoded)
                    source_counts[source_name] += 1
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    manifest = {
        'created_at': datetime.now(timezone.utc).isoformat(),
        'file': target.name,
        'sources': {'primary': primary.name, 'supplement': supplement.name},
        'posts_by_source': source_counts,
        'jsonl_sha256': digest.hexdigest(),
    }
    target.with_suffix('.manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('primary', type=Path)
    parser.add_argument('supplement', type=Path)
    parser.add_argument('output_directory', type=Path)
    args = parser.parse_args()
    print(json.dumps(combine(args.primary, args.supplement, args.output_directory)))


if __name__ == '__main__':
    main()
