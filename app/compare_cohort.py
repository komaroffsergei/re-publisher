"""Classify one reserved MAX cohort with each production model, preserving both runs."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from sqlalchemy import select

from app.config import get_settings
from app.db import create_engine, create_session_factory, session_scope
from app.import_comparison import load_cohort
from app.models import PipelineEntry, TaxonomyClassification, TelegramPost
from app.taxonomy.jobs import enqueue, text_sha256
from app.taxonomy.artifact import artifact_version, checkpoint_path, configured_artifact


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cohort", type=Path)
    parser.add_argument("--expect", type=int, default=100)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    args = parser.parse_args()
    selected = load_cohort(args.cohort, args.expect)
    settings = get_settings()
    engine = create_engine(settings)
    factory = create_session_factory(engine=engine)
    try:
        entries = []
        async with factory() as session:
            for peer, message_rows in selected.items():
                for message_id in message_rows:
                    entry_id = (await session.execute(
                        select(PipelineEntry.id)
                        .join(TelegramPost, TelegramPost.id == PipelineEntry.source_post_id)
                        .where(TelegramPost.chat_peer_id == peer, TelegramPost.message_id == message_id)
                    )).scalar_one_or_none()
                    if entry_id is None:
                        raise RuntimeError(f"comparison post not imported: {peer}/{message_id}")
                    entries.append(int(entry_id))
        if len(entries) != args.expect or len(set(entries)) != args.expect:
            raise RuntimeError("comparison cohort does not map to unique pipeline cards")
        for model_key in ("tfidf", "minilm"):
            directory = Path(settings.taxonomy_model_dir)
            weights = configured_artifact(directory, model_key)
            if model_key == 'minilm':
                weights = checkpoint_path(weights, json.loads((weights / 'training.json').read_text(encoding='utf-8')))
            expected_version = artifact_version(directory, model_key, weights)
            for entry_id in entries:
                async with session_scope(factory) as session:
                    previous = (await session.execute(
                        select(TaxonomyClassification, TelegramPost.text)
                        .join(TelegramPost, TelegramPost.id == TaxonomyClassification.source_post_id)
                        .where(TaxonomyClassification.pipeline_entry_id == entry_id,
                               TaxonomyClassification.model_key == model_key)
                    )).first()
                    if previous is not None:
                        job, current_text = previous
                        if ((job.model_version == expected_version or job.status in {'queued', 'running'})
                                and job.text_sha256 == text_sha256(current_text)
                                and job.status in {"queued", "running", "complete"}):
                            continue
                    await enqueue(session, entry_id, model_key)
            deadline = time.monotonic() + args.timeout_seconds
            while True:
                async with factory() as session:
                    rows = (await session.execute(
                        select(TaxonomyClassification.pipeline_entry_id, TaxonomyClassification.status,
                               TaxonomyClassification.elapsed_ms)
                        .where(TaxonomyClassification.pipeline_entry_id.in_(entries),
                               TaxonomyClassification.model_key == model_key)
                    )).all()
                statuses = {int(row[0]): (row[1], row[2]) for row in rows}
                if len(statuses) == len(entries) and all(status == "complete" for status, _ in statuses.values()):
                    elapsed = sorted(int(ms) for _, ms in statuses.values() if ms is not None)
                    print(json.dumps({"model": model_key, "complete": len(entries),
                                      "median_ms": elapsed[len(elapsed)//2], "max_ms": max(elapsed)}), flush=True)
                    break
                failed = [entry_id for entry_id, (status, _) in statuses.items() if status in {"failed", "stale"}]
                if failed:
                    raise RuntimeError(f"{model_key} failed or went stale on {len(failed)} entries")
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"{model_key} timed out: {len(statuses)} jobs observed")
                await asyncio.sleep(2)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
