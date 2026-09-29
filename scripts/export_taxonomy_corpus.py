"""Export a read-only MAX snapshot to a private local directory.

The command never prints post text. It needs the existing SSH alias and Docker
access on the VPS, but no Telegram session or database password on this host.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path


QUERY = """
SELECT row_to_json(snapshot)::text
FROM (
    SELECT p.id, p.chat_peer_id, p.message_id, p.date, p.text,
           p.media_type, p.grouped_id, p.is_deleted
    FROM telegram_posts AS p
    JOIN telegram_chats AS c ON c.peer_id = p.chat_peer_id
    WHERE c.folder_name = 'MAX' AND p.is_deleted = false
    ORDER BY p.id
) AS snapshot;
"""


def export_snapshot(directory: Path, host: str) -> dict[str, object]:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        directory.chmod(0o700)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = directory / f"max-posts-{timestamp}.jsonl.gz"
    temporary = directory / f".{target.name}.tmp"
    command = [
        "ssh", host, "sudo docker exec -i portfolio-infra-postgres-1 "
        "psql -X -q -A -t -v ON_ERROR_STOP=1 -U publisher -d publisher",
    ]
    result = subprocess.run(command, input=QUERY.encode("utf-8"), capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(f"read-only export failed (exit {result.returncode})")

    count = 0
    distinct_chats: set[int] = set()
    sha = hashlib.sha256()
    try:
        with gzip.open(temporary, "wb") as output:
            for line in result.stdout.splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                if not isinstance(row, dict) or "id" not in row or "text" not in row:
                    raise ValueError("unexpected snapshot row")
                normalized_line = json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
                output.write(normalized_line)
                sha.update(normalized_line)
                count += 1
                distinct_chats.add(int(row["chat_peer_id"]))
        if count == 0:
            raise ValueError("empty snapshot")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": "read-only publisher PostgreSQL snapshot, Telegram folder MAX",
        "file": target.name,
        "posts": count,
        "chats_with_posts": len(distinct_chats),
        "jsonl_sha256": sha.hexdigest(),
    }
    manifest_path = directory / f"max-posts-{timestamp}.manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="Private directory outside the Git checkout")
    parser.add_argument("--host", default="wtg-prod-vdsina")
    args = parser.parse_args()
    print(json.dumps(export_snapshot(args.directory, args.host), ensure_ascii=False))


if __name__ == "__main__":
    main()
